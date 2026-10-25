import json
from urllib.parse import urlsplit

from django.conf import settings
from django.http import HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.utils.http import is_same_domain

from .api_auth import authenticate_request
from .json_input import load_json
from .sms_service import ApiError, create_message, delete_message, get_message, list_messages, update_message

SUPPORTED_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26")
MAX_BODY_BYTES = 131072

TOOLS = [
    {
        "name": "list_sms",
        "description": "Search and page through archived SMS messages.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "page": {"type": "integer", "minimum": 1},
                "page_size": {"type": "integer", "minimum": 1, "maximum": 100},
                "q": {"type": "string", "maxLength": 200},
                "read": {"type": "boolean"},
            },
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    },
    {
        "name": "get_sms",
        "description": "Get one archived SMS message by its ID.",
        "inputSchema": {"type": "object", "properties": {"id": {"type": "integer", "minimum": 1}}, "required": ["id"], "additionalProperties": False},
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    },
    {
        "name": "create_sms",
        "description": "Create an archive-only SMS record. This does not contact a modem or send a notification.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "sender": {"type": "string", "maxLength": 128},
                "text": {"type": "string", "maxLength": 65536},
                "received_at": {"type": "string", "format": "date-time"},
                "read": {"type": "boolean"},
            },
            "required": ["sender", "text", "received_at", "read"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False},
    },
    {
        "name": "update_sms",
        "description": "Update an archived SMS record. Content cannot change while parts are incomplete or device cleanup is pending.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "integer", "minimum": 1},
                "sender": {"type": "string", "maxLength": 128},
                "text": {"type": "string", "maxLength": 65536},
                "received_at": {"type": "string", "format": "date-time"},
                "read": {"type": "boolean"},
            },
            "required": ["id"],
            "minProperties": 2,
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    },
    {
        "name": "delete_sms",
        "description": "Delete an archived SMS record after device cleanup has completed.",
        "inputSchema": {"type": "object", "properties": {"id": {"type": "integer", "minimum": 1}}, "required": ["id"], "additionalProperties": False},
        "annotations": {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": False},
    },
]


def _same_origin(value, request):
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.username or parsed.password:
            return False
        if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
            return False
        origin = f"{parsed.scheme}://{parsed.netloc}".lower()
        current = f"{request.scheme}://{request.get_host()}".lower()
        if origin == current:
            return True
        for trusted_origin in settings.CSRF_TRUSTED_ORIGINS:
            trusted = urlsplit(trusted_origin)
            if trusted.scheme == parsed.scheme and is_same_domain(parsed.netloc, trusted.netloc.removeprefix("*")):
                return True
        return False
    except (ValueError, UnicodeError):
        return False


def _origin_denied(request):
    origin = request.headers.get("Origin")
    return bool(origin and not _same_origin(origin, request))


def _response(payload, status=200):
    return JsonResponse(payload, status=status, json_dumps_params={"ensure_ascii": False})


def _rpc_error(request_id, code, message, data=None):
    error = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


def _validate_rpc(value):
    if not isinstance(value, dict) or value.get("jsonrpc") != "2.0" or not isinstance(value.get("method"), str):
        raise ValueError
    if "id" in value and value["id"] is not None and type(value["id"]) not in (str, int, float):
        raise ValueError
    params = value.get("params", {})
    if not isinstance(params, dict):
        raise ValueError
    return params


def _tool_result(result, is_error=False):
    return {
        "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}],
        "structuredContent": result,
        "isError": is_error,
    }


