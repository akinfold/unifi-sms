from datetime import timezone as datetime_timezone

from django.core.paginator import EmptyPage, PageNotAnInteger, Paginator
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .models import Message, Receipt

MAX_TEXT_LENGTH = 65536
MAX_SENDER_LENGTH = 128
MUTABLE_FIELDS = {"sender", "text", "received_at", "read"}


class ApiError(Exception):
    def __init__(self, message, status=400):
        self.message = message
        self.status = status
        super().__init__(message)


def _validate_fields(data, *, partial=False):
    if not isinstance(data, dict):
        raise ApiError("Expected a JSON object.")
    allowed = MUTABLE_FIELDS
    unknown = set(data) - allowed
    if unknown:
        raise ApiError("The request contains unsupported fields.")
    required = set() if partial else MUTABLE_FIELDS
    if required - set(data):
        raise ApiError("The request is missing required fields.")
    result = {}
    if "sender" in data:
        value = data["sender"]
        if not isinstance(value, str) or len(value) > MAX_SENDER_LENGTH:
            raise ApiError("sender must be a string of at most 128 characters.")
        result["sender"] = value
    if "text" in data:
        value = data["text"]
        if not isinstance(value, str) or len(value) > MAX_TEXT_LENGTH:
            raise ApiError("text must be a string of at most 65536 characters.")
        result["text"] = value
    if "received_at" in data:
        value = data["received_at"]
        try:
            parsed = parse_datetime(value) if isinstance(value, str) else None
            if parsed is None or timezone.is_naive(parsed):
                raise ValueError
            parsed.astimezone(datetime_timezone.utc)
        except (ValueError, OverflowError):
            raise ApiError("received_at must be an ISO 8601 timestamp with an explicit UTC offset.") from None
        result["received_at"] = parsed
    if "read" in data:
        if type(data["read"]) is not bool:
            raise ApiError("read must be a boolean.")
        result["read"] = data["read"]
    return result


def serialize_message(message):
    pending_cleanup = message.receipts.filter(cleared_at__isnull=True).exists()
    return {
        "id": message.pk,
        "source": message.source,
        "sender": message.sender,
        "received_at": message.received_at.isoformat(),
        "imported_at": message.imported_at.isoformat(),
        "text": message.text,
        "read": message.read_at is not None,
        "saved_parts": message.saved_parts,
        "total_parts": message.total_parts,
        "pending_cleanup": pending_cleanup,
    }


def list_messages(*, page=1, page_size=30, query="", read=None):
    records = Message.objects.all()
    if query:
        records = records.filter(Q(sender__icontains=query) | Q(text__icontains=query))
    if read is not None:
        records = records.filter(read_at__isnull=not read)
    paginator = Paginator(records, page_size)
    try:
        page_obj = paginator.page(page)
    except (EmptyPage, PageNotAnInteger):
        raise ApiError("page is outside the available range.")
    return {
        "messages": [serialize_message(item) for item in page_obj.object_list],
        "page": page_obj.number,
        "page_size": page_size,
        "pages": paginator.num_pages,
        "count": paginator.count,
    }


def get_message(pk):
    try:
        return serialize_message(Message.objects.get(pk=pk))
    except Message.DoesNotExist:
        raise ApiError("Message not found.", 404)


def _record(pk):
    try:
        return Message.objects.get(pk=pk)
    except Message.DoesNotExist:
        raise ApiError("Message not found.", 404)


def _set_values(message, values):
    if "read" in values:
        message.read_at = timezone.now() if values["read"] else None
    for name in ("sender", "text", "received_at"):
        if name in values:
            setattr(message, name, values[name])


def create_message(data):
    values = _validate_fields(data)
    message = Message(
        source="api",
        sender=values["sender"],
        text=values["text"],
        received_at=values["received_at"],
        read_at=timezone.now() if values["read"] else None,
        total_parts=1,
        saved_parts=1,
    )
    message.save()
    return serialize_message(message)


def update_message(pk, data, *, partial=False):
    values = _validate_fields(data, partial=partial)
    if not values:
        raise ApiError("The request must include at least one supported field.")
    with transaction.atomic():
        message = _record(pk)
        changes_content = bool({"sender", "text", "received_at"} & set(values))
        incomplete = message.saved_parts < message.total_parts
        pending_cleanup = message.receipts.filter(cleared_at__isnull=True).exists()
        if changes_content and (incomplete or pending_cleanup):
            raise ApiError("Message content cannot change while parts are incomplete or device cleanup is pending.", 409)
        _set_values(message, values)
        message.save(update_fields=[*({"sender", "text", "received_at"} & set(values)), *( ["read_at"] if "read" in values else [])])
    return serialize_message(message)


def delete_message(pk):
    with transaction.atomic():
        message = _record(pk)
        receipts = Receipt.objects.filter(message_id=message.pk)
        if receipts.filter(cleared_at__isnull=True).exists():
            raise ApiError("Message cannot be deleted while device cleanup is pending.", 409)
        # Keep receipt fingerprints for deduplication after removing archived text.
        receipts.update(pdu="")
        message.delete()
    return True
