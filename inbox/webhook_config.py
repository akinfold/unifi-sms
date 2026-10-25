from .connection import decrypt_secret, is_encrypted_secret
from .models import WebhookSettings


WEBHOOK_FIELDS = ("enabled", "url", "secret", "secret_header")


def _stored_values():
    stored = WebhookSettings.objects.filter(pk=1).first() or WebhookSettings()
    values = {field: getattr(stored, field) for field in WEBHOOK_FIELDS}
    secret = values["secret"]
    if secret:
        if not is_encrypted_secret(secret):
            raise ValueError("Stored webhook secret is invalid")
        values["secret"] = decrypt_secret(secret)
    return values


def webhook_values():
    """Return the saved webhook settings with the secret decrypted for delivery."""
    return _stored_values()


def webhook_active():
    from .webhook_forms import WebhookSettingsForm

    try:
        values = _stored_values()
    except (TypeError, ValueError):
        return False
    return bool(
        values["enabled"]
        and WebhookSettingsForm(data=values, stored_secret=values["secret"]).is_valid()
    )
