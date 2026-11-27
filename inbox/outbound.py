import hashlib
import json
import re
import secrets
import uuid
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone
from django.utils.crypto import salted_hmac

from modem.client import ModemClient
from modem.outbound import OutboundError, OutboundSimChanged, OutboundUnknown, validate_text

from .connection import get_connection
from .models import DeviceSettings, IntegrationSettings, OutboundMessage, OutboundProfile, OutboundSetup
from .rate_limits import reserve, window_key
from .sms_service import ApiError
from .trust import trust_lock

PHONE = re.compile(r"\+[1-9][0-9]{1,14}\Z")
REQUEST_KEY = re.compile(r"[!-~]{1,128}\Z")
PROFILES = ("qmi_ims_off", "qmi_ims_on", "at_pdu")
RUNNING = ("discovering", "number", "queued", "sending", "waiting", "verified")
TESTING = ("queued", "sending", "waiting", "verified")
WAIT_TIME = timedelta(seconds=90)
CANCEL_AFTER = timedelta(minutes=15)
QUEUE_TTL = timedelta(minutes=5)
SESSION_FAILED = "Could not open the modem session. Check the device connection and try again."
UNCONFIRMED = "The modem's response was lost. Check with the recipient before sending again."


def connection_id():
    stored = DeviceSettings.objects.filter(pk=1).values(
        "host", "port", "username", "fingerprint", "host_keys", "trusted_at"
    ).first()
    if not stored or not stored["trusted_at"]:
        return ""
    token = json.dumps(stored, sort_keys=True, default=str)
    return hashlib.sha256(token.encode()).hexdigest()


def active_profile(identity=None):
    identity = connection_id() if identity is None else identity
    return OutboundProfile.objects.filter(pk=1, connection_id=identity).first() if identity else None


def integration_owner(request, kind):
    return f"{kind}:{request.integration_key_hash}"


def integration_allowed(owner):
    kind, _, key_hash = owner.partition(":")
    if kind not in ("rest", "mcp"):
        return False
    return IntegrationSettings.objects.filter(pk=1, **{
        f"{kind}_enabled": True,
        f"{kind}_send_enabled": True,
        f"{kind}_key_hash": key_hash,
    }).exists()


def _require_permission(owner):
    if owner.startswith("ui:"):
        return
    if not integration_allowed(owner):
        raise ApiError("SMS sending is disabled for this integration.", 403)


def _phone(value):
    if not isinstance(value, str) or not PHONE.fullmatch(value.strip()):
        raise ApiError("Enter an international phone number starting with +.")
    return value.strip()


def _job_data(job):
    return {
        "id": str(job.pk), "to": job.recipient, "status": job.status, "detail": job.detail,
        "modem_reference": job.modem_ref or None, "updated_at": job.updated_at.isoformat(),
    }


def get_job(owner, job_id):
    _require_permission(owner)
    try:
        job = OutboundMessage.objects.filter(pk=uuid.UUID(str(job_id)), owner=owner).first()
    except ValueError:
        job = None
    if job is None:
        raise ApiError("Send request not found.", 404)
    return _job_data(job)


@transaction.atomic
def enqueue(owner, payload, request_key):
    _require_permission(owner)
    if not isinstance(request_key, str) or not REQUEST_KEY.fullmatch(request_key):
        raise ApiError("Supply an idempotency key of 1 to 128 printable ASCII characters without spaces.")
    if set(payload) != {"to", "text"}:
        raise ApiError("Expected to and text.")
    recipient, text = _phone(payload["to"]), payload["text"]
    try:
        validate_text(text)
    except ValueError as exc:
        raise ApiError(str(exc)) from None
    digest = salted_hmac("outbound-sms-content", text, algorithm="sha256").hexdigest()
    existing = OutboundMessage.objects.filter(owner=owner, request_key=request_key).first()
    if existing:
        if (existing.recipient, existing.text_digest) != (recipient, digest):
            raise ApiError("This idempotency key belongs to a different message.", 409)
        return _job_data(existing)
    identity = connection_id()
    profile = active_profile(identity)
    if profile is None:
        raise ApiError("Complete SMS sending setup first.", 409)
    if OutboundSetup.objects.filter(pk=1, connection_id=identity, state__in=TESTING).exists():
        raise ApiError("Finish or cancel SMS sending setup before sending messages.", 409)
    if (
        OutboundMessage.objects.filter(status="queued").count() >= 20
        or not reserve([(window_key("outbound-sms"), 10)], 60)
    ):
        raise ApiError("Too many SMS send requests. Try again in a minute.", 429)
    job = OutboundMessage.objects.create(
        owner=owner, request_key=request_key, recipient=recipient, text=text,
        text_digest=digest, profile_revision=profile.revision, detail="Waiting for the modem.",
    )
    return _job_data(job)


