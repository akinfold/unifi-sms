import re

from django.http import HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt

from .api_keys import check_api_key, is_api_key_digest, make_api_key_hash
from .models import IntegrationSettings
from .proxy import client_address
from .rate_limits import exhausted, reserve, window_key


BEARER_RE = re.compile(r"^Bearer ([!-~]{1,256})$", re.IGNORECASE)
FAILURE_LIMIT = 30
LEGACY_CLIENT_LIMIT = 5
LEGACY_GLOBAL_LIMIT = 20
RATE_SECONDS = 60


def _settings():
    return IntegrationSettings.objects.filter(pk=1).first()


def _denied(status=401):
    response = JsonResponse({"error": "Too many authentication attempts." if status == 429 else "Bearer API key required."}, status=status)
    response["WWW-Authenticate"] = 'Bearer realm="sms-archive"'
    if status == 429:
        response["Retry-After"] = str(RATE_SECONDS)
    return response


def authenticate_request(request, kind):
    config = _settings()
    if config is None or not getattr(config, f"{kind}_enabled", False):
        return HttpResponse(status=404)
    header = request.META.get("HTTP_AUTHORIZATION", "")
    match = BEARER_RE.fullmatch(header) if len(header) <= 263 else None
    supplied = match.group(1) if match else ""
    field = f"{kind}_key_hash"
    encoded = getattr(config, field, "")
    address = client_address(request)
    failure_key = window_key("api-failure", address)
    if supplied and encoded:
        legacy = not is_api_key_digest(encoded)
        # Legacy passwords need a shared CPU budget until a successful request upgrades them.
        if legacy and (exhausted(failure_key, FAILURE_LIMIT, RATE_SECONDS) or not reserve([
            (window_key("api-legacy-client", address), LEGACY_CLIENT_LIMIT),
            (window_key("api-legacy-global"), LEGACY_GLOBAL_LIMIT),
        ], RATE_SECONDS)):
            return _denied(429)
        if check_api_key(supplied, encoded):
            if legacy:
                IntegrationSettings.objects.filter(pk=config.pk, **{field: encoded}).update(**{field: make_api_key_hash(supplied)})
            return None
    admitted = reserve([(failure_key, FAILURE_LIMIT)], RATE_SECONDS)
    return _denied(401 if admitted else 429)


def csrf_exempt_api(view):
    return csrf_exempt(view)
