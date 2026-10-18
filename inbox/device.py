import hashlib
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from modem.client import ModemClient

from .connection import get_connection
from .models import DeviceInfo


REFRESH_INTERVAL = timedelta(hours=1)
RECOVERY_INTERVAL = timedelta(minutes=2)


def device_source(config=None):
    config = config or get_connection()
    return hashlib.sha256(
        f"{config.host}:{config.port}:{config.device}".encode()
    ).hexdigest()


def configured(config=None):
    try:
        return (config or get_connection()).configured
    except ValueError:
        return False


def refreshing(info):
    return bool(
        info.attempted_at and (not info.completed_at or info.attempted_at > info.completed_at)
    )


def phone_status():
    try:
        config = get_connection()
    except ValueError:
        return {
            "state": "error", "numbers": [], "checked_at": None,
            "error": "Device connection settings are invalid.",
        }
    if not config.configured:
        return {"state": "unconfigured", "numbers": [], "checked_at": None, "error": ""}
    info = DeviceInfo.objects.filter(pk=device_source(config)).first()
    state = "waiting"
    error = ""
    if info:
        if refreshing(info):
            state = "refreshing"
            if timezone.now() - info.attempted_at >= RECOVERY_INTERVAL:
                state = "error"
                error = "The device check was interrupted. Try updating again."
        elif info.requested_at and (not info.attempted_at or info.requested_at > info.attempted_at):
            state = "queued"
            if timezone.now() - info.requested_at > timedelta(
                seconds=max(180, settings.POLL_INTERVAL * 3)
            ):
                state = "error"
                error = "The importer has not processed this request. Check that it is running."
        elif info.attempted_at:
            state = "error" if info.last_error else "ok" if info.numbers else "empty"
            error = info.last_error
    return {
        "state": state,
        "numbers": info.numbers if info else [],
        "checked_at": info.checked_at.isoformat() if info and info.checked_at else None,
        "error": error,
    }


def device_status():
    try:
        config = get_connection()
    except ValueError:
        return {"address": "", "model": "", "error": True}
    if not config.configured:
        return {"address": config.host, "model": "", "error": False}
    info = DeviceInfo.objects.filter(pk=device_source(config)).first()
    return {
        "address": config.host,
        "model": info.model if info else "",
        "error": info.model_error if info else False,
    }


def request_phone_refresh():
    config = get_connection()
    with transaction.atomic():
        info, _ = DeviceInfo.objects.get_or_create(pk=device_source(config))
        now = timezone.now()
        if not refreshing(info) or now - info.attempted_at >= RECOVERY_INTERVAL:
            info.requested_at = now
            info.save(update_fields=["requested_at"])


def refresh_phone(client_factory=ModemClient, config=None):
    config = config or get_connection()
    info, _ = DeviceInfo.objects.get_or_create(pk=device_source(config))
    now = timezone.now()
    requested = info.requested_at and (
        not info.attempted_at or info.requested_at > info.attempted_at
    )
    interrupted = refreshing(info) and now - info.attempted_at >= RECOVERY_INTERVAL
    if info.attempted_at and info.model_attempted_at and not requested and not interrupted:
        if now - info.attempted_at < REFRESH_INTERVAL:
            return
    info.attempted_at = now
    info.model_attempted_at = now
    info.save(update_fields=["attempted_at", "model_attempted_at"])
    try:
        with client_factory(**config.client_kwargs) as client:
            try:
                numbers = client.phone_numbers()
            except Exception:
                numbers = None
            try:
                model = client.device_model()
            except Exception:
                model = None
    except Exception:
        numbers = None
        model = None
    now = timezone.now()
    updates = {"completed_at": now, "model_error": model is None}
    if numbers is None:
        updates["last_error"] = "Could not read the SIM number. Check the device connection and try again."
    else:
        updates.update(numbers=numbers, checked_at=now, last_error="")
    if model is not None:
        updates["model"] = model
    DeviceInfo.objects.filter(pk=info.pk).update(**updates)
