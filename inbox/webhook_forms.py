import re
import unicodedata
from urllib.parse import urlsplit

from django import forms
from django.core.exceptions import ValidationError

from .smtp_forms import StrictBooleanField


_HEADER_NAME_RE = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$", re.ASCII)
_RESERVED_HEADERS = {
    "accept",
    "connection",
    "content-length",
    "content-type",
    "host",
    "idempotency-key",
    "expect",
    "keep-alive",
    "proxy-authorization",
    "proxy-connection",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


class WebhookSettingsForm(forms.Form):
    enabled = StrictBooleanField(required=False, label="Enable webhook delivery")
    url = forms.CharField(required=False, max_length=2048, label="Webhook URL")
    secret = forms.CharField(
        required=False,
        strip=False,
        max_length=2048,
        widget=forms.PasswordInput(render_value=False, attrs={"autocomplete": "new-password"}),
        label="Secret",
    )
    secret_header = forms.CharField(
        required=False, max_length=128, label="Secret header",
        widget=forms.TextInput(attrs={"autocomplete": "off"}),
    )
    secret_action = forms.ChoiceField(
        required=False,
        choices=(("keep", "Keep saved secret"), ("replace", "Replace secret"), ("remove", "Remove secret")),
        initial="keep",
        label="Secret action",
    )

    def __init__(self, *args, stored_secret="", **kwargs):
        self.stored_secret = stored_secret or ""
        super().__init__(*args, **kwargs)
        if self.stored_secret:
            self.fields["secret"].label = "New secret"

    def clean_url(self):
        value = self.cleaned_data.get("url", "").strip()
        if not value:
            return ""
        try:
            parsed = urlsplit(value)
            # Accessing .port also validates malformed ports and brackets.
            port = parsed.port
        except ValueError:
            raise ValidationError("Enter a valid HTTP or HTTPS URL.") from None
        if (
            parsed.scheme.lower() not in {"http", "https"}
            or not parsed.netloc
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or "#" in value
            or port is not None and not 1 <= port <= 65535
        ):
            raise ValidationError("Enter an HTTP or HTTPS URL without credentials or a fragment.")
        if any(unicodedata.category(character) == "Cc" for character in value):
            raise ValidationError("Enter a valid HTTP or HTTPS URL.")
        return value

    def clean_secret(self):
        value = self.cleaned_data.get("secret", "")
        if any(unicodedata.category(character) == "Cc" for character in value):
            raise ValidationError("The secret cannot contain control characters.")
        try:
            value.encode("latin-1")
        except UnicodeEncodeError:
            raise ValidationError("The secret must be encodable as an HTTP header value.") from None
        return value

    def clean_secret_header(self):
        value = self.cleaned_data.get("secret_header", "").strip()
        if value and (
            not value.isascii()
            or not _HEADER_NAME_RE.fullmatch(value)
            or value.lower() in _RESERVED_HEADERS
        ):
            raise ValidationError("Enter a valid, non-reserved HTTP header name.")
        return value

    def clean(self):
        cleaned = super().clean()
        enabled = cleaned.get("enabled", False)
        if enabled and not cleaned.get("url"):
            self.add_error("url", "A webhook URL is required when delivery is enabled.")

        action = cleaned.get("secret_action") or "keep"
        submitted_secret = cleaned.get("secret", "")
        if action == "remove":
            effective_secret = ""
        else:
            effective_secret = submitted_secret or self.stored_secret
            if action == "replace" and not submitted_secret:
                self.add_error("secret", "Enter the new secret, or cancel the replacement.")
        if enabled and effective_secret and not cleaned.get("secret_header"):
            self.add_error("secret_header", "A secret header is required when a secret is set.")
        cleaned["secret"] = effective_secret
        return cleaned
