import inbox.fields
import uuid
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('inbox', '0009_encrypt_smtp_password'),
    ]

    operations = [
        migrations.CreateModel(
            name='OutboundProfile',
            fields=[
                ('id', models.PositiveSmallIntegerField(default=1, primary_key=True, serialize=False)),
                ('revision', models.UUIDField(default=uuid.uuid4, editable=False)),
                ('connection_id', models.CharField(max_length=64)),
                ('sim', models.CharField(max_length=64)),
                ('profile', models.CharField(max_length=32)),
            ],
        ),
        migrations.CreateModel(
            name='OutboundSetup',
            fields=[
                ('id', models.PositiveSmallIntegerField(default=1, primary_key=True, serialize=False)),
                ('run', models.UUIDField(default=uuid.uuid4, editable=False)),
                ('connection_id', models.CharField(blank=True, max_length=64)),
                ('state', models.CharField(default='idle', max_length=16)),
                ('detail', models.CharField(blank=True, max_length=250)),
                ('sim', models.CharField(blank=True, max_length=64)),
                ('detected_number', models.CharField(blank=True, max_length=16)),
                ('phone', models.CharField(blank=True, max_length=16)),
                ('profiles', models.JSONField(default=list)),
                ('attempts', models.JSONField(default=list)),
                ('deadline', models.DateTimeField(null=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
        ),
        migrations.AddField(
            model_name='integrationsettings',
            name='mcp_send_enabled',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='integrationsettings',
            name='rest_send_enabled',
            field=models.BooleanField(default=False),
        ),
        migrations.CreateModel(
            name='OutboundMessage',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('owner', models.CharField(max_length=128)),
                ('request_key', models.CharField(max_length=128)),
                ('recipient', models.CharField(max_length=16)),
                ('text', inbox.fields.EncryptedTextField(blank=True)),
                ('text_digest', models.CharField(max_length=64)),
                ('profile_revision', models.UUIDField()),
                ('status', models.CharField(default='queued', max_length=16)),
                ('detail', models.CharField(blank=True, max_length=250)),
                ('modem_ref', models.CharField(blank=True, max_length=64)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'indexes': [models.Index(fields=['status', 'created_at'], name='outbound_queue_idx')],
                'constraints': [models.UniqueConstraint(fields=('owner', 'request_key'), name='outbound_request_unique')],
            },
        ),
    ]
