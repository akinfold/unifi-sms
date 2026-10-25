from django.conf import settings
from django.contrib.auth.hashers import check_password
from django.utils.crypto import constant_time_compare, salted_hmac


KEY_DIGEST_PREFIX = "api_key_hmac_sha256$"


def make_api_key_hash(key):
    digest = salted_hmac("inbox.integration-api-key.v1", key, secret=settings.SECRET_KEY, algorithm="sha256")
    return KEY_DIGEST_PREFIX + digest.hexdigest()


def is_api_key_digest(encoded):
    return encoded.startswith(KEY_DIGEST_PREFIX)


def check_api_key(key, encoded):
    if is_api_key_digest(encoded):
        return constant_time_compare(make_api_key_hash(key), encoded)
    return check_password(key, encoded)
