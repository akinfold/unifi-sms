import base64
import hashlib
from dataclasses import dataclass, field

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings

from .models import DeviceSettings
from modem.client import MODEM_DEVICE, SMS_STORAGE, parse_host_keys, parse_private_key


CONNECTION_FIELDS = (
    "host", "port", "username", "auth_method", "key_file", "known_hosts",
    "host_keys", "password", "private_key",
)
_CIPHER_PREFIX = "fernet:"


def _cipher():
    digest = hashlib.sha256(settings.SECRET_KEY.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def is_encrypted_secret(value):
    return value.startswith(_CIPHER_PREFIX)


def encrypt_secret(value):
    if not value:
        return ""
    return _CIPHER_PREFIX + _cipher().encrypt(value.encode("utf-8")).decode("ascii")


def decrypt_secret(value):
    if not value:
        return ""
    if not is_encrypted_secret(value):
        raise ValueError("Stored connection secret is invalid")
    try:
        return _cipher().decrypt(value[len(_CIPHER_PREFIX) :].encode("ascii")).decode("utf-8")
    except (InvalidToken, UnicodeError, ValueError):
        raise ValueError("Stored connection secret is invalid") from None


def validate_private_key(value):
    parse_private_key(value)


def validate_host_keys(value, host=None, port=22):
    keys = parse_host_keys(value)
    if host and not keys.lookup(host if port == 22 else f"[{host}]:{port}"):
        raise ValueError("SSH host keys do not contain the device address")


def connection_values():
    """Return saved connection settings; legacy environment variables are ignored."""
    stored = DeviceSettings.objects.filter(pk=1).first() or DeviceSettings()
    values = {name: getattr(stored, name) for name in CONNECTION_FIELDS}
    for name in ("password", "private_key"):
        values[name] = decrypt_secret(values[name])
    values["known_hosts"] = str(settings.DATA_DIR / "known_hosts")
    return values, ()


@dataclass(frozen=True)
class ConnectionConfig:
    host: str
    port: int
    username: str
    auth_method: str
    key_file: str
    known_hosts: str
    host_keys: str
    device: str = field(default=MODEM_DEVICE, init=False)
    storage: str = field(default=SMS_STORAGE, init=False)
    password: str
    private_key: str
    trusted: bool = False

    @property
    def configured(self):
        return bool(self.host and self.username and self.trusted)

    @property
    def client_kwargs(self):
        kwargs = {
            "host": self.host,
            "port": self.port,
            "username": self.username,
            "key_filename": self.key_file,
            "known_hosts": self.known_hosts,
            "device": self.device,
            "storage": self.storage,
        }
        if self.auth_method == "password":
            kwargs.update(auth_method="password", password=self.password)
        elif self.auth_method == "private_key":
            kwargs.update(auth_method="private_key", private_key=self.private_key)
        if self.host_keys:
            kwargs["host_keys"] = self.host_keys
        return kwargs


def _get_connection():
    values, _ = connection_values()
    try:
        port_value = values["port"]
        if isinstance(port_value, bool) or not (
            isinstance(port_value, int)
            or isinstance(port_value, str) and port_value.isdecimal()
        ):
            raise ValueError
        port = int(port_value)
        if not 1 <= port <= 65535:
            raise ValueError
        if values["auth_method"] not in ("key_file", "private_key", "password"):
            raise ValueError
        if bool(values["host"]) != bool(values["username"]):
            raise ValueError
        stored = DeviceSettings.objects.filter(pk=1).first()
        trusted = bool(stored and stored.trusted_at)
        if values["host"] and trusted:
            credential = {
                "key_file": values["key_file"],
                "private_key": values["private_key"],
                "password": values["password"],
            }[values["auth_method"]]
            if not credential or not (values["known_hosts"] or values["host_keys"]):
                raise ValueError
            if values["host_keys"]:
                validate_host_keys(values["host_keys"], values["host"], port)
                import paramiko

                expected = parse_host_keys(values["host_keys"])
                actual = paramiko.HostKeys(values["known_hosts"])
                lookup = values["host"] if port == 22 else f'[{values["host"]}]:{port}'
                for key in expected.lookup(lookup).values():
                    if not actual.check(lookup, key):
                        raise ValueError
        return ConnectionConfig(**(values | {"port": port, "trusted": trusted}))
    except (TypeError, ValueError, KeyError, OSError):
        raise ValueError("Invalid device connection settings") from None


def get_connection(*, _locked=False):
    if _locked:
        return _get_connection()
    from .trust import trust_lock

    with trust_lock():
        return _get_connection()
