import ipaddress
import re

from django import forms

from .connection import validate_private_key


class SecretTextarea(forms.Textarea):
    def format_value(self, value):
        return ""


class AddressForm(forms.Form):
    host = forms.CharField(label="Device address", max_length=253,
                          widget=forms.TextInput(attrs={"placeholder": "IP address or hostname", "autocomplete": "off"}))

    def clean_host(self):
        host = self.cleaned_data["host"]
        try:
            ipaddress.ip_address(host)
        except ValueError:
            if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.?", host):
                raise forms.ValidationError("Enter an IP address or hostname without a scheme or path.")
            if any(not label or len(label) > 63 or label.startswith("-") or label.endswith("-")
                   for label in host.rstrip(".").split(".")):
                raise forms.ValidationError("Enter a valid hostname.")
        return host


class SSHAccessForm(forms.Form):
    port = forms.IntegerField(label="SSH port", min_value=1, max_value=65535, initial=22)
    username = forms.CharField(label="SSH username", max_length=128,
                              widget=forms.TextInput(attrs={"autocomplete": "off"}))
    auth_method = forms.ChoiceField(label="Authentication", choices=[
        ("password", "Password"), ("private_key", "SSH key"),
    ])
    key_source = forms.ChoiceField(label="SSH key", required=False, choices=[
        ("paste", "Use my private key"), ("generate", "Generate a key in this app"),
    ])
    password = forms.CharField(label="SSH password", required=False, max_length=4096, strip=False,
                               widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}))
    private_key = forms.CharField(label="Private key", required=False, max_length=65536,
                                  widget=SecretTextarea(attrs={"rows": 7, "spellcheck": "false", "autocomplete": "off"}))

    def __init__(self, *args, stored_password="", stored_private_key="", **kwargs):
        super().__init__(*args, **kwargs)
        self.stored_password = stored_password
        self.stored_private_key = stored_private_key

    def clean_username(self):
        value = self.cleaned_data["username"]
        if any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value):
            raise forms.ValidationError("The SSH username cannot contain spaces or control characters.")
        return value

    def clean(self):
        data = super().clean()
        method = data.get("auth_method")
        data["password"] = (data.get("password") or self.stored_password) if method == "password" else ""
        source = data.get("key_source") or "paste"
        data["key_source"] = source
        data["private_key"] = (data.get("private_key") or self.stored_private_key) if method == "private_key" else ""
        if method == "password" and not data["password"]:
            self.add_error("password", "Enter the device SSH password.")
        if method == "private_key" and source == "generate":
            data["private_key"] = self.stored_private_key
            if not data["private_key"]:
                self.add_error("key_source", "Select Generate SSH key before continuing.")
        if method == "private_key" and source == "paste":
            if not data["private_key"]:
                self.add_error("private_key", "Paste your private key or choose Generate a key.")
            else:
                try:
                    validate_private_key(data["private_key"])
                except ValueError:
                    self.add_error("private_key", "Enter a valid, unencrypted SSH private key.")
        return data
