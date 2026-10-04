import signal
import threading
import time
from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import close_old_connections
from inbox.sync import refresh_phone_once, sync_once
from inbox.forwarding import forward_pending


class Command(BaseCommand):
    help = "Import SMS, clear archived modem slots, and forward queued messages by SMTP."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true")

    def handle(self, *args, **options):
        stop = threading.Event()
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: stop.set())
        next_import = 0
        while not stop.is_set():
            close_old_connections()
            refresh_phone_once()
            if time.monotonic() >= next_import:
                sync_once()
                forward_pending(stop=stop)
                next_import = time.monotonic() + settings.POLL_INTERVAL
            if options["once"]:
                return
            stop.wait(1)
