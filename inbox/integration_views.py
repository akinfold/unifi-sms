import secrets

from django.contrib import messages
from django.contrib.auth.hashers import make_password
from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_POST, require_http_methods

from .integration_forms import IntegrationSettingsForm
from .models import IntegrationSettings, WebhookDelivery, WebhookSettings
from .views import admin_required, initialized
from .webhook_config import webhook_values
from .webhook_forms import WebhookSettingsForm


def _settings_access(request):
    if not initialized():
        return redirect("setup")
    if not (request.user.is_authenticated and request.user.is_active and request.user.is_staff):
        return redirect("login")
    return None


@sensitive_post_parameters("rest_key", "mcp_key")
@require_http_methods(["GET", "POST"])
def integrations(request):
    denied = _settings_access(request)
    if denied is not None:
        return denied
    stored = IntegrationSettings.objects.filter(pk=1).first() or IntegrationSettings()
    initial = {
        "rest_enabled": stored.rest_enabled, "mcp_enabled": stored.mcp_enabled,
        "rest_key_action": "keep", "mcp_key_action": "keep",
    }
    form = IntegrationSettingsForm(
        request.POST if request.method == "POST" else None, initial=initial, stored=stored
    )
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        # Update only selected fields so another administrator's key is not overwritten.
        defaults = {}
        for name in ("rest", "mcp"):
            defaults[f"{name}_enabled"] = data[f"{name}_enabled"]
            action = data[f"{name}_key_action"]
            if action == "replace":
                key = data[f"{name}_key"]
                defaults[f"{name}_key_hash"] = make_password(key)
                defaults[f"{name}_key_suffix"] = key[-4:]
            elif action == "revoke":
                defaults[f"{name}_key_hash"] = defaults[f"{name}_key_suffix"] = ""
        with transaction.atomic():
            IntegrationSettings.objects.update_or_create(pk=1, defaults=defaults)
        messages.success(request, "API and MCP settings saved. Key changes take effect immediately.")
        return redirect("integration-settings")
    return render(request, "inbox/integration_settings.html", {
        "form": form,
        "rest_has_key": bool(stored.rest_key_hash), "rest_suffix": stored.rest_key_suffix,
        "mcp_has_key": bool(stored.mcp_key_hash), "mcp_suffix": stored.mcp_key_suffix,
        "rest_endpoint": request.build_absolute_uri("/api/v1/messages/"),
        "mcp_endpoint": request.build_absolute_uri("/mcp/"),
    })


@require_POST
@admin_required
def generate_key(request):
    return JsonResponse({"key": secrets.token_urlsafe(32)})


@sensitive_post_parameters("secret")
@require_http_methods(["GET", "POST"])
def webhooks(request):
    denied = _settings_access(request)
    if denied is not None:
        return denied
    config_error = False
    try:
        values = webhook_values()
    except ValueError:
        stored = WebhookSettings.objects.filter(pk=1).first() or WebhookSettings()
        values = {"enabled": False, "url": stored.url, "secret": "", "secret_header": stored.secret_header}
        config_error = True
    initial = {name: value for name, value in values.items() if name != "secret"}
    initial["secret_action"] = "keep"
    form = WebhookSettingsForm(
        request.POST if request.method == "POST" else None,
        initial=initial, stored_secret=values["secret"],
    )
    if request.method == "POST" and config_error and not request.POST.get("secret") and request.POST.get("secret_action") != "remove":
        form.is_valid()
        form.add_error("secret", "Replace or remove the unreadable saved secret.")
    if request.method == "POST" and form.is_valid():
        WebhookSettings.objects.update_or_create(pk=1, defaults={
            name: form.cleaned_data[name] for name in ("enabled", "url", "secret", "secret_header")
        })
        messages.success(request, "Webhook settings saved.")
        return redirect("webhook-settings")
    summary = {
        "pending": WebhookDelivery.objects.filter(sent_at__isnull=True).count(),
        "last_sent": WebhookDelivery.objects.exclude(sent_at=None).order_by("-sent_at").values_list("sent_at", flat=True).first(),
    }
    return render(request, "inbox/webhook_settings.html", {
        "form": form, "has_secret": bool(values["secret"]),
        "config_error": config_error, "delivery_summary": summary,
    })
