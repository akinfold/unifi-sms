import re

from django.contrib.auth.hashers import check_password
from django.http import HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt

from .models import IntegrationSettings


BEARER_RE = re.compile(r"^Bearer ([^\s]+)$", re.IGNORECASE)


def _settings():
    return IntegrationSettings.objects.filter(pk=1).first()


def authenticate_request(request, kind):
    config = _settings()
    if config is None or not getattr(config, f"{kind}_enabled", False):
        return HttpResponse(status=404)
    match = BEARER_RE.fullmatch(request.META.get("HTTP_AUTHORIZATION", ""))
    supplied = match.group(1) if match else ""
    encoded = getattr(config, f"{kind}_key_hash", "")
    if not supplied or not encoded or not check_password(supplied, encoded):
        response = JsonResponse({"error": "Bearer API key required."}, status=401)
        response["WWW-Authenticate"] = 'Bearer realm="sms-archive"'
        return response
    return None


def csrf_exempt_api(view):
    return csrf_exempt(view)
