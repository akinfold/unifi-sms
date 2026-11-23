import signal
import threading
import time
from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import close_old_connections
from inbox.connection import queued_import_at
from inbox.models import SyncStatus
from inbox.sync import refresh_phone_once, sync_once
from inbox.forwarding import forward_pending
from inbox.webhooks import deliver_webhooks


class Command(BaseCommand):
    help = "Import SMS, clear archived modem slots, and forward queued messages by email and webhook."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true")

    def handle(self, *args, **options):
        stop = threading.Event()
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: stop.set())
        next_import = 0
        while not stop.is_set():
            close_old_connections()
            import_due = time.monotonic() >= next_import
            if not import_due:
                attempted_at = SyncStatus.objects.filter(pk=1).values_list("attempted_at", flat=True).first()
                import_due = queued_import_at(attempted_at) is not None
            if import_due:
                sync_once()
            refresh_phone_once()
            if import_due:
                forward_pending(stop=stop)
                deliver_webhooks(stop=stop)
                next_import = time.monotonic() + settings.POLL_INTERVAL
            if options["once"]:
                return
            stop.wait(1)
