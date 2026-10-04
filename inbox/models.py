import uuid

from django.db import models
from django.utils import timezone


class Installation(models.Model):
    id = models.PositiveSmallIntegerField(primary_key=True, default=1)
    completed = models.BooleanField(default=False)


class Message(models.Model):
    source = models.CharField(max_length=64)
    sender = models.CharField(max_length=128)
    received_at = models.DateTimeField(db_index=True)
    imported_at = models.DateTimeField(auto_now_add=True)
    text = models.TextField()
    read_at = models.DateTimeField(null=True)
    concat_ref = models.CharField(max_length=32, null=True)
    total_parts = models.PositiveSmallIntegerField(default=1)
    saved_parts = models.PositiveSmallIntegerField(default=1)

    class Meta:
        ordering = ["-received_at", "-id"]


class Receipt(models.Model):
    fingerprint = models.CharField(max_length=64, unique=True)
    source = models.CharField(max_length=64)
    message = models.ForeignKey(
        Message, null=True, on_delete=models.SET_NULL, related_name="receipts"
    )
    pdu = models.TextField()
    part = models.PositiveSmallIntegerField(default=1)
    slot = models.PositiveIntegerField()
    saved_at = models.DateTimeField(auto_now_add=True)
    cleared_at = models.DateTimeField(null=True)


class SyncStatus(models.Model):
    id = models.PositiveSmallIntegerField(primary_key=True, default=1)
    attempted_at = models.DateTimeField(null=True)
    succeeded_at = models.DateTimeField(null=True)
    state = models.CharField(max_length=32, default="waiting")
    detail = models.CharField(max_length=250, default="Waiting for the first import.")


class DeviceInfo(models.Model):
    source = models.CharField(max_length=64, primary_key=True)
    numbers = models.JSONField(default=list)
    model = models.CharField(max_length=64, blank=True)
    model_attempted_at = models.DateTimeField(null=True)
    model_error = models.BooleanField(default=False)
    requested_at = models.DateTimeField(null=True)
    attempted_at = models.DateTimeField(null=True)
    completed_at = models.DateTimeField(null=True)
    checked_at = models.DateTimeField(null=True)
    last_error = models.CharField(max_length=250, blank=True)


class AccessWindow(models.Model):
    key = models.CharField(max_length=64, primary_key=True)
    started_at = models.DateTimeField()
    attempts = models.PositiveIntegerField(default=0)


class SMTPSettings(models.Model):
    id = models.PositiveSmallIntegerField(primary_key=True, default=1)
    enabled = models.BooleanField(default=False)
    host = models.CharField(max_length=253, blank=True)
    port = models.PositiveIntegerField(default=587)
    username = models.CharField(max_length=254, blank=True)
    password = models.TextField(blank=True)
    security = models.CharField(
        max_length=8,
        choices=[("starttls", "STARTTLS"), ("tls", "TLS"), ("none", "None")],
        default="starttls",
    )
    from_email = models.EmailField(blank=True)
    recipients = models.TextField(blank=True)


class ForwardDelivery(models.Model):
    message = models.OneToOneField(Message, on_delete=models.CASCADE, related_name="forwarding")
    token = models.UUIDField(default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    sent_at = models.DateTimeField(null=True)
    attempts = models.PositiveIntegerField(default=0)
    next_attempt_at = models.DateTimeField(default=timezone.now)
    last_error = models.CharField(max_length=250, blank=True)
    recipients = models.JSONField(default=list)
    delivered_to = models.JSONField(default=list)

    class Meta:
        indexes = [models.Index(fields=["sent_at", "next_attempt_at"], name="forward_due_idx")]
