"""Run one webhook request in a process that the worker can terminate."""

import json
import ssl
import sys
import urllib.error
import urllib.request


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, new_url):
        # Redirects can expose the secret to another host.
        return None


def post(values):
    request = urllib.request.Request(
        values["url"], data=values["body"].encode("utf-8"),
        headers=values["headers"], method="POST",
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirectHandler())
    try:
        with opener.open(request, timeout=values["timeout"]) as response:
            return {"status": response.getcode()}
    except urllib.error.HTTPError as error:
        error.close()
        return {"status": error.code}
    except Exception as error:
        reason = getattr(error, "reason", error)
        if isinstance(reason, ssl.SSLError):
            return {"error": "tls"}
        if isinstance(reason, TimeoutError):
            return {"error": "timeout"}
        return {"error": "network"}


if __name__ == "__main__":
    try:
        result = post(json.load(sys.stdin))
    except Exception:
        result = {"error": "network"}
    sys.stdout.write(json.dumps(result))
