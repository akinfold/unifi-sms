import uuid
from datetime import timedelta

from django.conf import settings
from django.contrib import messages
from django.db import DatabaseError, transaction
from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_http_methods, require_POST

from .connection import decrypt_secret, encrypt_secret, get_connection
from .connection_forms import AddressForm, SSHAccessForm
from .device import device_source, request_phone_refresh
from .discovery import discover_devices, search_networks
from .models import DeviceInfo, DeviceSettings, DeviceSetup
from .probe import ProbeError, probe_connection
from .trust import atomic_trust_update, generate_private_key, public_key_for, save_known_host
from .views import admin_required, initialized, rate_limited


KEY_HELP_URL = "https://help.ui.com/hc/en-us/articles/235247068-Adding-SSH-Keys-to-UniFi-Devices"
PROBE_LIFETIME = timedelta(minutes=10)
DRAFT_LIFETIME = timedelta(hours=24)


def _draft_secrets(draft):
    return {name: decrypt_secret(getattr(draft, name)) for name in ("password", "private_key")}


def _render(request, active, draft, *, step=None, form=None, error=""):
    if step is None:
        step = request.session.get("device_step") if draft else None
    if step is None:
        step = "address" if not draft else "access" if not draft.username else "trust" if draft.probe else "verify"
        if not draft and active and active.trusted_at:
            step = "summary"
    request.session["device_step"] = step
    has_password = bool(draft and draft.password)
    has_private_key = bool(draft and draft.private_key)
    if form is None and step == "address":
        form = AddressForm(initial={"host": draft.host if draft else ""})
    if form is None and step == "access":
        form = SSHAccessForm(initial={name: getattr(draft, name) for name in
                                     ("port", "username", "auth_method", "key_source")})
    return render(request, "inbox/device_settings.html", {
        "step": step, "form": form, "active": active, "draft": draft,
        "has_password": has_password, "has_private_key": has_private_key,
        "probe": draft.probe if draft else {}, "probe_id": draft.probe_id if draft else "",
        "error": error, "key_help_url": KEY_HELP_URL,
    })


