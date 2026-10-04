from django import forms
from django.contrib.auth.forms import AuthenticationForm, UserCreationForm
from django.contrib.auth.models import User


class AdminAuthenticationForm(AuthenticationForm):
    def confirm_login_allowed(self, user):
        super().confirm_login_allowed(user)
        if not user.is_staff:
            raise forms.ValidationError("Administrator access is required.", code="inactive")


class SetupForm(UserCreationForm):
    class Meta:
        model = User
        fields = ["username", "password1", "password2"]