def setup_status():
    identity = connection_id()
    setup = OutboundSetup.objects.filter(pk=1, connection_id=identity).first() or OutboundSetup()
    profile = active_profile(identity)
    model = DeviceSettings.objects.filter(pk=1).values_list("model", flat=True).first()
    return {
        "enabled": profile is not None,
        "device_model": model or "",
        "profile": profile.profile if profile else "",
        "state": setup.state,
        "detected_number": setup.detected_number,
        "phone": setup.phone,
        "profiles": setup.profiles,
        "attempts": [{key: attempt[key] for key in ("profile", "state", "detail")} for attempt in setup.attempts],
        "deadline": setup.deadline.isoformat() if setup.deadline else None,
        "detail": setup.detail,
        "job": None,
    }


@transaction.atomic
def setup_action(action, phone=None):
    identity = connection_id()
    setup = OutboundSetup.objects.filter(pk=1).first() or OutboundSetup()
    if action == "prepare":
        if not identity:
            raise ApiError("Connect a trusted LTE device first.", 409)
        if setup.connection_id == identity and setup.state in RUNNING:
            return
        OutboundSetup.objects.update_or_create(pk=1, defaults={
            "run": uuid.uuid4(), "connection_id": identity, "state": "discovering",
            "detail": "Checking SMS support and the SIM number.", "sim": "", "detected_number": "",
            "phone": "", "profiles": [], "attempts": [], "deadline": None,
        })
        return
    if not identity or setup.connection_id != identity:
        raise ApiError("Start setup for the current LTE device.", 409)
    if action == "start" and setup.state == "number":
        setup.phone = _phone(phone)
        setup.state, setup.detail = "queued", "Waiting to test the first profile."
    elif action == "cancel" and setup.state in RUNNING:
        setup.state, setup.detail = "canceled", "Setup canceled."
        setup.deadline = None
    elif action == "finish" and setup.state == "verified":
        OutboundProfile.objects.update_or_create(pk=1, defaults={
            "revision": uuid.uuid4(), "connection_id": identity, "sim": setup.sim,
            "profile": setup.attempts[-1]["profile"],
        })
        setup.state, setup.detail = "idle", ""
        setup.deadline = None
    else:
        raise ApiError("Setup has already moved to a different step. Refresh its status.", 409)
    setup.save()


def waiting_for_setup_sms():
    return OutboundSetup.objects.filter(pk=1, state="waiting").exists()


def _test_text(nonce):
    return f"UniFi SMS setup {nonce}"


def match_setup_receipt(identity, decoded):
    """Match an imported SMS against this run's self-addressed probes."""
    setup = OutboundSetup.objects.filter(pk=1, connection_id=identity).first()
    if not setup or decoded.concat_ref is not None or decoded.sender.removeprefix("+") != setup.phone.removeprefix("+"):
        return False
    for index, attempt in enumerate(setup.attempts):
        if decoded.text != _test_text(attempt["nonce"]):
            continue
        if (
            setup.state == "waiting" and index == len(setup.attempts) - 1
            and setup.deadline and timezone.now() <= setup.deadline
        ):
            attempt.update(state="verified", detail="Test SMS received.")
            setup.state, setup.detail = "verified", "Test SMS received. Finish setup to enable sending."
            setup.deadline = None
            setup.save()
        return True
    return False


def _update_setup(setup, **changes):
    return OutboundSetup.objects.filter(pk=1, run=setup.run, state=setup.state).update(
        updated_at=timezone.now(), **changes
    )


def _finish_attempt(setup, state, detail, next_state, deadline=None):
    setup.attempts[-1].update(state=state, detail=detail)
    _update_setup(setup, state=next_state, detail=detail, deadline=deadline, attempts=setup.attempts)


def _invalidate_profile(sim, identity):
    OutboundProfile.objects.filter(pk=1, connection_id=identity, sim=sim).delete()


def _discover(setup, config, client_factory):
    try:
        with client_factory(**config.client_kwargs) as client:
            sim, profiles, detected = client.outbound_capabilities()
        profiles = [profile for profile in PROFILES if profile in profiles]
        if not sim or not profiles:
            raise OutboundError("No compatible SMS submission method was found.")
    except OutboundError as exc:
        _update_setup(setup, state="failed", detail=str(exc)[:250])
    except Exception:
        _update_setup(setup, state="failed", detail="Could not check the modem. Verify the device connection and try again.")
    else:
        OutboundProfile.objects.filter(pk=1, connection_id=setup.connection_id).exclude(sim=sim).delete()
        _update_setup(
            setup, state="number", detail="Confirm the number of the SIM in the LTE device.",
            sim=sim, profiles=profiles,
            detected_number=detected if isinstance(detected, str) and PHONE.fullmatch(detected) else "",
        )


