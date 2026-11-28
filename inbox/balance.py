import re
import uuid
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.utils import timezone
from modem.client import ModemClient
from modem.outbound import OutboundError, OutboundSimChanged, OutboundUnknown

from .balance_alerts import deliver_alerts, smtp_config
from .balance_profiles import (
    carriers, catalog_profiles, manual_profile, parse_balance, validate_recognition,
)
from .connection import get_connection
from .forwarding import queue_forward
from .models import BalanceMonitor, BalanceRun, OutboundSetup
from .outbound import PHONE, active_profile, connection_id
from .sms_service import ApiError
from .trust import trust_lock
from .webhooks import queue_webhook

METHODS = {"ussd": "USSD", "sms": "SMS", "code": "Code + SMS"}
WORKING = ("detecting", "queued", "requesting", "waiting", "activating")
SETUP_OPEN = (*WORKING, "carrier", "manual_result", "verified")
OUTBOUND_TESTING = ("queued", "sending", "waiting")
WIZARD_TTL = timedelta(minutes=30)
OVERDUE_MARGIN = timedelta(minutes=5)
MAX_REPLY = 4096
SESSION_FAILED = "Could not read the modem or SIM. Check the LTE device connection and try again."
SIM_CHANGED = "The SIM or trusted device changed. Run balance setup for the current SIM."
UNCONFIRMED = "The carrier request may have been submitted. Wait before testing this request again."


def _run(key="setup"):
    return BalanceRun.objects.filter(pk=key).first() or BalanceRun(key=key)


def _monitor():
    return BalanceMonitor.objects.filter(pk=1).first() or BalanceMonitor()


def _profile(run):
    return run.profiles[len(run.attempts) - 1] if run.attempts else {}


def _interval(profile, override):
    return override or (6 if profile.get("transport") == "ussd" else 24)


def _amount(value):
    return format(value.normalize(), "f") if value is not None else None


def _date(value):
    return value.isoformat() if value is not None else None


def summary(monitor=None, identity=None):
    monitor = _monitor() if monitor is None else monitor
    identity = connection_id() if identity is None else identity
    enabled = bool(monitor.enabled and identity and monitor.connection_id == identity)
    profile = monitor.profile
    return {
        "enabled": enabled,
        "amount": _amount(monitor.amount) if enabled else None,
        "currency": profile.get("currency", ""),
        "low": enabled and monitor.low,
        "checking": enabled and bool(monitor.next_check_at and monitor.next_check_at <= timezone.now()),
        "stale": enabled and (
            monitor.state == "error"
            or monitor.checked_at is None
            or timezone.now() > monitor.checked_at + timedelta(
                hours=_interval(profile, monitor.interval_hours)
            ) + OVERDUE_MARGIN
        ),
        "state": monitor.state,
        "detail": SIM_CHANGED if monitor.enabled and not enabled else monitor.detail,
        "checked_at": _date(monitor.checked_at) if enabled else None,
        "next_check_at": _date(monitor.next_check_at) if enabled else None,
        "method": METHODS.get(profile.get("transport"), ""),
        "profile": profile.get("label", ""),
        "interval_hours": _interval(profile, monitor.interval_hours) if profile else None,
        "settings": {
            "interval_hours": monitor.interval_hours,
            "threshold": _amount(monitor.threshold),
            "email": monitor.email,
            "phone": monitor.phone,
        },
        "alerts": monitor.alerts,
    }


def status():
    identity = connection_id()
    monitor = _monitor()
    setup = _run()
    if not identity or setup.connection_id != identity:
        setup = BalanceRun(key="setup")
    profile = _profile(setup)
    outbound = active_profile(identity)
    return {
        "summary": summary(monitor, identity),
        "wizard": {
            "state": setup.state,
            "detail": setup.detail,
            "network": setup.network,
            "attempts": setup.attempts,
            "total": len(setup.profiles),
            "deadline": _date(setup.deadline),
            "reply": setup.reply,
            "amount": _amount(setup.amount),
            "currency": profile.get("currency", ""),
            "marker": profile.get("marker", ""),
            "method": METHODS.get(profile.get("transport"), ""),
            "profile": profile.get("label", ""),
            "manual": setup.manual,
            "phase": "activation" if setup.state == "activating" else "",
        },
        "carriers": carriers(),
        "dependencies": {
            "smtp": smtp_config(monitor.email or "balance@example.test") is not None,
            "sms": bool(outbound and outbound.sim == (setup.sim or monitor.sim)),
        },
    }


