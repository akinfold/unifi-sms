import fcntl
import os
import secrets


def persistent_secret(path):
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(fd, "r+") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        value = stream.read().strip()
        if not value:
            value = secrets.token_urlsafe(48)
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        return value
