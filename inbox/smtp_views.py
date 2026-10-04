from django.contrib import messages
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_http_methods

from .models import ForwardDelivery, SMTPSettings
from .smtp_config import smtp_enabled, smtp_values
from .smtp_forms import SMTPSettingsForm
from .views import initialized


ENV_NOTICE = (
    "SMTP settings are managed by environment variables. Remove all SMTP_* settings from the "
    "container environment and recreate the container to edit them here."
)


@require_http_methods(["GET", "POST"])
def smtp_settings(request):
    if not initialized():
        return redirect("setup")
    if (
        not request.user.is_authenticated
        or not request.user.is_active
        or not request.user.is_staff
    ):
        return redirect("login")

    values, env_keys = smtp_values()
    env_managed = bool(env_keys)
    if request.method == "POST" and env_managed:
        return HttpResponse("SMTP settings are managed by the environment.", status=403)

    stored_password = values.get("password", "")
    initial = {key: value for key, value in values.items() if key != "password"}
    config_form = None
    if env_managed:
        validation_form = SMTPSettingsForm(data=values, stored_password="")
        validation_form.is_valid()
        config_form = SMTPSettingsForm(
            initial=initial, stored_password="", env_managed=True
        )
        config_form._errors = validation_form.errors
    if request.method == "POST":
        form = SMTPSettingsForm(request.POST, stored_password=stored_password)
        if form.is_valid():
            data = form.cleaned_data
            SMTPSettings.objects.update_or_create(
                pk=1,
                defaults={
                    "enabled": data["enabled"],
                    "host": data["host"],
                    "port": data["port"] or 587,
                    "username": data["username"],
                    "password": data["password"],
                    "security": data["security"],
                    "from_email": data["from_email"],
                    "recipients": data["recipients"],
                },
            )
            messages.success(request, "SMTP settings saved.")
            return redirect("smtp-settings")
    else:
        form = SMTPSettingsForm(
            initial=initial,
            stored_password=stored_password,
            env_managed=env_managed,
        )

    password_action = request.POST.get("password_action", "keep")
    if password_action != "remove" and request.POST.get("password"):
        password_action = "replace"
    if password_action not in {"keep", "replace", "remove"}:
        password_action = "keep"

    delivery_summary = {
        "pending": ForwardDelivery.objects.filter(sent_at__isnull=True).count(),
        "last_error": ForwardDelivery.objects.filter(sent_at__isnull=True).exclude(last_error="")
        .order_by("-created_at")
        .values_list("last_error", flat=True)
        .first(),
        "last_sent": ForwardDelivery.objects.filter(sent_at__isnull=False)
        .order_by("-sent_at")
        .values_list("sent_at", flat=True)
        .first(),
    }
    return render(
        request,
        "inbox/smtp_settings.html",
        {
            "form": config_form or form,
            "enabled_checked": (
                smtp_enabled({"enabled": request.POST.get("enabled", "")})
                if request.method == "POST"
                else str(values["enabled"]).strip().lower() in {"1", "true", "yes", "on"}
            ),
            "has_password": bool(stored_password),
            "has_credentials": bool(stored_password or values.get("username")),
            "password_action": password_action,
            "password_reentry": bool(request.POST.get("password")),
            "env_managed": env_managed,
            "env_keys": env_keys,
            "env_notice": ENV_NOTICE,
            "delivery_summary": delivery_summary,
        },
    )
