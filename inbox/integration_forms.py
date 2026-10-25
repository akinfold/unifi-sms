from django import forms

from .smtp_forms import StrictBooleanField


class IntegrationSettingsForm(forms.Form):
    rest_enabled = StrictBooleanField(required=False, label="Enable REST API")
    mcp_enabled = StrictBooleanField(required=False, label="Enable MCP")
    rest_key = forms.CharField(
        required=False, strip=False, max_length=256,
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
        label="New REST API key",
    )
    mcp_key = forms.CharField(
        required=False, strip=False, max_length=256,
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
        label="New MCP key",
    )
    rest_key_action = forms.ChoiceField(
        required=False,
        choices=[("keep", "Keep saved key"), ("replace", "Replace key"), ("revoke", "Revoke key")],
    )
    mcp_key_action = forms.ChoiceField(
        required=False,
        choices=[("keep", "Keep saved key"), ("replace", "Replace key"), ("revoke", "Revoke key")],
    )

    def __init__(self, *args, stored, **kwargs):
        self.stored = stored
        super().__init__(*args, **kwargs)

    def clean(self):
        data = super().clean()
        for name in ("rest", "mcp"):
            key = data.get(f"{name}_key", "")
            action = data.get(f"{name}_key_action") or "keep"
            if key and action == "revoke":
                self.add_error(f"{name}_key", "Clear the new key before revoking the saved key.")
            elif key:
                action = "replace"
            if action == "replace" and not key:
                self.add_error(f"{name}_key", "Enter or generate a new key.")
            if key and (len(key) < 32 or any(ord(c) < 33 or ord(c) > 126 for c in key)):
                self.add_error(
                    f"{name}_key", "Use 32 to 256 ASCII characters without spaces. Generate a random key for best protection."
                )
            has_key = bool(key) if action == "replace" else (
                action != "revoke" and bool(getattr(self.stored, f"{name}_key_hash"))
            )
            if data.get(f"{name}_enabled") and not has_key:
                self.add_error(f"{name}_key", "Set a key before enabling access, or disable access to revoke it.")
            data[f"{name}_key_action"] = action
        return data
