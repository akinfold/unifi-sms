from django.db import models


class EncryptedTextField(models.TextField):
    """Encrypt values in the database and return plaintext to application code."""

    def from_db_value(self, value, expression, connection):
        from .connection import decrypt_secret

        return decrypt_secret(value) if value is not None else None

    def get_prep_value(self, value):
        from .connection import encrypt_secret

        value = super().get_prep_value(value)
        return encrypt_secret(value) if value is not None else None
