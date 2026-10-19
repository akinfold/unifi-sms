"""Verify first-run setup over HTTP against a local Docker image."""

import argparse
from dataclasses import dataclass
from html.parser import HTMLParser
from http.cookiejar import CookieJar
import json
import secrets
import string
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import (
    HTTPCookieProcessor,
    HTTPRedirectHandler,
    ProxyHandler,
    Request,
    build_opener,
)
from uuid import uuid4


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def docker(*args, timeout=60):
    result = subprocess.run(
        ['docker', *args], capture_output=True, text=True, timeout=timeout
    )
    if result.returncode:
        raise RuntimeError(f"docker {args[0]} failed: {result.stderr.strip()}")
    output = result.stdout + (result.stderr if args[0] == 'logs' else '')
    return output.strip()


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class FormToken(HTMLParser):
    token = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'input' and attrs.get('name') == 'csrfmiddlewaretoken':
            self.token = attrs.get('value')


@dataclass
class Response:
    status: int
    location: str
    body: str

    def token(self):
        require(self.status == 200, f'Expected a form, received HTTP {self.status}')
        parser = FormToken()
        parser.feed(self.body)
        require(parser.token, 'The form has no CSRF token')
        return parser.token

    def redirect(self, location):
        require(
            self.status == 302 and self.location == location,
            f'Expected redirect to {location}, received HTTP {self.status} to {self.location}',
        )


class Client:
    def __init__(self, base_url):
        self.base_url = base_url
        self.opener = build_opener(ProxyHandler({}), HTTPCookieProcessor(CookieJar()), NoRedirect())

    def request(self, path, fields=None):
        data = None if fields is None else urlencode(fields).encode()
        request = Request(self.base_url + path, data=data)
        if fields is not None:
            request.add_header('Content-Type', 'application/x-www-form-urlencoded')
            request.add_header('Origin', self.base_url)
            request.add_header('Referer', self.base_url + path)
        try:
            response = self.opener.open(request, timeout=5)
        except HTTPError as error:
            response = error
        with response:
            return Response(
                response.code, response.headers.get('Location', ''), response.read().decode()
            )

    def post_form(self, path, fields):
        token = self.request(path).token()
        return self.request(path, {**fields, 'csrfmiddlewaretoken': token})

    def empty_inbox(self):
        response = self.request('/api/messages/')
        require(response.status == 200, f'Administrator API access failed: HTTP {response.status}')
        data = json.loads(response.body)
        require(data['messages'] == [] and data['count'] == 0, 'The fresh inbox is not empty')


