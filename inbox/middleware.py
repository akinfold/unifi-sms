from django.conf import settings

from .proxy import trusted_peer


class TrustedProxyMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if not settings.TRUST_PROXY or not trusted_peer(request.META.get("REMOTE_ADDR", "")):
            request.META.pop("HTTP_X_FORWARDED_PROTO", None)
        else:
            # Require one value set by the trusted ingress, not a client-supplied chain.
            if request.META.get("HTTP_X_FORWARDED_PROTO") not in ("http", "https"):
                request.META.pop("HTTP_X_FORWARDED_PROTO", None)
        return self.get_response(request)


class PrivateResponsesMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        response["Cache-Control"] = "no-store"
        response["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
        )
        return response