def _form(payload, identity, sim):
    if set(payload) - {"interval_hours", "threshold", "email", "phone"}:
        raise ApiError("The request contains unsupported settings.")
    raw = payload.get("interval_hours")
    interval = None
    if raw not in (None, ""):
        if type(raw) not in (int, str) or not re.fullmatch(r"[0-9]{1,3}", str(raw)):
            raise ApiError("Enter a check interval from 1 to 168 hours.")
        interval = int(raw)
        if not 1 <= interval <= 168:
            raise ApiError("Enter a check interval from 1 to 168 hours.")
    raw = payload.get("threshold")
    threshold = None
    if raw not in (None, ""):
        if not isinstance(raw, (str, int, float)) or not re.fullmatch(
            r"[0-9]{1,12}(?:\.[0-9]{1,4})?", str(raw)
        ):
            raise ApiError("Enter a valid threshold with up to four decimal places.")
        try:
            threshold = _amount(Decimal(str(raw)))
        except InvalidOperation:
            raise ApiError("Enter a valid threshold.") from None
    email, phone = payload.get("email") or "", payload.get("phone") or ""
    if not isinstance(email, str) or not isinstance(phone, str):
        raise ApiError("Enter valid notification recipients.")
    email, phone = email.strip(), phone.strip()
    if (email or phone) and threshold is None:
        raise ApiError("Set a balance threshold to receive alerts.")
    if email:
        try:
            validate_email(email)
        except ValidationError:
            raise ApiError("Enter a valid alert email address.") from None
        if len(email) > 254 or smtp_config(email) is None:
            raise ApiError("Configure an SMTP server in Email forwarding before enabling email alerts.")
    if phone:
        if not PHONE.fullmatch(phone):
            raise ApiError("Enter an international phone number starting with +.")
        profile = active_profile(identity)
        if not profile or profile.sim != sim:
            raise ApiError("Complete Send SMS setup for this SIM before enabling SMS alerts.")
    return {"interval_hours": interval, "threshold": threshold, "email": email, "phone": phone}


def _discard_reply(run):
    if run.reply_message_id:
        message = run.reply_message
        if message and not message.is_balance_reply:
            queue_forward(message)
            queue_webhook(message)
        run.reply_message = None
    run.reply = ""


def _reset(run, state="idle", detail=""):
    _discard_reply(run)
    return BalanceRun(key=run.key, state=state, detail=detail)


def _require_active(monitor, identity):
    if not monitor.enabled or monitor.connection_id != identity:
        raise ApiError("Enable monitoring for the current SIM first.", 409)


def _require_available(identity):
    if not identity:
        raise ApiError("Connect a trusted LTE device first.", 409)
    if OutboundSetup.objects.filter(
        pk=1, connection_id=identity, state__in=OUTBOUND_TESTING
    ).exists():
        raise ApiError("Wait for the SMS sending test to finish.", 409)


def _start_setup(setup, profiles, *, manual):
    run = _reset(setup, "queued", "Waiting to test the balance request.")
    run.connection_id, run.sim, run.network = setup.connection_id, setup.sim, setup.network
    run.profiles, run.manual = profiles, manual
    run.save()


