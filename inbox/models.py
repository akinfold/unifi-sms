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


class DeviceSettings(models.Model):
    id = models.PositiveSmallIntegerField(primary_key=True, default=1)
    host = models.CharField(max_length=253, blank=True)
    port = models.PositiveIntegerField(default=22)
    username = models.CharField(max_length=254, blank=True)
    auth_method = models.CharField(
        max_length=16,
        choices=[("key_file", "Key file"), ("private_key", "Private key"), ("password", "Password")],
        default="key_file",
    )
    key_file = models.CharField(max_length=1024, default="/run/secrets/lte_key")
    known_hosts = models.CharField(max_length=1024, default="/run/secrets/known_hosts")
    host_keys = models.TextField(blank=True)
    password = models.TextField(blank=True)
    private_key = models.TextField(blank=True)

    trusted_at = models.DateTimeField(null=True)
    fingerprint = models.CharField(max_length=100, blank=True)
    model = models.CharField(max_length=64, blank=True)
    name = models.CharField(max_length=128, blank=True)
    address = models.CharField(max_length=64, blank=True)
    public_key = models.TextField(blank=True)

    def save(self, *args, **kwargs):
        from .connection import encrypt_secret, is_encrypted_secret

        for field in ("password", "private_key"):
            value = getattr(self, field)
            if value and not is_encrypted_secret(value):
                setattr(self, field, encrypt_secret(value))
        super().save(*args, **kwargs)


class DeviceSetup(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.OneToOneField("auth.User", on_delete=models.CASCADE)
    revision = models.UUIDField(default=uuid.uuid4, editable=False)
    host = models.CharField(max_length=253)
    port = models.PositiveIntegerField(default=22)
    username = models.CharField(max_length=128, blank=True)
    auth_method = models.CharField(max_length=16, default="password")
    key_source = models.CharField(max_length=16, default="paste")
    password = models.TextField(blank=True)
    private_key = models.TextField(blank=True)
    public_key = models.TextField(blank=True)
    probe = models.JSONField(default=dict)
    probe_id = models.UUIDField(null=True)
    checked_at = models.DateTimeField(null=True)
    expires_at = models.DateTimeField()

    def save(self, *args, **kwargs):
        from .connection import encrypt_secret, is_encrypted_secret

        for field in ("password", "private_key"):
            value = getattr(self, field)
            if value and not is_encrypted_secret(value):
                setattr(self, field, encrypt_secret(value))
        super().save(*args, **kwargs)


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


class IntegrationSettings(models.Model):
    id = models.PositiveSmallIntegerField(primary_key=True, default=1)
    rest_enabled = models.BooleanField(default=False)
    rest_key_hash = models.TextField(blank=True)
    rest_key_suffix = models.CharField(max_length=4, blank=True)
    mcp_enabled = models.BooleanField(default=False)
    mcp_key_hash = models.TextField(blank=True)
    mcp_key_suffix = models.CharField(max_length=4, blank=True)


class WebhookSettings(models.Model):
    id = models.PositiveSmallIntegerField(primary_key=True, default=1)
    enabled = models.BooleanField(default=False)
    url = models.CharField(max_length=2048, blank=True)
    secret = models.TextField(blank=True)
    secret_header = models.CharField(max_length=128, default="X-Webhook-Secret", blank=True)

    def save(self, *args, **kwargs):
        from .connection import encrypt_secret, is_encrypted_secret

        if self.secret and not is_encrypted_secret(self.secret):
            self.secret = encrypt_secret(self.secret)
        super().save(*args, **kwargs)


class WebhookDelivery(models.Model):
    message = models.OneToOneField(Message, on_delete=models.CASCADE, related_name="webhook")
    token = models.UUIDField(default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    sent_at = models.DateTimeField(null=True)
    attempts = models.PositiveIntegerField(default=0)
    next_attempt_at = models.DateTimeField(default=timezone.now)
    last_error = models.CharField(max_length=250, blank=True)
    payload = models.JSONField(default=dict)

    class Meta:
        indexes = [models.Index(fields=["sent_at", "next_attempt_at"], name="webhook_due_idx")]
