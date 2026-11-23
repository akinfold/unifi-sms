from pathlib import Path
from tempfile import TemporaryDirectory

from django.contrib.auth.models import User
from django.contrib.staticfiles.storage import staticfiles_storage
from django.core.management import call_command
from django.test import TestCase

from .test_helpers import seed_connection


class FaviconTests(TestCase):
    def test_favicon_is_served_and_linked_on_public_and_archive_pages(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            with self.settings(DEBUG=False, DATA_DIR=root / "data", STATIC_ROOT=root / "static"):
                call_command("collectstatic", interactive=False, verbosity=0)
                url = staticfiles_storage.url("inbox/favicon.svg")
                self.assertRegex(url, r"^/static/inbox/favicon\.[0-9a-f]{12}\.svg$")
                link = f'<link rel="icon" type="image/svg+xml" sizes="any" href="{url}">'

                icon = self.client.get(url)
                self.assertEqual(icon.headers["Content-Type"], "image/svg+xml")
                self.assertContains(icon, '<svg xmlns="http://www.w3.org/2000/svg"')
                self.assertContains(self.client.get("/setup/"), link, count=1, html=True)

                admin = User.objects.create_superuser("administrator")
                self.assertContains(self.client.get("/login/"), link, count=1, html=True)

                seed_connection()
                self.client.force_login(admin)
                self.assertContains(self.client.get("/"), link, count=1, html=True)
