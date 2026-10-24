from .models import SMTPSettings

SMTP_FIELDS = ("enabled", "host", "port", "username", "password", "security", "from_email", "recipients")


def smtp_values():
    stored = SMTPSettings.objects.filter(pk=1).first() or SMTPSettings()
    return {field: getattr(stored, field) for field in SMTP_FIELDS}, ()


def smtp_enabled(values):
    return str(values["enabled"]).strip().lower() in {"1", "true", "yes", "on"}


def smtp_forwarding_active():
    from .smtp_forms import SMTPSettingsForm

    values, _ = smtp_values()
    return smtp_enabled(values) and SMTPSettingsForm(
        data=values, stored_password=values["password"]
    ).is_valid()
