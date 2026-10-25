"""Verify email forwarding labels and sending times in an isolated browser session."""

import argparse
import json
from pathlib import Path
import sys
import tempfile
from uuid import uuid4

from smoke_install import docker, require, wait_healthy


ADMIN = "forwarding-smoke-admin"
PASSWORD = "ForwardingSmoke-482!"
SENT_AT = "2026-01-02T03:04:00+00:00"
VISIBLE_SENT_AT = "Sent Jan 2, 2026, 3:04 AM"
CASES = {
    "pending": ("pending@example.invalid", "Email pending"),
    "error": ("error@example.invalid", "Email error"),
    "sent": ("sent@example.invalid", "Email sent"),
    "not_queued": ("notqueued@example.invalid", "Email not queued"),
}


def seed(container):
    query = """
import json
import os
import sys
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django
django.setup()
from django.contrib.auth.models import User
from django.utils.dateparse import parse_datetime
from inbox.models import DeviceSettings, ForwardDelivery, Installation, Message, SMTPSettings
fixture = json.load(sys.stdin)
User.objects.create_superuser(fixture["admin"], "smoke@example.invalid", fixture["password"])
Installation.objects.update_or_create(pk=1, defaults={"completed": True})
DeviceSettings.objects.update_or_create(pk=1, defaults={
    "host": "synthetic.invalid", "username": "smoke",
    "trusted_at": parse_datetime("2026-01-01T00:00:00+00:00"),
})
SMTPSettings.objects.update_or_create(pk=1, defaults={
    "enabled": True, "host": "127.0.0.1", "port": 1, "security": "none",
    "from_email": "sender@example.invalid", "recipients": "recipient@example.invalid",
})
ids = {}
for state, (sender, label) in fixture["cases"].items():
    message = Message.objects.create(
        source="synthetic", sender=sender, text=f"Synthetic {state} SMS",
        received_at=parse_datetime("2025-12-31T12:00:00+00:00"),
    )
    ids[state] = message.pk
    if state != "not_queued":
        ForwardDelivery.objects.create(
            message=message,
            sent_at=parse_datetime(fixture["sent_at"]) if state == "sent" else None,
            last_error="Synthetic prior failure" if state == "error" else "",
            attempts=1 if state == "error" else 0,
        )
print(json.dumps(ids))
"""
    fixture = {"admin": ADMIN, "password": PASSWORD, "sent_at": SENT_AT, "cases": CASES}
    return json.loads(
        docker("exec", "-i", container, "python", "-c", query, input=json.dumps(fixture))
    )


def set_forwarding_enabled(container, enabled):
    query = (
        "import os; os.environ.setdefault('DJANGO_SETTINGS_MODULE','config.settings'); "
        "import django; django.setup(); from inbox.models import SMTPSettings; "
        f"SMTPSettings.objects.filter(pk=1).update(enabled={enabled!r})"
    )
    docker("exec", container, "python", "-c", query)


def verify_database(container, ids):
    query = """
import json
import os
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django
django.setup()
from inbox.models import ForwardDelivery, Message
print(json.dumps({
    "messages": Message.objects.count(),
    "deliveries": {str(row.message_id): {
        "sent_at": row.sent_at.isoformat() if row.sent_at else None,
        "attempts": row.attempts,
    } for row in ForwardDelivery.objects.all()},
}))
"""
    result = json.loads(docker("exec", container, "python", "-c", query))
    expected = {
        str(ids["sent"]): {"sent_at": SENT_AT, "attempts": 0},
        str(ids["pending"]): {"sent_at": None, "attempts": 0},
        str(ids["error"]): {"sent_at": None, "attempts": 1},
    }
    require(result["messages"] == len(CASES), "The browser check changed the SMS archive")
    require(result["deliveries"] == expected, "Sending times or delivery attempts changed")