@transaction.atomic
def action(name, payload):
    identity = connection_id()
    setup = _run()
    monitor = _monitor()
    if name == "prepare":
        _require_available(identity)
        if _run("check").state in WORKING:
            raise ApiError("Wait for the current balance check to finish.", 409)
        if setup.connection_id != identity or setup.state not in WORKING:
            setup = _reset(setup, "detecting", "Identifying the SIM and its home network.")
            setup.connection_id = identity
            setup.save()
    elif name == "cancel":
        _reset(setup).save()
    elif name == "disable":
        monitor.enabled = monitor.low = False
        monitor.revision = uuid.uuid4()
        monitor.state, monitor.detail, monitor.next_check_at, monitor.alerts = "idle", "", None, {}
        monitor.save()
        _reset(setup).save()
        _reset(_run("check")).save()
    elif name in ("settings", "check"):
        _require_active(monitor, identity)
        if name == "check":
            _require_available(identity)
            if setup.state in SETUP_OPEN or _run("check").state in WORKING:
                raise ApiError("Wait for the current balance test or check to finish.", 409)
            if monitor.next_check_at and monitor.next_check_at <= timezone.now():
                return status()
            monitor.next_check_at = timezone.now()
            monitor.save(update_fields=["next_check_at"])
        else:
            cleaned = _form(payload, identity, monitor.sim)
            for channel, field in (("email", "email"), ("sms", "phone")):
                if cleaned[field] != getattr(monitor, field):
                    monitor.alerts.pop(channel, None)
            previous_interval = monitor.interval_hours
            for field, value in cleaned.items():
                setattr(monitor, field, value)
            if monitor.threshold is None or (
                monitor.amount is not None and monitor.amount > Decimal(monitor.threshold)
            ):
                monitor.low, monitor.alerts = False, {}
            if previous_interval != monitor.interval_hours and monitor.checked_at:
                monitor.next_check_at = monitor.checked_at + timedelta(
                    hours=_interval(monitor.profile, monitor.interval_hours)
                )
            monitor.save()
    else:
        if not identity or setup.connection_id != identity or not setup.sim:
            raise ApiError("Start balance setup for the current SIM.", 409)
        if timezone.now() - setup.updated_at > WIZARD_TTL:
            raise ApiError("This setup expired. Start again to verify the SIM.", 409)
        try:
            if name in ("auto", "manual") and setup.state in (
                "carrier", "failed", "manual_result", "verified"
            ):
                _require_available(identity)
                if name == "manual":
                    profiles = [manual_profile(payload)]
                else:
                    carrier = payload.get("carrier")
                    sender = payload.get("sender", "")
                    if not isinstance(sender, str):
                        raise ApiError("Enter the exact sender of the carrier's balance SMS.")
                    profiles = []
                    for profile in catalog_profiles(carrier) if isinstance(carrier, str) else []:
                        if profile["eligible"]:
                            profiles.append(profile)
                        elif sender.strip():
                            data = {
                                key: profile[key]
                                for key in ("transport", "code", "destination", "text", "currency")
                            }
                            candidate = manual_profile({**data, "sender": sender})
                            profiles.append({**profile, "sender": candidate["sender"]})
                    if not profiles:
                        raise ApiError("No automatic request is available for this carrier and plan. Use manual setup.")
                _start_setup(setup, profiles, manual=name == "manual")
            elif name == "recognize" and setup.manual and setup.state in ("manual_result", "verified"):
                result = validate_recognition(payload)
                profile = {**_profile(setup), **result}
                amount = parse_balance(setup.reply, profile)
                if amount is None:
                    raise ApiError("Could not identify one balance amount. Check the currency and text before it.")
                setup.profiles = [profile]
                _verified(setup, amount, setup.reply, setup.checked_at)
                setup.save()
            elif name == "enable" and setup.state == "verified":
                setup.settings = _form(payload, identity, setup.sim)
                setup.state, setup.detail = "activating", "Verifying the SIM before enabling monitoring."
                setup.save()
            else:
                raise ApiError("Balance setup moved to another step. Refresh its status.", 409)
        except ValueError as exc:
            raise ApiError(str(exc)) from None
    return status()


def _record_attempt(run, state, detail):
    run.attempts[-1].update(state=state, detail=detail)
    run.state, run.detail = state, detail


def _verified(run, amount, reply, checked_at):
    _record_attempt(run, "verified", "Received a balance amount.")
    run.amount, run.reply, run.checked_at, run.deadline = amount, reply, checked_at, None
    if run.reply_message_id:
        run.reply_message.is_balance_reply = True
        run.reply_message.save(update_fields=["is_balance_reply"])


def _store_result(monitor, amount, checked_at):
    monitor.amount, monitor.checked_at = amount, checked_at
    monitor.state, monitor.detail = "ok", ""
    monitor.next_check_at = checked_at + timedelta(
        hours=_interval(monitor.profile, monitor.interval_hours)
    )
    low = monitor.threshold is not None and amount <= Decimal(monitor.threshold)
    if low and not monitor.low:
        monitor.alerts = {
            channel: "pending"
            for channel, recipient in (("email", monitor.email), ("sms", monitor.phone))
            if recipient
        }
    elif not low:
        monitor.alerts = {}
    monitor.low = low
    monitor.save()


