import json

from django.http import HttpResponse, HttpResponseNotAllowed, JsonResponse

from .api_auth import authenticate_request, csrf_exempt_api
from .sms_service import ApiError, create_message, delete_message, get_message, list_messages, update_message

MAX_BODY_BYTES = 131072


def _error(exc):
    return JsonResponse({"error": exc.message}, status=exc.status)


def _json_body(request):
    if len(request.body) > MAX_BODY_BYTES:
        raise ApiError("Request body is too large.", 413)
    if request.content_type != "application/json":
        raise ApiError("Content-Type must be application/json.", 415)
    try:
        value = json.loads(request.body)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        raise ApiError("Request body must contain valid JSON.")
    if not isinstance(value, dict):
        raise ApiError("Expected a JSON object.")
    return value


def _integer(value, label, minimum, maximum=None):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise ApiError(f"{label} must be an integer.")
    if str(parsed) != str(value) or parsed < minimum or (maximum is not None and parsed > maximum):
        raise ApiError(f"{label} is outside the supported range.")
    return parsed


def _run(request, kind, method, allowed_methods):
    denied = authenticate_request(request, kind)
    if denied:
        return denied
    if request.method not in allowed_methods:
        return HttpResponseNotAllowed(allowed_methods)
    try:
        return method()
    except ApiError as exc:
        return _error(exc)


@csrf_exempt_api
def messages(request):
    def execute():
        if request.method == "GET":
            query = request.GET.get("q", "")
            if len(query) > 200:
                raise ApiError("q must be at most 200 characters.")
            raw_read = request.GET.get("read")
            if raw_read not in (None, "true", "false"):
                raise ApiError("read must be true or false.")
            data = list_messages(
                page=_integer(request.GET.get("page", "1"), "page", 1),
                page_size=_integer(request.GET.get("page_size", "30"), "page_size", 1, 100),
                query=query,
                read=None if raw_read is None else raw_read == "true",
            )
            return JsonResponse(data)
        return JsonResponse(create_message(_json_body(request)), status=201)

    return _run(request, "rest", execute, ("GET", "POST"))


@csrf_exempt_api
def message(request, pk):
    def execute():
        if request.method == "GET":
            return JsonResponse(get_message(pk))
        if request.method == "DELETE":
            delete_message(pk)
            return HttpResponse(status=204)
        data = _json_body(request)
        return JsonResponse(update_message(pk, data, partial=request.method == "PATCH"))

    return _run(request, "rest", execute, ("GET", "PUT", "PATCH", "DELETE"))
