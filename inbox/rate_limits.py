import hashlib
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from .models import AccessWindow


def window_key(scope, *parts):
    value = "\0".join((scope, *parts))
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()


def _active(window, now, seconds):
    return window is not None and now - window.started_at < timedelta(seconds=seconds)


def exhausted(key, limit, seconds):
    window = AccessWindow.objects.filter(pk=key).first()
    return _active(window, timezone.now(), seconds) and window.attempts >= limit


def reserve(buckets, seconds):
    """Reserve one attempt in every bucket, or leave every bucket unchanged."""
    now = timezone.now()
    with transaction.atomic():
        windows = {item.pk: item for item in AccessWindow.objects.filter(pk__in=[key for key, _ in buckets])}
        if any(_active(windows.get(key), now, seconds) and windows[key].attempts >= limit for key, limit in buckets):
            return False
        for key, _ in buckets:
            window = windows.get(key)
            if not _active(window, now, seconds):
                window = AccessWindow(key=key, started_at=now)
            window.attempts += 1
            window.save()
        AccessWindow.objects.filter(started_at__lt=now - timedelta(days=1)).delete()
    return True
