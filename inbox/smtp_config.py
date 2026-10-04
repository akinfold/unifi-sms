import os

from .models import SMTPSettings

SMTP_ENV_FIELDS = {
    "SMTP_ENABLED": "enabled",
    "SMTP_HOST": "host",
    "SMTP_PORT": "port",
    "SMTP_USERNAME": "username",
    "SMTP_PASSWORD": "password",
    "SMTP_SECURITY": "security",
    "SMTP_FROM": "from_email",
    "SMTP_TO": "recipients",
}


def smtp_values():
    env_keys = tuple(key for key in SMTP_ENV_FIELDS if key in os.environ)
    stored = SMTPSettings() if env_keys else SMTPSettings.objects.filter(pk=1).first()
    stored = stored or SMTPSettings()
    values = {field: getattr(stored, field) for field in SMTP_ENV_FIELDS.values()}
    for key in env_keys:
        values[SMTP_ENV_FIELDS[key]] = os.environ[key]
    return values, env_keys


def smtp_enabled(values):
    return str(values["enabled"]).strip().lower() in {"1", "true", "yes", "on"}
