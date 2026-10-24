"""Verify an upgrade between local Docker images with a shared disposable data volume."""

import argparse
import json
import secrets
import sys
from uuid import uuid4

from smoke_install import Client, check_administrator, docker, require, wait_healthy


# Run fixture creation and inspection with each image's own Django runtime.
STATE_PROGRAM = r'''
import hashlib
import io
import json
import os
import sys
from datetime import datetime, timezone

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
import django
django.setup()

import paramiko
from django.conf import settings
from django.contrib.auth.models import User
from django.core.serializers.json import DjangoJSONEncoder
from django.db import connection
from inbox.connection import connection_values, decrypt_secret, get_connection
from inbox.models import DeviceSettings, Installation, Message, Receipt, SMTPSettings
from inbox.smtp_config import smtp_values
from inbox.trust import save_known_host
from modem.client import parse_private_key

def digest(value):
    if not isinstance(value, bytes):
        value = json.dumps(value, cls=DjangoJSONEncoder, sort_keys=True).encode()
    return hashlib.sha256(value).hexdigest()

if sys.argv[1] == 'seed':
    key = paramiko.RSAKey.generate(2048)
    private_key = io.StringIO()
    key.write_private_key(private_key)
    key_line = f'{key.get_name()} {key.get_base64()}'
    fingerprint = save_known_host('127.0.0.1', 65534, key_line)
    DeviceSettings.objects.update_or_create(pk=1, defaults={
        'host': '127.0.0.1', 'port': 65534, 'username': 'upgrade-device',
        'auth_method': 'private_key', 'password': 'synthetic-device-password',
        'private_key': private_key.getvalue(), 'public_key': key_line,
        'known_hosts': str(settings.DATA_DIR / 'known_hosts'),
        'host_keys': f'[127.0.0.1]:65534 {key_line}',
        'trusted_at': datetime(2025, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
        'fingerprint': fingerprint, 'model': 'ULTE',
        'name': 'Upgrade test device', 'address': '127.0.0.1',
    })
    SMTPSettings.objects.update_or_create(pk=1, defaults={
        'enabled': True, 'host': '127.0.0.1', 'port': 65533,
        'username': 'upgrade-smtp', 'password': 'synthetic-smtp-password',
        'security': 'starttls', 'from_email': 'sms@example.invalid',
        'recipients': 'first@example.invalid\nsecond@example.invalid',
    })
    for index, text in enumerate(['Unread upgrade message: café 🚀', 'Read multipart message. ' * 12]):
        timestamp = datetime(2025, 1, 2 + index, 3, 4, 5, tzinfo=timezone.utc)
        message = Message.objects.create(
            source='upgrade-fixture', sender=f'+1555000000{index}',
            received_at=timestamp, text=text, read_at=timestamp if index else None,
            concat_ref='42' if index else None, total_parts=2 if index else 1,
            saved_parts=2 if index else 1,
        )
        for part in range(1, message.total_parts + 1):
            Receipt.objects.create(
                fingerprint=digest([index, part]), source=message.source, message=message,
                pdu=f'synthetic-pdu-{index}-{part}', part=part, slot=index * 2 + part,
                cleared_at=timestamp,
            )

models = [User, Installation, Message, Receipt, DeviceSettings, SMTPSettings]
fields = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {
    model._meta.label: [field.attname for field in model._meta.concrete_fields
                        if not (model == User and field.name == 'last_login')]
    for model in models
}
rows = {
    model._meta.label: digest(list(model.objects.order_by('pk').values(*fields[model._meta.label])))
    for model in models
}
device = DeviceSettings.objects.get(pk=1)
assert device.password.startswith('fernet:') and device.private_key.startswith('fernet:')
assert decrypt_secret(device.password) == 'synthetic-device-password'
private_key = decrypt_secret(device.private_key)
assert f'{parse_private_key(private_key).get_name()} {parse_private_key(private_key).get_base64()}' == device.public_key
effective, _ = connection_values()
smtp, _ = smtp_values()
assert get_connection().configured
assert effective['host'] == smtp['host'] == '127.0.0.1'
assert effective['port'] == 65534 and smtp['port'] == 65533
with connection.cursor() as cursor:
    assert cursor.execute('PRAGMA integrity_check').fetchall() == [('ok',)]
print(json.dumps({
    'fields': fields, 'rows': rows,
    'files': {name: digest((settings.DATA_DIR / name).read_bytes())
              for name in ['secret-key', 'known_hosts']},
    'effective_device': digest(effective), 'effective_smtp': digest(smtp),
}))
'''


def state(container, *, seed=False, fields=None):
    args = ['exec', '-i', container, 'python', '-', 'seed' if seed else 'inspect']
    if fields is not None:
        args.append(json.dumps(fields))
    return json.loads(docker(*args, input=STATE_PROGRAM))