def run_browser(base_url, container, ids, artifacts_dir):
    from playwright.sync_api import expect, sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(locale="en-US", timezone_id="UTC")
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))

        def row(state):
            return page.locator(".message-row").filter(has_text=CASES[state][0])

        def open_message(state):
            with page.expect_response(
                lambda response: (
                    response.request.method == "POST"
                    and response.url.endswith(f"/api/messages/{ids[state]}/")
                )
            ) as response:
                row(state).locator("button.message-open").click()
            require(response.value.ok, f"Unable to open the {state} SMS")
            expect(page.locator("#reader .reader-sender")).to_have_text(CASES[state][0])
            expect(page.locator("#reader .message-body")).to_have_text(f"Synthetic {state} SMS")
            return page.locator("#reader-forwarding")

        def refresh(enabled):
            with page.expect_response(
                lambda response: (
                    response.request.method == "GET" and "/api/messages/?" in response.url
                )
            ) as response:
                page.locator("#refresh").click()
            require(response.value.ok, "Unable to refresh the inbox")
            require(
                response.value.json()["forwarding_enabled"] is enabled,
                "The API did not report the expected forwarding setting",
            )

        def expect_sent_time(scope):
            timestamp = scope.locator("time.forwarded-at")
            expect(timestamp).to_be_visible()
            expect(timestamp).to_have_attribute("datetime", SENT_AT)
            expect(timestamp).to_have_text(VISIBLE_SENT_AT)

        def expect_hidden():
            expect(page.locator(".message-row")).to_have_count(len(CASES))
            expect(
                page.locator(".forwarding-status, .forwarding-label, time.forwarded-at")
            ).to_have_count(0)

        try:
            page.goto(base_url, wait_until="networkidle")
            page.get_by_label("Username").fill(ADMIN)
            page.get_by_label("Password").fill(PASSWORD)
            page.get_by_role("button", name="Sign in").click()
            expect(page.get_by_role("heading", name="Inbox")).to_be_visible()
            expect(page.locator(".message-row")).to_have_count(len(CASES))

            for state, (_, label) in CASES.items():
                expect(row(state).locator(".forwarding-label")).to_have_text(label)
                expect(row(state).locator(".forwarding-label")).to_be_visible()
                reader = open_message(state)
                expect(reader.locator(".forwarding-label")).to_have_text(label)
                expect(reader.locator(".forwarding-label")).to_be_visible()
                if state == "sent":
                    expect_sent_time(row(state))
                    expect_sent_time(reader)
                else:
                    expect(row(state).locator("time.forwarded-at")).to_have_count(0)
                    expect(reader.locator("time.forwarded-at")).to_have_count(0)
            print("PASS: all four statuses and sending times in the inbox and reader", flush=True)

            open_message("sent")
            set_forwarding_enabled(container, False)
            refresh(False)
            expect_hidden()
            expect(page.locator("#reader .reader-sender")).to_have_text(CASES["sent"][0])
            open_message("pending")
            expect_hidden()
            page.reload(wait_until="networkidle")
            expect_hidden()
            open_message("sent")
            expect_hidden()
            print(
                "PASS: disabled forwarding hides statuses and times after refresh, open, and reload",
                flush=True,
            )

            set_forwarding_enabled(container, True)
            refresh(True)
            for state, (_, label) in CASES.items():
                expect(row(state).locator(".forwarding-label")).to_have_text(label)
            expect_sent_time(row("sent"))
            reader = page.locator("#reader-forwarding")
            expect(reader.locator(".forwarding-label")).to_have_text("Email sent")
            expect_sent_time(reader)
            require(not errors, f"Browser JavaScript errors: {errors}")
            print(
                "PASS: re-enabling restores statuses and sending time in the open reader",
                flush=True,
            )
        except Exception:
            artifacts_dir.mkdir(parents=True, exist_ok=True)
            (artifacts_dir / "browser-errors.log").write_text("\n".join(errors))
            try:
                page.screenshot(
                    path=str(artifacts_dir / "forwarding-ui-failure.png"), full_page=True
                )
            except Exception as error:
                print(f"Unable to capture failure screenshot: {error}", file=sys.stderr)
            raise
        finally:
            context.close()
            browser.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--image", required=True, help="Locally available image to test; never pulled"
    )
    parser.add_argument("--timeout", type=int, default=120, help="Health timeout in seconds")
    parser.add_argument("--artifacts-dir", type=Path, help="Directory for failure diagnostics")
    args = parser.parse_args()
    require(args.timeout > 0, "The health timeout must be positive")
    name = f"unifi-sms-forwarding-ui-{uuid4().hex[:12]}"
    volume = f"{name}-data"
    artifacts_dir = args.artifacts_dir or Path(tempfile.gettempdir()) / f"{name}-artifacts"
    container_created = volume_created = False
    try:
        docker("image", "inspect", args.image)
        docker("volume", "create", volume)
        volume_created = True
        # Keep pending/error fixtures stable and avoid device or SMTP connections.
        web_only = (
            "import os,subprocess,sys; os.umask(0o077); "
            "subprocess.run([sys.executable,'manage.py','migrate','--noinput'],check=True); "
            "os.execvp('gunicorn',['gunicorn','config.wsgi:application',"
            "'--bind','0.0.0.0:8000','--workers','1','--threads','4','--access-logfile','/dev/null'])"
        )
        docker(
            "create",
            "--pull=never",
            "--name",
            name,
            "--publish",
            "127.0.0.1::8000",
            "--mount",
            f"type=volume,source={volume},target=/data",
            "--security-opt",
            "no-new-privileges:true",
            "--cap-drop",
            "ALL",
            "--stop-timeout",
            "20",
            "--entrypoint",
            "python",
            args.image,
            "-c",
            web_only,
        )
        container_created = True
        docker("start", name)
        base_url = wait_healthy(name, args.timeout)
        ids = seed(name)
        run_browser(base_url, name, ids, artifacts_dir)
        verify_database(name, ids)
        print("PASS: sending times retained and no email delivery attempts occurred", flush=True)
    except Exception:
        if container_created:
            try:
                logs = docker("logs", "--tail", "100", name)
                print(f"Container logs:\n{logs}", file=sys.stderr)
                artifacts_dir.mkdir(parents=True, exist_ok=True)
                (artifacts_dir / "container.log").write_text(logs)
            except Exception as error:
                print(f"Unable to save container logs: {error}", file=sys.stderr)
        if artifacts_dir.exists():
            print(f"Artifacts: {artifacts_dir}", file=sys.stderr)
        raise
    finally:
        try:
            if container_created:
                docker("rm", "--force", name)
        finally:
            if volume_created:
                docker("volume", "rm", volume)
        print("Removed temporary forwarding UI resources.", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"Forwarding UI smoke test failed: {error}", file=sys.stderr)
        sys.exit(1)
