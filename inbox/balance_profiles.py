"""Documented balance routes and conservative monetary response recognition."""

from copy import deepcopy
from decimal import Decimal, InvalidOperation
import re


_CURRENCIES = {
    "EUR": ("EUR", "€"), "GBP": ("GBP", "£"), "NGN": ("NGN", "₦"),
    "USD": ("USD", "US$", "$"), "MXN": ("MXN", "MX$", "$"),
    "BRL": ("BRL", "R$"), "AUD": ("AUD", "A$", "$"),
    "NZD": ("NZD", "NZ$", "$"), "RUB": ("RUB", "₽", "руб."),
    "CAD": ("CAD", "C$", "$"), "INR": ("INR", "₹"),
    "ZAR": ("ZAR",), "JPY": ("JPY", "¥"), "CNY": ("CNY", "元"),
    "CHF": ("CHF",), "PLN": ("PLN", "zł"), "TRY": ("TRY", "₺"),
    "UAH": ("UAH", "₴"), "KZT": ("KZT", "₸"), "TWD": ("TWD", "NT$"),
}
_CARRIERS = (
    ("telekom-de", "Germany · Telekom Prepaid", "26201"),
    ("vodafone-uk", "United Kingdom · Vodafone Pay as you go bundles", "23415"),
    ("mtn-ng", "Nigeria · MTN BetaGist prepaid", "62130"),
    ("giffgaff-uk", "United Kingdom · giffgaff Prepaid", "23410"),
    ("att-us", "United States · AT&T Prepaid", "310410"),
    ("telcel-mx", "Mexico · Telcel Amigo", "334020"),
    ("claro-br", "Brazil · Claro Pré", "72405"),
    ("telstra-au", "Australia · Telstra Pre-Paid", "50501"),
    ("one-nz", "New Zealand · One NZ Prepay", "53001"),
)
# A destination does not establish the sender of the reply. Unknown SMS senders
# keep these documented routes unavailable for unattended requests.
_ROUTES = (
    ("telekom-de", "prepaid-ussd", "Prepaid · direct USSD", "ussd", "*100#", "", "", "EUR",
     "https://www.telekom.de/hilfe/mobilfunk/prepaid/guthaben-abfragen"),
    ("vodafone-uk", "payg-bundles", "Pay as you go bundles · direct USSD", "ussd", "*#1345#", "", "", "GBP",
     "https://www.vodafone.co.uk/sim-only/pay-as-you-go-sim/renew-bundle"),
    ("mtn-ng", "betagist", "BetaGist prepaid · direct USSD", "ussd", "*310#", "", "", "NGN",
     "https://www.mtn.ng/helppersonal/betagist/"),
    ("telekom-de", "prepaid-sms", "Prepaid · SMS (reply sender required)", "sms", "", "2000", "", "EUR",
     "https://www.telekom.de/hilfe/mobilfunk/prepaid/guthaben-abfragen"),
    ("giffgaff-uk", "info", "Prepaid · INFO (reply sender required)", "sms", "", "85075", "INFO", "GBP",
     "https://help.giffgaff.com/en/articles/258872-guide-to-the-usage-statement"),
    ("att-us", "prepaid", "Prepaid · code then SMS (reply sender required)", "code", "*777#", "", "", "USD",
     "https://www.att.com/support/article-modal/wireless/KM1048283/"),
    ("telcel-mx", "amigo", "Amigo · SMS (reply sender required)", "sms", "", "333", "Saldo", "MXN",
     "https://www.telcel.com/personas/amigo/consulta-tu-saldo/medios-de-consulta-saldo"),
    ("claro-br", "pre", "Pré · SMS (reply sender required)", "sms", "", "1052", "Claro", "BRL",
     "https://www.claro.com.br/faq/pre-pago/como-consulto-meu-saldo"),
    ("telstra-au", "prepaid", "Pre-Paid · SMS (reply sender required)", "sms", "", "125100", "balance", "AUD",
     "https://www.telstra.com.au/connected/mobiles/how-to-recharge-your-prepaid-service"),
    ("one-nz", "prepay", "Prepay · SMS (reply sender required)", "sms", "", "777", "BAL", "NZD",
     "https://one.nz/faq/one-nz-and-other-mobile-short-code-numbers"),
)
_BALANCE = re.compile(r"\b(?:balance|credit|guthaben|saldo|airtime|баланс|остаток)\b", re.I)
_NUMBER = r"[-+−]?[0-9](?:[0-9., \u00a0\u202f]*[0-9])?"
_QUOTA = re.compile(r"^\s*(?:%|[kmgt]b\b|[kmgt]ib\b|minutes?\b|mins?\b|days?\b|hours?\b|гб\b|мб\b|мин\b)", re.I)