@sensitive_post_parameters("password", "private_key")
@require_http_methods(["GET", "POST"])
def device_settings(request):
    if not initialized():
        return redirect("setup")
    if not request.user.is_authenticated or not request.user.is_active or not request.user.is_staff:
        return redirect("login")
    now = timezone.now()
    DeviceSetup.objects.filter(expires_at__lte=now).delete()
    active = DeviceSettings.objects.filter(pk=1).first()
    draft = DeviceSetup.objects.filter(owner=request.user).first()
    if request.method == "GET":
        if draft and draft.checked_at and now - draft.checked_at > PROBE_LIFETIME:
            draft.probe = {}
            draft.probe_id = None
            draft.checked_at = None
            DeviceSetup.objects.filter(pk=draft.pk, revision=draft.revision, probe_id__isnull=False).update(probe={}, probe_id=None, checked_at=None)
            request.session["device_step"] = "verify"
            draft.refresh_from_db()
        return _render(request, active, draft)
    action = request.POST.get("action")
    if action == "start":
        if draft:
            draft.delete()
        return _render(request, active, None, step="address")
    if action == "cancel":
        request.session.pop("device_step", None)
        if draft:
            draft.delete()
        return redirect("device-settings")
    if action == "choose-address":
        form = AddressForm(request.POST)
        if not form.is_valid():
            return _render(request, active, draft, step="address", form=form)
        if draft:
            draft.delete()
        draft = DeviceSetup(owner=request.user, host=form.cleaned_data["host"], expires_at=now + DRAFT_LIFETIME)
        # Reuse credentials only for the currently configured address.
        if active and active.host == draft.host:
            draft.port, draft.username, draft.auth_method = active.port, active.username, active.auth_method
            draft.password, draft.private_key, draft.public_key = active.password, active.private_key, active.public_key
        draft.save()
        request.session["device_step"] = "access"
        return _render(request, active, draft, step="access")
    if not draft:
        if action == "generate-key":
            return JsonResponse({"error": "This setup has expired. Choose the device address again."}, status=409)
        return _render(request, active, None, error="Start by choosing a device address.")
    if action == "change-address":
        return _render(request, active, draft, step="address")
    if action == "edit-access":
        return _render(request, active, draft, step="access")
    if action == "generate-key":
        if draft.key_source == "generate" and draft.private_key and draft.public_key:
            return JsonResponse({"public_key": draft.public_key})
        try:
            private_key, public_key = generate_private_key()
            encrypted_key = encrypt_secret(private_key)
        except (OSError, ValueError):
            return JsonResponse({"error": "Could not generate a key. Try again."}, status=500)
        changed = DeviceSetup.objects.filter(pk=draft.pk, owner=request.user, revision=draft.revision).update(
            private_key=encrypted_key, public_key=public_key, key_source="generate",
            auth_method="private_key", password="", probe={}, probe_id=None, checked_at=None,
            revision=uuid.uuid4(), expires_at=now + DRAFT_LIFETIME,
        )
        if not changed:
            return JsonResponse({"error": "The setup changed in another request. Reload this page and try again."}, status=409)
        request.session["device_step"] = "access"
        return JsonResponse({"public_key": public_key})
    if action == "save-access":
        try:
            secrets = _draft_secrets(draft)
        except ValueError:
            secrets = {"password": "", "private_key": ""}
        source = request.POST.get("key_source", "paste")
        if source != draft.key_source:
            secrets["private_key"] = ""
        form = SSHAccessForm(request.POST, stored_password=secrets["password"], stored_private_key=secrets["private_key"])
        if not form.is_valid():
            return _render(request, active, draft, step="access", form=form,
                           error="Access settings were not saved. Correct the fields below.")
        data = form.cleaned_data
        if data["auth_method"] == "private_key":
            draft.public_key = public_key_for(data["private_key"])
        else:
            draft.public_key = ""
        for name in ("port", "username", "auth_method", "key_source", "password", "private_key"):
            setattr(draft, name, data[name])
        draft.probe, draft.probe_id, draft.checked_at = {}, None, None
        previous_revision = draft.revision
        draft.revision = uuid.uuid4()
        draft.expires_at = now + DRAFT_LIFETIME
        changes = {name: getattr(draft, name) for name in
                   ("port", "username", "auth_method", "key_source", "public_key", "probe", "probe_id", "checked_at", "revision", "expires_at")}
        changes.update(password=encrypt_secret(draft.password),
                       private_key=encrypt_secret(draft.private_key))
        DeviceSetup.objects.filter(pk=draft.pk, revision=previous_revision).update(**changes)
        request.session["device_step"] = "verify"
        return redirect("device-settings")
    if action == "check":
        if rate_limited(request):
            return HttpResponse("Too many connection checks. Try again in 15 minutes.", status=429)
        if not draft.username or not (draft.password or draft.private_key):
            return _render(request, active, draft, step="access", error="Enter SSH access details first.")
        # Invalidate an older successful check before any network operation.
        revision = uuid.uuid4()
        if not DeviceSetup.objects.filter(pk=draft.pk, revision=draft.revision).update(
                revision=revision, probe={}, probe_id=None, checked_at=None):
            return redirect("device-settings")
        draft.probe, draft.probe_id, draft.checked_at = {}, None, None
        try:
            values = {name: getattr(draft, name) for name in ("host", "port", "username", "auth_method")}
            values.update(_draft_secrets(draft))
            result = probe_connection(values)
        except (ProbeError, ValueError):
            return _render(request, active, draft, step="verify",
                           error="Connection check failed. Check the address, SSH port, username, and credential. For key authentication, make sure its public key has been applied to the device.")
        token = uuid.uuid4()
        changed = DeviceSetup.objects.filter(pk=draft.pk, revision=revision).update(
            probe=result, probe_id=token, checked_at=timezone.now())
        if not changed:
            return redirect("device-settings")
        draft.refresh_from_db()
        return _render(request, active, draft, step="trust")
    if action == "trust":
        try:
            with atomic_trust_update(), transaction.atomic():
                draft = DeviceSetup.objects.select_for_update().filter(pk=draft.pk, owner=request.user).first()
                if (not draft or not draft.probe_id or str(draft.probe_id) != request.POST.get("probe_id")
                        or not draft.checked_at or timezone.now() - draft.checked_at > PROBE_LIFETIME):
                    return _render(request, active, draft, step="verify",
                                   error="This connection check has expired or changed. Check the connection again.")
                probe = draft.probe
                fingerprint = save_known_host(draft.host, draft.port, probe["host_key"], locked=True)
                if fingerprint != probe["fingerprint"]:
                    raise ValueError
                lookup = draft.host if draft.port == 22 else f"[{draft.host}]:{draft.port}"
                DeviceSettings.objects.update_or_create(pk=1, defaults={
                    "host": draft.host, "port": draft.port, "username": draft.username,
                    "auth_method": draft.auth_method, "password": draft.password, "private_key": draft.private_key,
                    "key_file": "", "known_hosts": str(settings.DATA_DIR / "known_hosts"),
                    "host_keys": f'{lookup} {probe["host_key"]}', "fingerprint": fingerprint,
                    "trusted_at": timezone.now(), "model": probe["model"], "name": probe["name"],
                    "address": probe["address"], "public_key": draft.public_key,
                })
                config = get_connection(_locked=True)
                DeviceInfo.objects.update_or_create(pk=device_source(config), defaults={
                    "model": probe["model"], "model_attempted_at": timezone.now(), "model_error": False,
                    "numbers": probe["numbers"], "checked_at": timezone.now(),
                })
                draft.delete()
        except (OSError, ValueError, KeyError, DatabaseError):
            return _render(request, active, draft, step="trust",
                           error="Could not save the trusted device. Your previous connection is unchanged. Check the persistent data volume and try again.")
        request.session.pop("device_step", None)
        request_phone_refresh()
        messages.success(request, "Device trusted. SMS import will start on the worker's next scan.")
        return redirect("home")
    return HttpResponse("Unknown setup action.", status=400)


@require_POST
@admin_required
def device_discovery(request):
    if rate_limited(request):
        return JsonResponse({"error": "Too many searches. Try again in 15 minutes."}, status=429)
    try:
        devices = discover_devices(request.POST.get("network") or None)
        return JsonResponse({"devices": devices, "networks": search_networks(request.POST.get("network") or None)})
    except ValueError:
        return JsonResponse({"error": "Enter a private IPv4 network with at most 256 addresses, such as a /24 subnet."}, status=400)
    except OSError:
        return JsonResponse({"error": "Network discovery is unavailable. Enter the device address manually."}, status=503)
