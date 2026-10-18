import paramiko
from django.conf import settings
from django.utils import timezone

from .models import DeviceSettings
from .trust import save_known_host

TEST_HOST_KEY = paramiko.RSAKey.generate(1024)


def seed_connection(host="modem.example.invalid", port=22):
    settings.DATA_DIR.mkdir(parents=True, exist_ok=True)
    lookup = host if port == 22 else f"[{host}]:{port}"
    key_line = f"{TEST_HOST_KEY.get_name()} {TEST_HOST_KEY.get_base64()}"
    fingerprint = save_known_host(host, port, key_line)
    return DeviceSettings.objects.update_or_create(pk=1, defaults={
        "host": host, "port": port, "username": "sms", "auth_method": "password",
        "password": "synthetic-device-password", "known_hosts": str(settings.DATA_DIR / "known_hosts"),
        "host_keys": f"{lookup} {key_line}", "trusted_at": timezone.now(), "fingerprint": fingerprint,
    })[0]
