import hashlib
import json
from datetime import timedelta
from functools import wraps

from django.conf import settings
from django.contrib.auth import login
from django.contrib.auth.models import User
from django.contrib.auth.views import LoginView
from django.core.paginator import Paginator
from django.db import connection, transaction
from django.db.models import Q
from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from .device import configured, device_status, phone_status, request_phone_refresh
from .forms import AdminAuthenticationForm, SetupForm
from .models import AccessWindow, ForwardDelivery, Installation, Message, Receipt, SyncStatus
from .smtp_config import smtp_forwarding_active


def initialized():
    return (
        Installation.objects.filter(pk=1, completed=True).exists()
        or User.objects.filter(is_superuser=True).exists()
    )


def rate_limited(request):
    key = hashlib.sha256((request.META.get("REMOTE_ADDR", "") + request.path).encode()).hexdigest()
    now = timezone.now()
    with transaction.atomic():
        window, _ = AccessWindow.objects.get_or_create(key=key, defaults={"started_at": now})
        if now - window.started_at > timedelta(minutes=15):
            window.started_at, window.attempts = now, 0
        window.attempts += 1
        window.save()
        AccessWindow.objects.filter(started_at__lt=now - timedelta(days=1)).delete()
        return window.attempts > 10


def admin_required(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if (
            not request.user.is_authenticated
            or not request.user.is_active
            or not request.user.is_staff
        ):
            return JsonResponse({"error": "Administrator login required."}, status=403)
        return view(request, *args, **kwargs)

    return wrapped


class AdminLogin(LoginView):
    template_name = "inbox/login.html"
    authentication_form = AdminAuthenticationForm

    def dispatch(self, request, *args, **kwargs):
        if not initialized():
            return redirect("setup")
        if request.method == "POST" and rate_limited(request):
            return HttpResponse("Too many attempts. Try again in 15 minutes.", status=429)
        return super().dispatch(request, *args, **kwargs)


def setup(request):
    if initialized():
        return redirect("login")
    if request.method == "POST" and rate_limited(request):
        return HttpResponse("Too many attempts. Try again in 15 minutes.", status=429)
    form = SetupForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            installation, _ = Installation.objects.get_or_create(pk=1)
            if installation.completed or User.objects.filter(is_superuser=True).exists():
                return redirect("login")
            user = form.save(commit=False)
            user.is_staff = user.is_superuser = True
            user.save()
            installation.completed = True
            installation.save()
        login(request, user)
        return redirect("home")
    return render(request, "inbox/setup.html", {"form": form})


@require_GET
def home(request):
    if not initialized():
        return redirect("setup")
    if not request.user.is_authenticated or not request.user.is_staff or not request.user.is_active:
        return redirect("login")
    if not configured():
        return redirect("device-settings")
    return render(request, "inbox/home.html")


def serialize_forwarding(message):
    try:
        delivery = message.forwarding
    except ForwardDelivery.DoesNotExist:
        delivery = None
    forwarding = {
        "state": "not_queued",
        "sent_at": None,
    }
    if delivery:
        forwarding = {
            "state": "sent"
            if delivery.sent_at
            else "error"
            if delivery.last_error
            else "pending",
            "sent_at": delivery.sent_at.isoformat() if delivery.sent_at else None,
        }
    return forwarding


def serialize(message, full=False):
    return {
        "id": message.id,
        "sender": message.sender,
        "received_at": message.received_at.isoformat(),
        "text": message.text if full else message.text[:140],
        "read": message.read_at is not None,
        "saved_parts": message.saved_parts,
        "total_parts": message.total_parts,
        "pending_cleanup": message.receipts.filter(cleared_at__isnull=True).exists(),
        "forwarding": serialize_forwarding(message),
    }


@require_GET
@admin_required
def message_list(request):
    messages = Message.objects.select_related("forwarding")
    query = request.GET.get("q", "")[:200]
    if query:
        messages = messages.filter(Q(sender__icontains=query) | Q(text__icontains=query))
    if request.GET.get("unread") == "1":
        messages = messages.filter(read_at__isnull=True)
    page = Paginator(messages, 30).get_page(request.GET.get("page", "1"))
    try:
        opened_id = int(request.GET.get("opened", ""))
    except ValueError:
        opened_id = 0
    opened_message = (
        Message.objects.select_related("forwarding").filter(pk=opened_id).first()
        if 0 < opened_id <= 2**63 - 1 else None
    )
    return JsonResponse(
        {
            "messages": [serialize(m) for m in page],
            "opened_forwarding": {
                "id": opened_message.pk,
                "forwarding": serialize_forwarding(opened_message),
            } if opened_message else None,
            "forwarding_enabled": smtp_forwarding_active(),
            "page": page.number,
            "pages": page.paginator.num_pages,
            "count": page.paginator.count,
            "unread": Message.objects.filter(read_at__isnull=True).count(),
        }
    )


@require_POST
@admin_required
def message_detail(request, pk):
    try:
        message = Message.objects.select_related("forwarding").get(pk=pk)
    except Message.DoesNotExist:
        return JsonResponse({"error": "Message not found."}, status=404)
    if message.read_at is None:
        message.read_at = timezone.now()
        message.save(update_fields=["read_at"])
    return JsonResponse(
        {**serialize(message, full=True), "forwarding_enabled": smtp_forwarding_active()}
    )


@require_POST
@admin_required
def delete_messages(request):
    try:
        data = json.loads(request.body)
        ids = data["ids"]
        if (
            not isinstance(ids, list)
            or not 1 <= len(ids) <= 100
            or any(type(i) is not int or i < 1 for i in ids)
        ):
            raise ValueError
    except (ValueError, KeyError, TypeError):
        return JsonResponse({"error": "Select between 1 and 100 messages."}, status=400)
    with transaction.atomic():
        if Receipt.objects.filter(message_id__in=ids, cleared_at__isnull=True).exists():
            return JsonResponse(
                {
                    "error": "Some messages are still on the device. Wait for cleanup before deleting their archive."
                },
                status=409,
            )
        Receipt.objects.filter(message_id__in=ids).update(pdu="")
        count = Message.objects.filter(pk__in=ids).count()
        Message.objects.filter(pk__in=ids).delete()
    return JsonResponse({"deleted": count})


@require_GET
@admin_required
def status(request):
    state = SyncStatus.objects.filter(pk=1).first()
    stale = (
        state is not None
        and state.attempted_at is not None
        and timezone.now() - state.attempted_at
        > timedelta(seconds=max(180, settings.POLL_INTERVAL * 3))
    )
    return JsonResponse(
        {
            "state": "stale" if stale else state.state if state else "waiting",
            "detail": "The importer has stopped reporting."
            if stale
            else state.detail
            if state
            else "Waiting for the importer.",
            "last_success": state.succeeded_at.isoformat()
            if state and state.succeeded_at
            else None,
            "pending": Receipt.objects.filter(cleared_at__isnull=True).count(),
            "phone": phone_status(),
            "device": device_status(),
        }
    )


@require_POST
@admin_required
def refresh_phone_number(request):
    if not configured():
        return JsonResponse({"error": "Configure the LTE SSH connection first."}, status=409)
    request_phone_refresh()
    return JsonResponse(phone_status(), status=202)


@require_GET
def health(request):
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
    return HttpResponse("ok", content_type="text/plain")