def _test_next(setup, config, client_factory):
    if len(setup.attempts) >= len(setup.profiles):
        _update_setup(setup, state="failed", detail="No profile returned a test SMS. Check the SIM number and SMS service.")
        return
    profile = setup.profiles[len(setup.attempts)]
    nonce = secrets.token_hex(12)
    setup.attempts.append({"profile": profile, "nonce": nonce, "state": "sending", "detail": "Submitting test SMS."})
    if not _update_setup(setup, state="sending", attempts=setup.attempts, detail="Submitting test SMS."):
        return
    setup.state = "sending"
    opened = False
    try:
        with client_factory(**config.client_kwargs) as client:
            opened = True
            client.send_sms(profile, setup.phone, _test_text(nonce), expected_sim=setup.sim)
    except OutboundSimChanged:
        _invalidate_profile(setup.sim, setup.connection_id)
        _finish_attempt(setup, "failed", "The SIM changed. Start setup again.", "failed")
    except OutboundUnknown:
        _finish_attempt(setup, "waiting", "Submission unconfirmed. Waiting for the test SMS.", "waiting", timezone.now() + WAIT_TIME)
    except OutboundError as exc:
        _finish_attempt(setup, "failed", str(exc)[:250], "queued")
    except Exception:
        if opened:
            _finish_attempt(setup, "waiting", "Submission unconfirmed. Waiting for the test SMS.", "waiting", timezone.now() + WAIT_TIME)
        else:
            _finish_attempt(setup, "failed", SESSION_FAILED, "failed")
    else:
        _finish_attempt(setup, "waiting", "Submitted. Waiting for the test SMS.", "waiting", timezone.now() + WAIT_TIME)


def _setup_tick(setup, identity, config, client_factory):
    now = timezone.now()
    if setup.connection_id != identity or not config or not config.configured:
        _update_setup(setup, state="failed", detail="The LTE device connection changed.", deadline=None)
    elif setup.state == "sending":
        _finish_attempt(setup, "waiting", "Submission interrupted. Waiting for the test SMS.", "waiting", now + WAIT_TIME)
    elif setup.state == "waiting" and now >= setup.deadline:
        _finish_attempt(setup, "timed_out", "No test SMS arrived before the timeout.", "queued")
    elif setup.state in ("number", "verified") and now - setup.updated_at >= CANCEL_AFTER:
        _update_setup(setup, state="canceled", detail="Setup timed out. Start again to test SMS sending.", deadline=None)
    elif setup.state == "discovering":
        _discover(setup, config, client_factory)
    elif setup.state == "queued":
        _test_next(setup, config, client_factory)


def _complete_job(job, status, detail, reference=""):
    OutboundMessage.objects.filter(pk=job.pk, status="sending").update(
        status=status, detail=detail, modem_ref=reference[:64], text="", updated_at=timezone.now()
    )


def _job_tick(identity, config, client_factory):
    with transaction.atomic():
        if OutboundSetup.objects.filter(pk=1, connection_id=identity, state__in=TESTING).exists():
            return
        job = OutboundMessage.objects.filter(status="queued").order_by("created_at", "pk").first()
        if job is None:
            return
        profile = active_profile(identity)
        job.status, job.detail = "sending", "Submitting to the modem."
        job.save(update_fields=["status", "detail", "updated_at"])
        if not profile or profile.revision != job.profile_revision or not config or not config.configured:
            _complete_job(job, "failed", "The SMS profile changed. Complete setup and try again.")
            return
        if timezone.now() - job.created_at >= QUEUE_TTL:
            _complete_job(job, "failed", "The send request expired in the queue. Try again.")
            return
        if job.owner.startswith("ui:"):
            authorized = get_user_model().objects.filter(
                pk=job.owner.removeprefix("ui:"), is_active=True, is_staff=True
            ).exists()
        else:
            authorized = integration_allowed(job.owner)
        if not authorized:
            _complete_job(job, "failed", "SMS sending permission was removed.")
            return
    opened = False
    try:
        with client_factory(**config.client_kwargs) as client:
            opened = True
            reference = client.send_sms(profile.profile, job.recipient, job.text, expected_sim=profile.sim)
    except OutboundSimChanged:
        _invalidate_profile(profile.sim, identity)
        _complete_job(job, "failed", "The SIM changed. Run SMS sending setup again.")
    except OutboundUnknown:
        _complete_job(job, "unknown", UNCONFIRMED)
    except OutboundError as exc:
        _complete_job(job, "failed", str(exc)[:250])
    except Exception:
        if opened:
            _complete_job(job, "unknown", UNCONFIRMED)
        else:
            _complete_job(job, "failed", SESSION_FAILED)
    else:
        _complete_job(job, "accepted", "The modem accepted the SMS.", str(reference))


def tick(client_factory=ModemClient):
    from .sync import importer_lock

    if (
        not OutboundSetup.objects.filter(pk=1, state__in=RUNNING).exists()
        and not OutboundMessage.objects.filter(status__in=("queued", "sending")).exists()
    ):
        return
    with importer_lock() as acquired:
        if not acquired:
            return
        # A send left in progress may already have reached the carrier.
        OutboundMessage.objects.filter(status="sending").update(
            status="unknown", detail=UNCONFIRMED, text="", updated_at=timezone.now()
        )
        with trust_lock():
            identity = connection_id()
            try:
                config = get_connection(_locked=True)
            except ValueError:
                config = None
            setup = OutboundSetup.objects.filter(pk=1, state__in=RUNNING).first()
            if setup:
                _setup_tick(setup, identity, config, client_factory)
            if not setup or setup.state not in TESTING:
                _job_tick(identity, config, client_factory)
