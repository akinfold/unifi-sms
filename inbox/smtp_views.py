import smtplib
import ssl

from django.contrib import messages
from django.core.mail import EmailMessage
from django.core.mail.backends.smtp import EmailBackend
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_http_methods

from .models import ForwardDelivery, SMTPSettings
from .smtp_config import smtp_enabled, smtp_values
from .smtp_forms import SMTPSettingsForm
from .views import initialized


@sensitive_post_parameters("password")
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

    values, _ = smtp_values()
    stored_password = values.get("password", "")
    initial = {key: value for key, value in values.items() if key != "password"}
    if request.method == "POST":
        test_email = request.POST.get("action") == "test-email"
        form = SMTPSettingsForm(
            request.POST, stored_password=stored_password, test_email=test_email
        )
        if test_email:
            if not form.is_valid():
                return JsonResponse(
                    {
                        "ok": False,
                        "message": "Check the SMTP settings before sending a test email.",
                        "errors": form.errors.get_json_data(),
                    },
                    status=400,
                )
            return _send_test_email(form.cleaned_data)
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
            "form": form,
            "enabled_checked": (
                smtp_enabled({"enabled": request.POST.get("enabled", "")})
                if request.method == "POST"
                else str(values["enabled"]).strip().lower() in {"1", "true", "yes", "on"}
            ),
            "has_password": bool(stored_password),
            "has_credentials": bool(stored_password or values.get("username")),
            "password_action": password_action,
            "password_reentry": bool(request.POST.get("password")),
            "delivery_summary": delivery_summary,
        },
    )


def _send_test_email(config):
    try:
        with EmailBackend(
            host=config["host"],
            port=config["port"],
            username=config["username"],
            password=config["password"],
            use_tls=config["security"] == "starttls",
            use_ssl=config["security"] == "tls",
            timeout=10,
            fail_silently=False,
        ) as connection:
            for recipient in config["recipients"].split(", "):
                email = EmailMessage(
                    subject="UniFi SMS: SMTP test",
                    body=(
                        "This is a test email from UniFi SMS.\n\n"
                        "It was sent using the settings entered on the Email forwarding page.\n"
                        "No SMS content is included.\n"
                    ),
                    from_email=config["from_email"],
                    to=[recipient],
                    connection=connection,
                )
                if email.send() != 1:
                    raise smtplib.SMTPException("Message not accepted")
    except smtplib.SMTPAuthenticationError:
        message = "SMTP sign-in failed. Check your username and password or API key."
    except smtplib.SMTPRecipientsRefused:
        message = "The SMTP server rejected a recipient. Check the recipient addresses."
    except smtplib.SMTPSenderRefused:
        message = "The SMTP server rejected the sender. Check the From address and your provider's sender verification."
    except smtplib.SMTPNotSupportedError:
        message = "The SMTP server does not support the selected security or authentication method."
    except ssl.SSLError:
        message = "The secure SMTP connection failed. Check the encryption method and server certificate."
    except TimeoutError:
        message = "The SMTP connection timed out. Check the host, port, and network access."
    except OSError:
        message = "Could not connect to the SMTP server. Check the host, port, and network access."
    except Exception:
        message = "The test email could not be sent to every recipient. Check the SMTP settings and try again."
    else:
        return JsonResponse(
            {
                "ok": True,
                "message": "The SMTP server accepted the test email for all recipients. Check your inbox and spam folder. Settings were not saved.",
            }
        )
    return JsonResponse({"ok": False, "message": message}, status=502)