def _call_tool(name, arguments):
    if not isinstance(arguments, dict):
        raise ValueError("arguments must be an object")
    allowed = {tool["name"] for tool in TOOLS}
    if name not in allowed:
        return _tool_result({"error": "Unknown tool."}, True)
    try:
        if name == "list_sms":
            unexpected = set(arguments) - {"page", "page_size", "q", "read"}
            if unexpected:
                raise ValueError("unsupported arguments")
            page = arguments.get("page", 1)
            page_size = arguments.get("page_size", 30)
            query = arguments.get("q", "")
            read = arguments.get("read")
            if type(page) is not int or page < 1 or type(page_size) is not int or not 1 <= page_size <= 100:
                raise ValueError("invalid pagination arguments")
            if not isinstance(query, str) or len(query) > 200 or (read is not None and type(read) is not bool):
                raise ValueError("invalid filters")
            result = list_messages(page=page, page_size=page_size, query=query, read=read)
        elif name == "get_sms":
            if set(arguments) != {"id"} or type(arguments.get("id")) is not int or arguments["id"] < 1:
                raise ValueError("id must be a positive integer")
            result = get_message(arguments["id"])
        elif name == "create_sms":
            result = create_message(arguments)
        elif name == "update_sms":
            if "id" not in arguments or type(arguments["id"]) is not int or arguments["id"] < 1:
                raise ValueError("id must be a positive integer")
            changes = {key: value for key, value in arguments.items() if key != "id"}
            result = update_message(arguments["id"], changes, partial=True)
        else:
            if set(arguments) != {"id"} or type(arguments.get("id")) is not int or arguments["id"] < 1:
                raise ValueError("id must be a positive integer")
            delete_message(arguments["id"])
            result = {"deleted": True, "id": arguments["id"]}
        return _tool_result(result)
    except ValueError as exc:
        return _tool_result({"error": str(exc)}, True)
    except ApiError as exc:
        return _tool_result({"error": exc.message, "status": exc.status}, True)


def _is_rpc_response(message):
    if message.get("jsonrpc") != "2.0" or "id" not in message:
        return False
    has_result = "result" in message and "error" not in message
    error = message.get("error")
    has_error = (
        isinstance(error, dict)
        and type(error.get("code")) is int
        and isinstance(error.get("message"), str)
        and "result" not in message
    )
    return has_result or has_error


def _dispatch(message):
    try:
        params = _validate_rpc(message)
    except ValueError:
        request_id = message.get("id") if isinstance(message, dict) else None
        if type(request_id) not in (str, int, float):
            request_id = None
        return _rpc_error(request_id, -32600, "Invalid Request")
    request_id = message.get("id")
    method = message["method"]
    if "id" not in message:
        return None
    if method == "initialize":
        requested = params.get("protocolVersion")
        if not isinstance(requested, str):
            return _rpc_error(request_id, -32602, "Invalid params")
        negotiated = requested if requested in SUPPORTED_VERSIONS else SUPPORTED_VERSIONS[0]
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "protocolVersion": negotiated,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "sms-archive", "version": "1.0.0"},
            },
        }
    if method == "ping":
        return {"jsonrpc": "2.0", "id": request_id, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": TOOLS}}
    if method == "tools/call":
        name, arguments = params.get("name"), params.get("arguments", {})
        if not isinstance(name, str) or not isinstance(arguments, dict):
            return _rpc_error(request_id, -32602, "Invalid params")
        return {"jsonrpc": "2.0", "id": request_id, "result": _call_tool(name, arguments)}
    if method.startswith("notifications/"):
        return None
    return _rpc_error(request_id, -32601, "Method not found")


@csrf_exempt
def endpoint(request):
    denied = authenticate_request(request, "mcp")
    if denied:
        return denied
    if _origin_denied(request):
        return _response({"jsonrpc": "2.0", "id": None, "error": {"code": -32000, "message": "Invalid Origin"}}, 403)
    if request.method != "POST":
        response = HttpResponse(status=405)
        response["Allow"] = "POST"
        return response
    if request.content_type != "application/json":
        return HttpResponse(status=415)
    if request.headers.get("MCP-Protocol-Version") not in (None, *SUPPORTED_VERSIONS):
        return HttpResponse(status=400)
    if len(request.body) > MAX_BODY_BYTES:
        return HttpResponse(status=413)
    try:
        message = load_json(request.body)
    except (UnicodeError, ValueError, RecursionError):
        return _response(_rpc_error(None, -32700, "Parse error"), 400)
    if isinstance(message, list):
        return _response(_rpc_error(None, -32600, "Batch requests are not supported"), 400)
    if not isinstance(message, dict):
        return _response(_rpc_error(None, -32600, "Invalid Request"), 400)
    if _is_rpc_response(message):
        return HttpResponse(status=202)
    result = _dispatch(message)
    if result is None:
        return HttpResponse(status=202)
    return _response(result)
