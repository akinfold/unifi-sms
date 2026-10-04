import hashlib
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from modem.client import ModemClient

from .models import DeviceInfo


REFRESH_INTERVAL = timedelta(hours=1)
RECOVERY_INTERVAL = timedelta(minutes=2)


def device_source():
    return hashlib.sha256(
        f"{settings.LTE_HOST}:{settings.LTE_PORT}:{settings.LTE_DEVICE}".encode()
    ).hexdigest()


def configured():
    return bool(settings.LTE_HOST and settings.LTE_USERNAME)


def refreshing(info):
    return bool(
        info.attempted_at and (not info.completed_at or info.attempted_at > info.completed_at)
    )


def phone_status():
    if not configured():
        return {"state": "unconfigured", "numbers": [], "checked_at": None, "error": ""}
    info = DeviceInfo.objects.filter(pk=device_source()).first()
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


def request_phone_refresh():
    with transaction.atomic():
        info, _ = DeviceInfo.objects.get_or_create(pk=device_source())
        now = timezone.now()
        if not refreshing(info) or now - info.attempted_at >= RECOVERY_INTERVAL:
            info.requested_at = now
            info.save(update_fields=["requested_at"])


def refresh_phone(client_factory=ModemClient):
    info, _ = DeviceInfo.objects.get_or_create(pk=device_source())
    now = timezone.now()
    requested = info.requested_at and (
        not info.attempted_at or info.requested_at > info.attempted_at
    )
    interrupted = refreshing(info) and now - info.attempted_at >= RECOVERY_INTERVAL
    if info.attempted_at and not requested and not interrupted:
        if now - info.attempted_at < REFRESH_INTERVAL:
            return
    info.attempted_at = now
    info.save(update_fields=["attempted_at"])
    try:
        with client_factory(
            host=settings.LTE_HOST,
            port=settings.LTE_PORT,
            username=settings.LTE_USERNAME,
            key_filename=settings.LTE_KEY_FILE,
            known_hosts=settings.LTE_KNOWN_HOSTS,
            device=settings.LTE_DEVICE,
            storage=settings.LTE_STORAGE,
        ) as client:
            numbers = client.phone_numbers()
    except Exception:
        # Keep transport output and credentials out of the API response.
        DeviceInfo.objects.filter(pk=info.pk).update(
            completed_at=timezone.now(),
            last_error="Could not read the SIM number. Check the device connection and try again.",
        )
    else:
        now = timezone.now()
        DeviceInfo.objects.filter(pk=info.pk).update(
            numbers=numbers, checked_at=now, completed_at=now, last_error=""
        )