def _store_failure(monitor, detail):
    monitor.state, monitor.detail = "error", detail
    monitor.next_check_at = timezone.now() + timedelta(
        hours=_interval(monitor.profile, monitor.interval_hours)
    )
    monitor.save(update_fields=["state", "detail", "next_check_at"])


def _fail(run, detail, *, try_next=False, suspend=False):
    run.state, run.detail, run.deadline = "failed", detail, None
    if run.attempts:
        _record_attempt(run, "failed", detail)
    if try_next and len(run.attempts) < len(run.profiles):
        run.state = "queued"
    run.save()
    if run.key == "check":
        monitor = _monitor()
        if monitor.enabled and monitor.revision == run.revision:
            if suspend:
                monitor.enabled, monitor.low, monitor.alerts = False, False, {}
                monitor.state, monitor.detail, monitor.next_check_at = "suspended", detail, None
                monitor.save()
            else:
                _store_failure(monitor, detail)


def _receive(run, reply, checked_at, message=None):
    profile = _profile(run)
    amount = parse_balance(reply, profile)
    if amount is not None:
        run.reply_message = message
        _verified(run, amount, reply[:MAX_REPLY], checked_at)
        if run.key == "check":
            _store_result(_monitor(), amount, checked_at)
            run.reply, run.reply_message = "", None
        run.save()
        return True
    if run.key == "setup" and run.manual and len(reply) <= MAX_REPLY:
        _record_attempt(run, "manual_result", "Choose the currency and text before the amount.")
        run.reply, run.reply_message = reply, message
        run.checked_at, run.deadline = checked_at, None
        run.save()
        return True
    if message is None:
        _fail(run, "The response did not contain one recognizable balance amount.", try_next=True)
    return False


def waiting_for_reply(identity=None):
    runs = BalanceRun.objects.filter(state="waiting", deadline__gte=timezone.now())
    if identity is not None:
        runs = runs.filter(connection_id=identity)
    return runs.exists()


def match_reply(identity, sim, message):
    """Associate a complete, fresh SMS only with a matching request on the current SIM."""
    if not identity or not sim or message.saved_parts != message.total_parts:
        return False
    run = BalanceRun.objects.filter(
        connection_id=identity, sim=sim, state="waiting", deadline__gte=timezone.now()
    ).first()
    now = timezone.now()
    if not run or not run.started_at or not (
        run.started_at.replace(microsecond=0) <= message.received_at <= now + timedelta(seconds=30)
    ):
        return False
    sender = _profile(run).get("sender", "")
    if not sender or sender.casefold() != message.sender.casefold():
        return False
    return _receive(run, message.text, now, message)


def _begin_check(monitor, identity, now):
    if not monitor.enabled or not monitor.next_check_at or monitor.next_check_at > now:
        return
    run = BalanceRun(
        key="check", revision=monitor.revision, state="queued", connection_id=identity,
        sim=monitor.sim, profiles=[monitor.profile],
    )
    run.save()
    return run


def _activate(run, monitor):
    if (not monitor.enabled or monitor.sim != run.sim
            or monitor.profile.get("currency") != _profile(run)["currency"]):
        monitor.low, monitor.alerts = False, {}
    for channel, field in (("email", "email"), ("sms", "phone")):
        if run.settings[field] != getattr(monitor, field):
            monitor.alerts.pop(channel, None)
    monitor.enabled, monitor.connection_id, monitor.sim = True, run.connection_id, run.sim
    monitor.profile, monitor.revision = _profile(run), uuid.uuid4()
    for field, value in run.settings.items():
        setattr(monitor, field, value)
    _store_result(monitor, run.amount, run.checked_at)
    _reset(run).save()


def _probe(run, config, client_factory):
    original_state = run.state
    request_revision = run.revision
    activation = original_state == "activating"
    try:
        with client_factory(**config.client_kwargs) as client:
            sim, network = client.balance_identity()
    except Exception:
        sim, network = "", ""
    with transaction.atomic():
        run = _run(run.key)
        if run.revision != request_revision or run.state != original_state:
            return
        if not sim:
            _fail(run, SESSION_FAILED)
        elif activation and run.sim != sim:
            _fail(run, SIM_CHANGED)
        elif activation:
            _activate(run, _monitor())
        else:
            run.sim, run.network = sim, network
            run.state = "carrier"
            run.detail = (
                f"Home network prefix: {network}. Confirm the carrier and plan."
                if network else "Select the carrier and plan for this SIM."
            )
            run.save()