def wait_healthy(container, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        details = json.loads(docker('inspect', container))[0]
        state = details['State']
        require(state['Running'], f"Container stopped with exit code {state['ExitCode']}")
        port = details['NetworkSettings']['Ports']['8000/tcp'][0]['HostPort']
        base_url = f'http://127.0.0.1:{port}'
        if state.get('Health', {}).get('Status') == 'healthy':
            try:
                response = Client(base_url).request('/healthz/')
                if response.status == 200 and response.body.strip() == 'ok':
                    return base_url
            except (OSError, URLError):
                pass
        time.sleep(1)
    raise RuntimeError(f'Container did not become healthy within {timeout} seconds')


def check_administrator(container):
    query = """
import json
import os
import django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()
from django.contrib.auth.models import User
from inbox.models import Installation
users = list(User.objects.values('is_active', 'is_staff', 'is_superuser'))
print(json.dumps({'users': users, 'setup_completed': Installation.objects.get(pk=1).completed}))
"""
    data = json.loads(docker('exec', container, 'python', '-c', query))
    require(
        data == {
            'users': [{'is_active': True, 'is_staff': True, 'is_superuser': True}],
            'setup_completed': True,
        },
        'Expected one active administrator and completed installation',
    )


def verify_installation(container, timeout):
    client = Client(wait_healthy(container, timeout))
    client.request('/').redirect('/setup/')
    require(client.request('/api/messages/').status == 403, 'Anonymous API access was allowed')
    setup = client.request('/setup/')
    setup.token()
    require('setup_token' not in setup.body, 'Setup unexpectedly requires a setup token')
    require('at least 8 characters' in setup.body, 'The minimum password length is not shown')
    username = 'smoke-admin'
    password = 'Q8!' + ''.join(secrets.choice(string.ascii_letters) for _ in range(5))
    fields = {'username': username, 'password1': password, 'password2': password}
    require(client.request('/setup/', fields).status == 403, 'Setup accepted a missing CSRF token')
    weak = client.post_form('/setup/', {**fields, 'password1': password[:7], 'password2': password[:7]})
    require(weak.status == 200 and 'too short' in weak.body, 'Setup accepted a short password')
    client.request('/').redirect('/setup/')
    client.post_form('/setup/', fields).redirect('/')
    client.empty_inbox()
    page = client.request('/settings/device/')
    require(page.status == 200 and username in page.body, 'Device onboarding did not open')
    check_administrator(container)
    print('PASS: clean startup, CSRF, password validation, administrator creation, and device onboarding', flush=True)

    client.request('/logout/', {'csrfmiddlewaretoken': page.token()}).redirect('/login/')
    require(client.request('/api/messages/').status == 403, 'Logout left API access enabled')
    client.request('/setup/').redirect('/login/')
    token = client.request('/login/').token()
    client.request('/setup/', {**fields, 'csrfmiddlewaretoken': token}).redirect('/login/')
    credentials = {'username': username, 'password': password}
    bad_login = client.post_form('/login/', {**credentials, 'password': password + '-wrong'})
    require(bad_login.status == 200, 'Invalid login did not show the login form')
    require(client.request('/api/messages/').status == 403, 'Invalid credentials authenticated')
    client.post_form('/login/', credentials).redirect('/')
    client.empty_inbox()
    print('PASS: setup closes, logout works, invalid login is rejected, and valid login succeeds', flush=True)

    docker('restart', container, timeout=75)
    client.base_url = wait_healthy(container, timeout)
    client.empty_inbox()
    fresh_client = Client(client.base_url)
    fresh_client.request('/').redirect('/login/')
    fresh_client.request('/setup/').redirect('/login/')
    fresh_client.post_form('/login/', credentials).redirect('/')
    fresh_client.empty_inbox()
    check_administrator(container)
    print('PASS: administrator, setup closure, and session persist across container restart', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', required=True, help='Locally available image to test; never pulled')
    parser.add_argument('--timeout', type=int, default=120, help='Health timeout per startup in seconds')
    args = parser.parse_args()
    require(args.timeout > 0, 'The health timeout must be positive')
    name = f'unifi-sms-install-smoke-{uuid4().hex[:12]}'
    volume = f'{name}-data'
    volume_created = container_created = False
    try:
        docker('image', 'inspect', args.image)
        docker('volume', 'create', volume)
        volume_created = True
        docker(
            'create', '--pull=never', '--name', name,
            '--publish', '127.0.0.1::8000',
            '--mount', f'type=volume,source={volume},target=/data',
            '--security-opt', 'no-new-privileges:true', '--cap-drop', 'ALL',
            '--stop-timeout', '45', args.image,
        )
        container_created = True
        docker('start', name)
        verify_installation(name, args.timeout)
    except Exception:
        if container_created:
            print('Container logs:', file=sys.stderr)
            print(docker('logs', '--tail', '100', name), file=sys.stderr)
        raise
    finally:
        try:
            if container_created:
                docker('rm', '--force', name)
        finally:
            if volume_created:
                docker('volume', 'rm', volume)
        print('Removed temporary installation resources.', flush=True)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(f'Fresh installation smoke test failed: {error}', file=sys.stderr)
        sys.exit(1)