def carriers():
    """Return carrier choices; network prefixes are hints, never tariff selection."""
    return [
        dict(id=key, label=label, network=network, profiles=catalog_profiles(key))
        for key, label, network in _CARRIERS
    ]


def network_hint(value):
    """Return a valid MCC/MNC prefix without inferring a carrier or tariff."""
    return value if isinstance(value, str) and re.fullmatch(r"[0-9]{5,6}", value) else ""


def catalog_profiles(carrier):
    """Return explicit tariff choices, including routes awaiting an exact SMS sender."""
    result = []
    for owner, key, label, transport, code, destination, body, currency, source in _ROUTES:
        if owner == carrier:
            result.append(dict(
                id=f"{owner}:{key}", label=label, transport=transport, code=code,
                destination=destination, text=body, sender="", currency=currency,
                marker="", timeout=30 if transport == "ussd" else 90, eligible=transport == "ussd", source=source,
            ))
    return deepcopy(result)


def validate_recognition(payload):
    """Validate an agreed ISO currency and an optional literal amount prefix."""
    currency = payload.get("currency", "")
    marker = payload.get("marker", "")
    if not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency):
        raise ValueError("Enter a three-letter currency code, such as EUR.")
    if not isinstance(marker, str) or len(marker) > 80 or any(not c.isprintable() for c in marker):
        raise ValueError("The amount marker must be at most 80 printable characters.")
    if marker and not marker.strip():
        raise ValueError("The amount marker cannot contain only whitespace.")
    return {"currency": currency, "marker": marker}


def manual_profile(payload):
    """Validate one manual route without accepting executable expressions or patterns."""
    allowed = {"id", "label", "transport", "code", "destination", "text", "sender", "currency", "marker", "timeout"}
    if not isinstance(payload, dict) or set(payload) - allowed:
        raise ValueError("Unknown manual profile fields.")
    result = {key: payload.get(key, "") for key in allowed - {"timeout"}}
    if any(not isinstance(value, str) for value in result.values()):
        raise ValueError("Profile fields must be text.")
    result["id"] = "manual"
    result["label"] = result["label"] or "Manual profile"
    if len(result["label"]) > 100 or any(not c.isprintable() for c in result["label"]):
        raise ValueError("The profile label must be at most 100 printable characters.")
    recognition = dict(payload)
    if recognition.get("currency", "") == "":
        recognition["currency"] = "USD"
    result.update(validate_recognition(recognition))
    result["currency"] = payload.get("currency", "")
    result["sender"] = result["sender"].strip()
    timeout = payload.get("timeout", 30 if result["transport"] == "ussd" else 90)
    upper_timeout = 60 if result["transport"] == "ussd" else 300
    if type(timeout) is not int or not 5 <= timeout <= upper_timeout:
        raise ValueError(f"Enter a response timeout from 5 to {upper_timeout} seconds.")
    result["timeout"] = timeout
    transport = result["transport"]
    if transport not in {"ussd", "sms", "code"}:
        raise ValueError("Choose USSD, SMS, or code followed by SMS.")
    if transport in {"ussd", "code"}:
        if (not re.fullmatch(r"[*#][*#0-9]{0,30}#", result["code"])
                or not re.search(r"[0-9]", result["code"])):
            raise ValueError("A dial code must contain only digits, * and #, and end in #.")
        if result["destination"] or result["text"]:
            raise ValueError("Dial-code profiles cannot contain an SMS destination or body.")
    else:
        if result["code"] or not re.fullmatch(r"(?:[0-9]{2,15}|\+[1-9][0-9]{7,14})", result["destination"]):
            raise ValueError("An SMS profile requires a short number or an international destination.")
        if len(result["text"]) > 160 or any(not c.isprintable() for c in result["text"]):
            raise ValueError("The SMS body must contain at most 160 printable characters.")
    if transport == "ussd":
        if result["sender"]:
            raise ValueError("Direct USSD profiles do not use an SMS sender.")
    elif not re.fullmatch(r"[A-Za-z0-9+][A-Za-z0-9+ ._-]{0,63}", result["sender"]):
        raise ValueError("An exact reply sender is required; patterns are not supported.")
    return result


