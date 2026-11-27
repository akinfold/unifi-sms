from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_GET, require_POST

from .api_views import _json_body
from .outbound import enqueue, get_job, setup_action, setup_status
from .sms_service import ApiError
from .views import admin_required, initialized


@require_GET
def send_sms(request):
    if not initialized():
        return redirect("setup")
    if not request.user.is_authenticated or not request.user.is_active or not request.user.is_staff:
        return redirect("login")
    return render(request, "inbox/send_sms.html")


def _error(exc):
    return JsonResponse({"error": exc.message}, status=exc.status)


@require_GET
@admin_required
def status(request):
    result = setup_status()
    job_id = request.GET.get("job_id")
    if job_id:
        try:
            result["job"] = get_job(f"ui:{request.user.pk}", job_id)
        except ApiError as exc:
            return _error(exc)
    return JsonResponse(result)


@require_POST
@admin_required
def action(request):
    job = None
    try:
        data = _json_body(request)
        operation = data.pop("action", None)
        if operation == "send":
            key = data.pop("idempotency_key", None)
            job = enqueue(f"ui:{request.user.pk}", data, key)
        elif (
            operation in ("prepare", "cancel", "finish") and not data
            or operation == "start" and set(data) == {"phone"}
        ):
            setup_action(operation, data.get("phone"))
        else:
            raise ApiError("Invalid SMS action.")
    except ApiError as exc:
        return _error(exc)
    return JsonResponse(setup_status() | {"job": job}, status=202 if job else 200)