def inbox(client):
    response = client.request('/api/messages/')
    require(response.status == 200, f'Administrator API access failed: HTTP {response.status}')
    data = json.loads(response.body)
    require(data['count'] == 2 and data['unread'] == 1, 'Expected two SMS, including one unread SMS')
    return data



def preserves_values(actual, expected):
    # API additions must not hide changes to values exposed by the previous version.
    if type(actual) is not type(expected):
        return False
    if isinstance(expected, dict):
        return all(key in actual and preserves_values(actual[key], value)
                   for key, value in expected.items())
    if isinstance(expected, list):
        return len(actual) == len(expected) and all(
            preserves_values(item, old_item) for item, old_item in zip(actual, expected)
        )
    return actual == expected


def create_container(name, image, volume):
    docker(
        'create', '--pull=never', '--name', name, '--publish', '127.0.0.1::8000',
        '--mount', f'type=volume,source={volume},target=/data',
        '--security-opt', 'no-new-privileges:true', '--cap-drop', 'ALL',
        '--stop-timeout', '45', image,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--previous-image', required=True, help='Locally available previous image')
    parser.add_argument('--image', required=True, help='Locally available candidate image')
    parser.add_argument('--timeout', type=int, default=120, help='Health timeout per startup in seconds')
    args = parser.parse_args()
    require(args.timeout > 0, 'The health timeout must be positive')
    prefix = f'unifi-sms-upgrade-smoke-{uuid4().hex[:12]}'
    previous, candidate, volume = f'{prefix}-previous', f'{prefix}-candidate', f'{prefix}-data'
    created = []
    volume_created = False
    try:
        for image in (args.previous_image, args.image):
            docker('image', 'inspect', image)
        docker('volume', 'create', volume)
        volume_created = True
        create_container(previous, args.previous_image, volume)
        created.append(previous)
        docker('start', previous)
        client = Client(wait_healthy(previous, args.timeout))
        client.request('/').redirect('/setup/')
        credentials = {'username': 'upgrade-admin', 'password': 'Q8!' + secrets.token_urlsafe(18)}
        client.post_form('/setup/', {
            'username': credentials['username'],
            'password1': credentials['password'], 'password2': credentials['password'],
        }).redirect('/')
        client.empty_inbox()
        before = state(previous, seed=True)
        expected_inbox = inbox(client)
        check_administrator(previous)
        print('PASS: previous image created the administrator, SMS, and device and SMTP settings', flush=True)

        docker('stop', '--time', '45', previous, timeout=75)
        create_container(candidate, args.image, volume)
        created.append(candidate)
        docker('start', candidate)
        client.base_url = wait_healthy(candidate, args.timeout)
        after = state(candidate, fields=before['fields'])
        for category in ('rows', 'files', 'effective_device', 'effective_smtp'):
            require(before[category] == after[category], f'Upgrade changed persisted {category}')
        require(preserves_values(inbox(client), expected_inbox), 'SMS or the authenticated session did not survive the upgrade')
        check_administrator(candidate)
        print('PASS: exact SMS, settings, encrypted credentials, host trust, and session survived the upgrade', flush=True)

        fresh = Client(client.base_url)
        fresh.request('/').redirect('/login/')
        fresh.request('/setup/').redirect('/login/')
        fresh.post_form('/login/', credentials).redirect('/')
        require(preserves_values(inbox(fresh), expected_inbox), 'Administrator login or SMS access failed after upgrade')
        for path in ('/', '/settings/device/', '/settings/smtp/'):
            require(fresh.request(path).status == 200, f'Upgraded page failed: {path}')
        print('PASS: existing administrator can sign in; setup stays closed and settings pages open', flush=True)
    except Exception:
        for name in created:
            print(f'Container logs ({name}):', file=sys.stderr)
            try:
                print(docker('logs', '--tail', '100', name), file=sys.stderr)
            except Exception as error:
                print(f'Could not read logs: {error}', file=sys.stderr)
        raise
    finally:
        original_failure = sys.exc_info()[0] is not None
        cleanup_errors = []
        for name in reversed(created):
            try:
                docker('rm', '--force', name)
            except Exception as error:
                cleanup_errors.append(str(error))
        if volume_created:
            try:
                docker('volume', 'rm', volume)
            except Exception as error:
                cleanup_errors.append(str(error))
        if cleanup_errors:
            for error in cleanup_errors:
                print(f'Cleanup failed: {error}', file=sys.stderr)
            if not original_failure:
                raise RuntimeError('Could not remove all temporary upgrade resources')
        else:
            print('Removed temporary upgrade resources.', flush=True)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(f'Upgrade smoke test failed: {error}', file=sys.stderr)
        sys.exit(1)
