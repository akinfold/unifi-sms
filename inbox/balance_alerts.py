from django.core.mail import EmailMessage
from django.core.mail.backends.smtp import EmailBackend
from django.db import transaction
from modem.outbound import OutboundError, OutboundSimChanged, OutboundUnknown

from .models import BalanceMonitor
from .outbound import active_profile
from .smtp_config import smtp_values
from .smtp_forms import SMTPSettingsForm

ALERT_UNKNOWN = "Submission unconfirmed"
ALERT_FAILED = "Submission failed"


def smtp_config(recipient):
    values, _ = smtp_values()
    form = SMTPSettingsForm(
        data={**values, "enabled": False, "recipients": recipient},
        stored_password=values["password"],
        test_email=True,
    )
    return form.cleaned_data if form.is_valid() else None


def _send_email(address, body):
    config = smtp_config(address)
    if config is None:
        return "SMTP settings unavailable"
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
        count = EmailMessage(
            subject="UniFi SMS: SIM balance below threshold",
            body=body,
            from_email=config["from_email"],
            to=[address],
            connection=connection,
        ).send()
    return "Accepted by the mail server" if count == 1 else ALERT_FAILED


def _send_sms(monitor, body, config, client_factory):
    outbound = active_profile(monitor.connection_id)
    if not outbound or outbound.sim != monitor.sim or not config or not config.configured:
        return "Verified SMS profile unavailable"
    with client_factory(**config.client_kwargs) as client:
        client.send_sms(outbound.profile, monitor.phone, body, expected_sim=monitor.sim)
    return "Accepted by the modem"


@transaction.atomic
def _claim(identity, channel):
    monitor = BalanceMonitor.objects.filter(
        pk=1, enabled=True, low=True, connection_id=identity
    ).first()
    if monitor is None:
        return
    state = monitor.alerts.get(channel)
    if state == "sending":
        monitor.alerts[channel] = ALERT_UNKNOWN
        monitor.save(update_fields=["alerts"])
    if state == "pending":
        monitor.alerts[channel] = "sending"
        monitor.save(update_fields=["alerts"])
        return monitor


@transaction.atomic
def _finish(monitor, channel, result):
    latest = BalanceMonitor.objects.filter(
        pk=monitor.pk, revision=monitor.revision, enabled=True, low=True
    ).first()
    if latest and latest.alerts.get(channel) == "sending":
        latest.alerts[channel] = result
        latest.save(update_fields=["alerts"])


def deliver_alerts(identity, config, client_factory):
    for channel in ("email", "sms"):
        monitor = _claim(identity, channel)
        if not monitor:
            continue
        amount = format(monitor.amount.normalize(), "f")
        currency = monitor.profile["currency"]
        body = f"UniFi SMS: SIM balance is {amount} {currency}, at or below the alert threshold."
        try:
            result = (
                _send_email(monitor.email, body) if channel == "email"
                else _send_sms(monitor, body, config, client_factory)
            )
        except OutboundSimChanged:
            result = "The SIM changed. Run balance setup again."
        except OutboundError:
            result = ALERT_FAILED
        except OutboundUnknown:
            result = ALERT_UNKNOWN
        except Exception:
            result = ALERT_UNKNOWN
        _finish(monitor, channel, result)
