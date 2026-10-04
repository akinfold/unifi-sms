from django.core.management.base import BaseCommand
from inbox.views import initialized


class Command(BaseCommand):
    help = "Show setup instructions before the first administrator is created."

    def handle(self, *args, **options):
        if not initialized():
            self.stdout.write("Open /setup/ to create the administrator.")
