import fcntl
import hashlib
from contextlib import contextmanager
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from modem.client import ModemClient
from modem.pdu import decode_pdu
from .connection import get_connection
from .models import Message, Receipt, SyncStatus
from .device import refresh_phone
from .views import initialized
from .forwarding import queue_forward
from .webhooks import queue_webhook


def fingerprint(source, pdu):
    return hashlib.sha256((source + ":" + pdu.upper()).encode("ascii")).hexdigest()


@contextmanager
def importer_lock():
    with (settings.DATA_DIR / "importer.lock").open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def archive(source, slot, pdu):
    digest = fingerprint(source, pdu)
    # A durable transaction must finish before any modem deletion can start.
    with transaction.atomic(durable=True):
        receipt = Receipt.objects.filter(fingerprint=digest).first()
        if receipt:
            receipt.cleared_at = None
            receipt.slot = slot
            receipt.save(update_fields=["cleared_at", "slot"])
            return receipt
        decoded = decode_pdu(pdu)
        message = None
        if decoded.concat_ref is not None:
            candidates = Message.objects.filter(
                source=source,
                sender=decoded.sender,
                concat_ref=decoded.concat_ref,
                total_parts=decoded.total,
                received_at__gte=decoded.received_at - timedelta(hours=24),
                received_at__lte=decoded.received_at + timedelta(hours=24),
            ).order_by("-received_at")
            for candidate in candidates:
                if (
                    candidate.saved_parts < candidate.total_parts
                    and not candidate.receipts.filter(part=decoded.part).exists()
                ):
                    message = candidate
                    break
        if message is None:
            message = Message.objects.create(
                source=source,
                sender=decoded.sender,
                received_at=decoded.received_at,
                text="",
                concat_ref=decoded.concat_ref,
                total_parts=decoded.total,
                saved_parts=0,
            )
        receipt = Receipt.objects.create(
            fingerprint=digest,
            source=source,
            message=message,
            pdu=pdu.upper(),
            part=decoded.part,
            slot=slot,
        )
        parts = {r.part: decode_pdu(r.pdu).text for r in message.receipts.all()}
        message.saved_parts = len(parts)
        message.text = "".join(
            parts.get(i, f"[Missing part {i}]") for i in range(1, message.total_parts + 1)
        )
        message.received_at = min(message.received_at, decoded.received_at)
        message.read_at = None
        message.save()
        queue_forward(message)
        queue_webhook(message)
        return receipt


def refresh_phone_once(client_factory=ModemClient):
    if not initialized():
        return
    with importer_lock() as acquired:
        if acquired:
            try:
                config = get_connection()
            except ValueError:
                return
            if config.configured:
                refresh_phone(client_factory, config)


def sync_once(client_factory=ModemClient):
    with importer_lock() as acquired:
        if not acquired:
            return
        state, _ = SyncStatus.objects.get_or_create(pk=1)
        state.attempted_at = timezone.now()
        try:
            config = get_connection()
        except ValueError:
            state.state = "error"
            state.detail = "Device connection settings are invalid."
            state.save()
            return
        if not initialized() or not config.configured:
            state.state = "waiting"
            state.detail = (
                "Configure the LTE SSH connection."
                if initialized()
                else "Complete administrator setup."
            )
            state.save()
            return
        state.state, state.detail = "syncing", "Reading the modem."
        state.save()
        source = hashlib.sha256(
            f"{config.host}:{config.port}:{config.device}:{config.storage}".encode()
        ).hexdigest()
        rejected = 0
        try:
            with client_factory(**config.client_kwargs) as client:
                pdus = client.list_pdus()
                present = [fingerprint(source, pdu) for pdu in pdus.values()]
                Receipt.objects.filter(source=source, cleared_at__isnull=True).exclude(
                    fingerprint__in=present
                ).update(cleared_at=timezone.now())
                for slot, pdu in pdus.items():
                    try:
                        receipt = archive(source, slot, pdu)
                    except ValueError:
                        rejected += 1
                        continue
                    if client.read_pdu(slot) != pdu:
                        rejected += 1
                        continue
                    client.delete(slot)
                    Receipt.objects.filter(pk=receipt.pk).update(cleared_at=timezone.now())
            state.state = "warning" if rejected else "ok"
            state.detail = (
                f"{rejected} slot(s) retained because decoding or verification failed."
                if rejected
                else "Import complete. Archived slots have been cleared."
            )
            state.succeeded_at = timezone.now()
        except Exception:
            # Transport exceptions can contain modem output or credentials.
            state.state, state.detail = (
                "error",
                "Import failed. Check SSH access, host keys, modem availability, and disk space. Saved messages will be retried.",
            )
        state.save()
