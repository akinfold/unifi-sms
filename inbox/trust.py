import base64
import fcntl
import hashlib
import os
import tempfile
from contextlib import contextmanager, nullcontext
from io import StringIO

import paramiko
from django.conf import settings

from modem.client import parse_host_keys, parse_private_key


def key_fingerprint(key):
    digest = base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode("ascii").rstrip("=")
    return "SHA256:" + digest


def generate_private_key():
    key = paramiko.RSAKey.generate(3072)
    stream = StringIO()
    key.write_private_key(stream)
    return stream.getvalue(), f"{key.get_name()} {key.get_base64()} unifi-sms"


def public_key_for(private_key):
    key = parse_private_key(private_key)
    return f"{key.get_name()} {key.get_base64()} unifi-sms"


@contextmanager
def trust_lock():
    path = settings.DATA_DIR / "device-trust.lock"
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(descriptor, "a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def save_known_host(host, port, key_line, *, locked=False):
    """Atomically pin a confirmed key in the persistent container data directory."""
    lookup = host if port == 22 else f"[{host}]:{port}"
    keys = parse_host_keys(f"{lookup} {key_line}")
    key = next(iter(keys.lookup(lookup).values()))
    path = settings.DATA_DIR / "known_hosts"
    with nullcontext() if locked else trust_lock():
        existing = paramiko.HostKeys()
        if path.exists():
            existing.load(str(path))
        if lookup in existing:
            del existing[lookup]
        existing.add(lookup, key.get_name(), key)
        descriptor, temporary = tempfile.mkstemp(prefix=".known-hosts-", dir=settings.DATA_DIR)
        try:
            os.close(descriptor)
            existing.save(temporary)
            os.chmod(temporary, 0o600)
            with open(temporary, "rb") as stream:
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            directory = os.open(settings.DATA_DIR, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    return key_fingerprint(key)


@contextmanager
def atomic_trust_update():
    """Serialize readers and restore the previous pin if the database commit fails."""
    with trust_lock():
        path = settings.DATA_DIR / "known_hosts"
        previous = path.read_bytes() if path.exists() else None
        try:
            yield
        except BaseException:
            if previous is None:
                path.unlink(missing_ok=True)
            else:
                descriptor, temporary = tempfile.mkstemp(prefix=".known-hosts-rollback-", dir=settings.DATA_DIR)
                try:
                    with os.fdopen(descriptor, "wb") as stream:
                        stream.write(previous)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(temporary, path)
                finally:
                    if os.path.exists(temporary):
                        os.unlink(temporary)
            raise