def _amount(raw):
    value = raw.replace("−", "-").replace("\u00a0", " ").replace("\u202f", " ")
    sign = ""
    if value[:1] in {"-", "+"}:
        sign, value = value[0], value[1:]
    if " " in value:
        if not re.fullmatch(r"[0-9]{1,3}(?: [0-9]{3})+(?:[.,][0-9]{1,2})?", value):
            return None
        value = value.replace(" ", "")
    if "." in value and "," in value:
        decimal, grouping = (".", ",") if value.rfind(".") > value.rfind(",") else (",", ".")
        if not re.fullmatch(r"[0-9]{1,3}(?:" + re.escape(grouping) + r"[0-9]{3})+" + re.escape(decimal) + r"[0-9]{1,2}", value):
            return None
        value = value.replace(grouping, "").replace(decimal, ".")
    elif "." in value or "," in value:
        if not re.fullmatch(r"[0-9]+[.,][0-9]{1,2}", value):
            return None
        value = value.replace(",", ".")
    if len(value.split(".")[0]) > 12:
        return None
    try:
        return Decimal(sign + value)
    except InvalidOperation:
        return None


def parse_balance(text, profile):
    """Return one monetary amount, or None for unrecognized or ambiguous text."""
    if not isinstance(text, str) or not 0 < len(text) <= 4096:
        return None
    if any(not c.isprintable() and c not in "\r\n\t\u00a0\u202f" for c in text):
        return None
    try:
        recognition = validate_recognition(profile)
    except (ValueError, AttributeError):
        return None
    currency, marker = recognition["currency"], recognition["marker"]
    currency_aliases = _CURRENCIES.get(currency, (currency,))
    aliases = "(?:" + "|".join(re.escape(alias) for alias in sorted(currency_aliases, key=len, reverse=True)) + ")"
    if marker:
        if text.casefold().count(marker.casefold()) != 1:
            return None
        location = re.search(re.escape(marker), text, re.I)
        if not location:
            return None
        start = location.end()
        pattern = re.compile(r"\s*(?:" + aliases + r"\s*)?(?P<amount>" + _NUMBER + r")(?![\w/:-]|[.,][0-9])", re.I)
        found = pattern.match(text, start)
        if not found or _QUOTA.match(text[found.end():]):
            return None
        # Reject a conflicting explicit currency rather than changing its meaning.
        tail = text[found.end():]
        for other, symbols in _CURRENCIES.items():
            if other != currency and any(re.match(r"\s*" + re.escape(s) + r"(?!\w)", tail, re.I) for s in symbols if s not in currency_aliases):
                return None
        return _amount(found["amount"])
    if not _BALANCE.search(text):
        return None
    pattern = re.compile(
        r"(?<![\w.,/:+-])(?:" + aliases + r"\s*(?P<before>" + _NUMBER + r")|(?P<after>" + _NUMBER + r")\s*" + aliases + r")(?![\w/:-]|[.,][0-9])", re.I)
    matches = list(pattern.finditer(text))
    all_aliases = "(?:" + "|".join(
        re.escape(alias) for alias in sorted(
            {currency, *(alias for group in _CURRENCIES.values() for alias in group)},
            key=len, reverse=True,
        )
    ) + ")"
    all_money = re.compile(
        r"(?<![\w.,/:+-])(?:" + all_aliases + r"\s*" + _NUMBER
        + "|" + _NUMBER + r"\s*" + all_aliases + r")(?![\w/:-]|[.,][0-9])", re.I)
    if len(list(all_money.finditer(text))) != 1:
        return None
    if len(matches) != 1 or _QUOTA.match(text[matches[0].end():]):
        return None
    money = matches[0]
    associated = False
    for label in _BALANCE.finditer(text):
        if label.end() <= money.start():
            gap = text[label.end():money.start()]
        elif money.end() <= label.start():
            gap = text[money.end():label.start()]
        else:
            continue
        if (len(gap) <= 40 and not re.search(r"[0-9\r\n;.!?]", gap)
                and not re.search(r"\b(?:bonus|limit|renewal|price|cost|top|recharge|data)\b", gap, re.I)):
            associated = True
    return _amount(money["before"] or money["after"]) if associated else None
