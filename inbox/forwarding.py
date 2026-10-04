import fcntl
import hashlib
from contextlib import contextmanager
from datetime import timedelta

from django.conf import settings
from django.core.mail import EmailMessage
from django.core.mail.backends.smtp import EmailBackend
from django.utils import timezone

from .models import ForwardDelivery
from .smtp_config import smtp_enabled, smtp_values


@contextmanager
def forwarding_lock():
    with (settings.DATA_DIR / "smtp.lock").open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def queue_forward(message):
    if message.saved_parts == message.total_parts and smtp_enabled(smtp_values()[0]):
        ForwardDelivery.objects.get_or_create(message=message)


def forward_pending(stop=None):
    from .smtp_forms import SMTPSettingsForm

    with forwarding_lock() as acquired:
        if not acquired:
            return
        values, _ = smtp_values()
        if not smtp_enabled(values):
            return
        form = SMTPSettingsForm(data=values, stored_password=values["password"])
        if not form.is_valid():
            return
        config = form.cleaned_data
        due = (
            ForwardDelivery.objects.filter(
                sent_at__isnull=True, next_attempt_at__lte=timezone.now()
            )
            .order_by("id")
            .values_list("id", flat=True)[:10]
        )
        for delivery_id in list(due):
            if stop is not None and stop.is_set():
                return
            delivery = (
                ForwardDelivery.objects.select_related("message").filter(pk=delivery_id).first()
            )
            if delivery is None:
                continue
            if not delivery.recipients:
                delivery.recipients = [
                    address.strip() for address in config["recipients"].split(",")
                ]
                ForwardDelivery.objects.filter(pk=delivery.pk).update(
                    recipients=delivery.recipients
                )
            delivery.attempts += 1
            # Persist the retry delay before SMTP: a crash must not trigger a tight resend loop.
            retry_at = timezone.now() + timedelta(
                seconds=min(3600, 60 * 2 ** min(delivery.attempts - 1, 6))
            )
            ForwardDelivery.objects.filter(pk=delivery.pk).update(
                attempts=delivery.attempts,
                next_attempt_at=retry_at,
            )
            sms = delivery.message
            sender = " ".join(sms.sender.split())[:128]
            body = f"From: {sms.sender}\nReceived: {sms.received_at.isoformat()}\n\n{sms.text}"
            try:
                with EmailBackend(
                    host=config["host"],
                    port=config["port"],
                    username=config["username"],
                    password=config["password"],
                    use_tls=config["security"] == "starttls",
                    use_ssl=config["security"] == "tls",
                    timeout=10,
                    fail_silently=False,
                ) as connection:
                    for recipient in delivery.recipients:
                        if recipient in delivery.delivered_to:
                            continue
                        if stop is not None and stop.is_set():
                            return
                        recipient_id = hashlib.sha256(recipient.encode()).hexdigest()[:16]
                        email = EmailMessage(
                            subject=f"SMS from {sender}",
                            body=body,
                            from_email=config["from_email"],
                            to=[recipient],
                            connection=connection,
                            headers={
                                "Message-ID": f"<{delivery.token}.{recipient_id}@unifi-sms.local>"
                            },
                        )
                        if email.send() != 1:
                            raise RuntimeError("SMTP did not accept the message.")
                        delivery.delivered_to.append(recipient)
                        # Track each recipient separately so partial failures do not resend to others.
                        ForwardDelivery.objects.filter(pk=delivery.pk).update(
                            delivered_to=delivery.delivered_to,
                        )
                ForwardDelivery.objects.filter(pk=delivery.pk).update(
                    sent_at=timezone.now(), last_error=""
                )
            except Exception:
                # SMTP exceptions may include credentials, recipients, or message text.
                ForwardDelivery.objects.filter(pk=delivery.pk).update(
                    last_error="SMTP delivery failed. Check the server, credentials, and recipient addresses. A retry is scheduled."
                )
                return
