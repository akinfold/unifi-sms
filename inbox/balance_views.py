from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST

from .api_views import _json_body
from .sms_service import ApiError
from .views import admin_required, initialized


@require_GET
def page(request):
    if not initialized():
        return redirect("setup")
    if not request.user.is_authenticated or not request.user.is_active or not request.user.is_staff:
        return redirect("login")
    return render(request, "inbox/balance.html")


@require_GET
@admin_required
@never_cache
def status(request):
    from . import balance
    return JsonResponse(balance.status())


@require_POST
@admin_required
def action(request):
    from . import balance
    try:
        data = _json_body(request)
        operation = data.pop("action", None)
        return JsonResponse(balance.action(operation, data))
    except ApiError as exc:
        return JsonResponse({"error": exc.message}, status=exc.status)
