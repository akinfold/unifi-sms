import fcntl
import json
import ssl
import subprocess
import sys
import time
import urllib.error
import uuid
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.utils import timezone

from .models import WebhookDelivery
from .webhook_config import webhook_active, webhook_values


@contextmanager
def webhook_lock():
    with (settings.DATA_DIR / "webhook.lock").open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def queue_webhook(message):
    if message.saved_parts != message.total_parts or not webhook_active():
        return None

    event_id = uuid.uuid4()
    payload = {
        "event": "sms.received",
        "event_id": str(event_id),
        "sms": {
            "id": message.pk,
            "source": message.source,
            "sender": message.sender,
            "text": message.text,
            "received_at": message.received_at.isoformat(),
            "imported_at": message.imported_at.isoformat(),
        },
    }
    delivery, _created = WebhookDelivery.objects.get_or_create(
        message=message,
        defaults={"token": event_id, "payload": payload},
    )
    return delivery


_POST_TIMEOUT = 10


def _retry_time(attempts):
    return timezone.now() + timedelta(seconds=min(3600, 60 * 2 ** min(attempts - 1, 6)))


def _post(url, payload, secret, secret_header, token):
    deadline = time.monotonic() + _POST_TIMEOUT
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "Idempotency-Key": str(token),
    }
    if secret:
        headers[secret_header] = secret
    values = {
        "url": url,
        "body": json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        "headers": headers,
        "timeout": _POST_TIMEOUT,
    }
    # A socket timeout cannot bound DNS or a peer that keeps sending header bytes.
    # Process isolation lets the worker cancel all network operations and reap them.
    with subprocess.Popen(
        [sys.executable, "-I", str(Path(__file__).with_name("webhook_http.py"))],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    ) as process:
        try:
            output, _ = process.communicate(
                json.dumps(values).encode("utf-8"),
                timeout=max(0.001, deadline - time.monotonic()),
            )
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            raise TimeoutError("Webhook request timed out") from None
        if process.returncode:
            raise urllib.error.URLError("Request failed")
    result = json.loads(output)
    if result.get("error") == "timeout":
        raise TimeoutError("Webhook request timed out")
    if result.get("error") == "tls":
        raise ssl.SSLError("TLS verification failed")
    if result.get("error"):
        raise urllib.error.URLError("Request failed")
    if not 200 <= result["status"] < 300:
        raise _WebhookHTTPError(result["status"])


class _WebhookHTTPError(Exception):
    def __init__(self, status):
        self.status = status


def _safe_error(error):
    if isinstance(error, TimeoutError):
        return "Webhook request timed out. A retry is scheduled."
    if isinstance(error, _WebhookHTTPError):
        return f"Webhook returned HTTP {error.status}. A retry is scheduled."
    if isinstance(error, urllib.error.HTTPError):
        return f"Webhook returned HTTP {error.code}. A retry is scheduled."
    if isinstance(error, (TimeoutError, urllib.error.URLError)):
        reason = getattr(error, "reason", None)
        if (
            isinstance(reason, TimeoutError)
            or "timed out" in type(reason).__name__.lower()
            or isinstance(reason, str) and "timed out" in reason.lower()
        ):
            return "Webhook request timed out. A retry is scheduled."
        if isinstance(reason, ssl.SSLError) or isinstance(error, ssl.SSLError):
            return "Webhook TLS verification failed. A retry is scheduled."
        return "Webhook network request failed. A retry is scheduled."
    if isinstance(error, ssl.SSLError):
        return "Webhook TLS verification failed. A retry is scheduled."
    return "Webhook delivery failed. A retry is scheduled."


def deliver_webhooks(stop=None):
    with webhook_lock() as acquired:
        if not acquired:
            return
        if not webhook_active():
            return
        try:
            config = webhook_values()
        except (TypeError, ValueError):
            return
        due = list(
            WebhookDelivery.objects.filter(
                sent_at__isnull=True,
                next_attempt_at__lte=timezone.now(),
            )
            .order_by("id")
            .values_list("id", flat=True)[:10]
        )
        for delivery_id in due:
            if stop is not None and stop.is_set():
                return
            delivery = WebhookDelivery.objects.filter(pk=delivery_id).first()
            if delivery is None or delivery.sent_at is not None:
                continue

            delivery.attempts += 1
            # Persist the delay before HTTP so a crash cannot cause a tight retry loop.
            WebhookDelivery.objects.filter(pk=delivery.pk).update(
                attempts=delivery.attempts,
                next_attempt_at=_retry_time(delivery.attempts),
            )
            try:
                _post(
                    config["url"],
                    delivery.payload,
                    config["secret"],
                    config["secret_header"],
                    delivery.token,
                )
            except Exception as error:
                WebhookDelivery.objects.filter(pk=delivery.pk).update(
                    last_error=_safe_error(error)[:250]
                )
                continue
            WebhookDelivery.objects.filter(pk=delivery.pk).update(
                sent_at=timezone.now(), last_error=""
            )