def _request(run, config, client_factory):
    with transaction.atomic():
        queued = _run(run.key)
        if queued.revision != run.revision or queued.state != "queued":
            return
        run = queued
        profile = run.profiles[len(run.attempts)]
        run.attempts.append({"label": profile["label"], "state": "requesting", "detail": ""})
        run.state, run.detail = "requesting", "Sending the balance request."
        run.started_at = timezone.now()
        if profile["transport"] == "ussd":
            run.deadline = run.started_at + timedelta(seconds=profile["timeout"])
        run.save()
    result, error, try_next, suspend = None, "", False, False
    try:
        with client_factory(**config.client_kwargs) as client:
            outbound = active_profile(run.connection_id)
            result = client.request_balance(
                profile, expected_sim=run.sim,
                sms_profile=outbound.profile if outbound and outbound.sim == run.sim else None,
            )
    except OutboundSimChanged:
        error, suspend = SIM_CHANGED, True
    except OutboundUnknown:
        error = UNCONFIRMED
    except (OutboundError, ValueError) as exc:
        error, try_next = str(exc)[:250], True
    except Exception:
        error = SESSION_FAILED
    with transaction.atomic():
        current = _run(run.key)
        if current.revision != run.revision or current.state != "requesting":
            return
        if error:
            _fail(current, error, try_next=try_next, suspend=suspend)
        elif result is None and profile["transport"] == "ussd":
            _fail(current, "The carrier returned no USSD balance.", try_next=True)
        elif result is None:
            _record_attempt(current, "waiting", "Request submitted. Waiting for the carrier SMS.")
            current.deadline = timezone.now() + timedelta(seconds=profile["timeout"])
            current.save()
        else:
            _receive(current, result, timezone.now())


@transaction.atomic
def _next_run(identity):
    now = timezone.now()
    monitor = _monitor()
    setup, check = _run(), _run("check")
    if monitor.enabled and monitor.connection_id != identity:
        monitor.enabled, monitor.low, monitor.alerts = False, False, {}
        monitor.state, monitor.detail, monitor.next_check_at = "suspended", SIM_CHANGED, None
        monitor.save()
    for run in (setup, check):
        if run.state == "idle":
            continue
        if run.connection_id != identity or (run.key == "check" and (
            not monitor.enabled or run.revision != monitor.revision
        )):
            _reset(run).save()
            continue
        if run.state == "requesting":
            _fail(run, UNCONFIRMED)
            continue
        if run.state == "waiting":
            if run.deadline > now:
                return
            _fail(run, "No matching balance SMS arrived before the timeout.", try_next=True)
        if run.key == "setup" and now - run.updated_at > WIZARD_TTL:
            _reset(run, "failed", "Setup expired. Run setup again to check the current SIM.").save()
            continue
        if run.state in ("detecting", "activating", "queued"):
            return run
        if run.key == "setup" and run.state in SETUP_OPEN:
            return
    return _begin_check(monitor, identity, now)


def tick(client_factory=ModemClient):
    from .sync import importer_lock

    if not BalanceMonitor.objects.filter(enabled=True).exists() and not BalanceRun.objects.exclude(
        state__in=("idle", "failed")
    ).exists():
        return
    with importer_lock() as acquired:
        if not acquired:
            return
        with trust_lock():
            identity = connection_id()
            if OutboundSetup.objects.filter(pk=1, connection_id=identity, state__in=OUTBOUND_TESTING).exists():
                return
            run = _next_run(identity)
            try:
                config = get_connection(_locked=True)
            except ValueError:
                config = None
            if run:
                if not config or not config.configured:
                    with transaction.atomic():
                        current = _run(run.key)
                        if current.revision == run.revision and current.state == run.state:
                            _fail(current, SESSION_FAILED)
                elif run.state in ("detecting", "activating"):
                    _probe(run, config, client_factory)
                else:
                    _request(run, config, client_factory)
            if not waiting_for_reply(identity):
                deliver_alerts(identity, config, client_factory)
