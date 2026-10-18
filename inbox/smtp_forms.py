from django import forms
from django.core.validators import validate_email
from django.core.exceptions import ValidationError


class StrictCheckboxInput(forms.CheckboxInput):
    def value_from_datadict(self, data, files, name):
        return data.get(name, False)


class StrictBooleanField(forms.Field):
    widget = StrictCheckboxInput

    default_error_messages = {"invalid": "Enter true, false, yes, no, on, or off."}

    def to_python(self, value):
        if value is None or value is False:
            return False
        if value is True:
            return True
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"1", "true", "yes", "on"}:
                return True
            if normalized in {"0", "false", "no", "off"}:
                return False
        raise ValidationError(self.error_messages["invalid"], code="invalid")


class SMTPSettingsForm(forms.Form):
    enabled = StrictBooleanField(required=False, label="Enable email forwarding")
    host = forms.CharField(required=False, max_length=253, label="SMTP host")
    port = forms.IntegerField(required=True, min_value=1, max_value=65535, label="Port")
    username = forms.CharField(
        required=False, max_length=254, label="Username",
        widget=forms.TextInput(attrs={"autocomplete": "off"}),
    )
    password = forms.CharField(
        required=False,
        strip=False,
        widget=forms.PasswordInput(render_value=False, attrs={"autocomplete": "new-password"}),
        label="Password",
    )
    security = forms.ChoiceField(
        required=True,
        choices=(("starttls", "STARTTLS"), ("tls", "SSL/TLS"), ("none", "None")),
        label="Connection security",
    )
    from_email = forms.CharField(required=False, max_length=254, label="From address")
    recipients = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
        label="Recipients",
    )
    password_action = forms.ChoiceField(
        required=False,
        choices=(("keep", "Keep"), ("replace", "Replace"), ("remove", "Remove")),
        widget=forms.HiddenInput,
    )

    def __init__(self, *args, stored_password="", test_email=False, **kwargs):
        self.test_email = test_email
        self.stored_password = stored_password or ""
        super().__init__(*args, **kwargs)
        for name, description in {
            "host": "host-help", "port": "port-help", "security": "security-help",
            "username": "username-help", "password": "password-help",
            "from_email": "from-help", "recipients": "recipients-help",
        }.items():
            self.fields[name].widget.attrs["aria-describedby"] = description
        if self.stored_password:
            self.fields["password"].label = "New password"

    def clean_host(self):
        raw = self.cleaned_data.get("host", "")
        if any(character.isspace() for character in raw):
            raise ValidationError("Enter a valid SMTP host.")
        value = raw
        return value

    def clean_username(self):
        raw = self.cleaned_data.get("username", "")
        if "\r" in raw or "\n" in raw:
            raise ValidationError("Enter a valid SMTP username.")
        value = raw.strip()
        if "\r" in value or "\n" in value:
            raise ValidationError("Enter a valid SMTP username.")
        return value

    def clean_from_email(self):
        raw = self.cleaned_data.get("from_email", "")
        if "\r" in raw or "\n" in raw:
            raise ValidationError("Enter a valid email address.")
        value = raw.strip()
        if "\r" in value or "\n" in value:
            raise ValidationError("Enter a valid email address.")
        if value:
            try:
                validate_email(value)
            except ValidationError:
                raise ValidationError("Enter a valid email address.") from None
        return value

    def clean_recipients(self):
        raw = self.cleaned_data.get("recipients", "")
        addresses = [address.strip() for address in raw.split(",") if address.strip()]
        unique_addresses = []
        seen = set()
        for address in addresses:
            try:
                validate_email(address)
            except ValidationError:
                raise ValidationError("Enter comma-separated email addresses.") from None
            key = address.casefold()
            if key not in seen:
                seen.add(key)
                unique_addresses.append(address)
        return ", ".join(unique_addresses)

    def clean(self):
        cleaned = super().clean()
        enabled = cleaned.get("enabled", False)
        username = cleaned.get("username", "")
        password = cleaned.get("password", "")
        if enabled or self.test_email:
            for key, label in (("host", "SMTP host"), ("from_email", "From address"), ("recipients", "At least one recipient")):
                if not cleaned.get(key):
                    reason = "to send a test email" if self.test_email else "when email forwarding is enabled"
                    self.add_error(key, f"{label} is required {reason}.")
        action = cleaned.pop("password_action", "keep")
        if action == "remove":
            username = cleaned["username"] = ""
            effective_password = ""
        else:
            effective_password = password or self.stored_password
            if action == "replace" and not password:
                self.add_error("password", "Enter the new password, or cancel the replacement.")
        if (enabled or self.test_email) and (username or effective_password) and bool(username) != bool(effective_password):
            self.add_error("username", "Enter both a username and password, or leave both blank.")
            self.add_error("password", "Enter both a username and password, or leave both blank.")
        cleaned["password"] = effective_password
        return cleaned
