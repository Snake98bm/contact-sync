"""Tiny HTTP client on the standard library.

READ-ONLY GUARD: until the write phase exists, the only non-GET requests this app may send are
OAuth sign-in / token calls. Anything else raises before it leaves the container, so a bug cannot
change a contact.
"""
import json
import time
import urllib.error
import urllib.parse
import urllib.request

from version import VERSION

USER_AGENT = f"contact-sync/{VERSION}"

# POSTs that are allowed: signing in and refreshing tokens. Nothing that touches contacts.
ALLOWED_POST_PREFIXES = (
    "https://login.microsoftonline.com/",
    "https://oauth2.googleapis.com/token",
)


class HttpError(Exception):
    def __init__(self, status, body, url):
        self.status, self.body, self.url = status, body, url
        super().__init__(f"HTTP {status} from {urllib.parse.urlsplit(url).netloc}: {body[:300]}")

    def json(self):
        try:
            return json.loads(self.body)
        except ValueError:
            return {}


class WriteBlocked(Exception):
    pass


def check_allowed(method, url):
    if method == "GET":
        return
    if method == "POST" and url.startswith(ALLOWED_POST_PREFIXES):
        return
    raise WriteBlocked(f"read-only build: refused {method} {url.split('?')[0]}")


def request(method, url, *, form=None, body=None, headers=None, timeout=60, retries=4):
    """Send a request and return the parsed JSON (or {} for an empty body).
    Retries 429 and 5xx with backoff, honouring Retry-After."""
    check_allowed(method, url)
    data = None
    hdrs = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        hdrs["Content-Type"] = "application/x-www-form-urlencoded"
    elif body is not None:
        data = json.dumps(body).encode()
        hdrs["Content-Type"] = "application/json"
    hdrs.update(headers or {})
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read().decode("utf-8", "replace")
                return json.loads(raw) if raw.strip() else {}
        except urllib.error.HTTPError as e:
            text = e.read().decode("utf-8", "replace")
            if (e.code == 429 or e.code >= 500) and attempt < retries:
                wait = e.headers.get("Retry-After")
                time.sleep(min(float(wait), 60) if wait and wait.isdigit() else 2 ** attempt * 2)
                continue
            raise HttpError(e.code, text, url) from None
        except urllib.error.URLError:
            if attempt < retries:
                time.sleep(2 ** attempt * 2)
                continue
            raise
