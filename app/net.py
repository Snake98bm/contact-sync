"""Tiny HTTP client on the standard library.

WRITE GUARD: besides reads and OAuth sign-in/token calls, the only requests this app may send are
the contact create/update/delete calls listed in WRITE_PATTERNS, and only from code running inside
`with net.writes_allowed():` (writer.py, which also checks the write mode and saves a copy first).
Anything else raises before it leaves the container.
"""
import contextlib
import json
import re
import threading
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


WRITE_PATTERNS = [
    ("POST", r"https://graph\.microsoft\.com/v1\.0/me/contacts"),
    ("PATCH", r"https://graph\.microsoft\.com/v1\.0/me/contacts/[^/?]+"),
    ("DELETE", r"https://graph\.microsoft\.com/v1\.0/me/contacts/[^/?]+"),
    ("POST", r"https://people\.googleapis\.com/v1/people:createContact(\?[^/]*)?"),
    ("PATCH", r"https://people\.googleapis\.com/v1/people/[A-Za-z0-9_-]+:updateContact\?[^/]*"),
    ("DELETE", r"https://people\.googleapis\.com/v1/people/[A-Za-z0-9_-]+:deleteContact"),
    # contact photos: upload to Outlook / Google
    ("PUT", r"https://graph\.microsoft\.com/v1\.0/me/contacts/[^/?]+/photo/\$value"),
    ("PATCH", r"https://people\.googleapis\.com/v1/people/[A-Za-z0-9_-]+:updateContactPhoto"),
    # deleting a Google label only with deleteContacts=false (contacts that are in no other group are
    # still removed by Google, so writer.delete_label first adds every member to My Contacts)
    ("DELETE", r"https://people\.googleapis\.com/v1/contactGroups/[A-Za-z0-9_-]+\?deleteContacts=false"),
    ("POST", r"https://people\.googleapis\.com/v1/contactGroups/myContacts/members:modify"),
]
_write = threading.local()


@contextlib.contextmanager
def writes_allowed():
    """Contact writes are only possible inside this block, on this thread."""
    _write.on = True
    try:
        yield
    finally:
        _write.on = False


# Pacing per service, shared by every worker: a small gap between requests that grows when the service
# answers 429 (too many requests) and shrinks back as requests go through.
MIN_GAP = {"people.googleapis.com": 0.1, "graph.microsoft.com": 0.05}
_pace, _pace_lock = {}, threading.Lock()
# Google asks for changes to one user's contacts to be sent one at a time ("mutate requests for the same
# user should be sent sequentially"); reads, and Outlook, can go in parallel.
_google_writes = threading.Lock()


def _host(url):
    return urllib.parse.urlsplit(url).hostname or ""


def _wait_turn(url):
    host = _host(url)
    with _pace_lock:
        p = _pace.setdefault(host, [MIN_GAP.get(host, 0.0), 0.0])
        now = time.monotonic()
        start = max(now, p[1])
        p[1] = start + p[0]
    if start > now:
        time.sleep(start - now)


def _paced(url, ok):
    host = _host(url)
    with _pace_lock:
        p = _pace.setdefault(host, [MIN_GAP.get(host, 0.0), 0.0])
        p[0] = max(MIN_GAP.get(host, 0.0), p[0] * 0.95) if ok else min(max(p[0] * 2, 0.5), 5.0)


def _serial(method, url):
    return _google_writes if method != "GET" and _host(url) == "people.googleapis.com" else contextlib.nullcontext()


def check_allowed(method, url):
    if method == "GET":
        return
    if method == "POST" and url.startswith(ALLOWED_POST_PREFIXES):
        return
    if getattr(_write, "on", False) and any(m == method and re.fullmatch(p, url) for m, p in WRITE_PATTERNS):
        return
    raise WriteBlocked(f"write refused: {method} {url.split('?')[0]}")


def request(method, url, *, form=None, body=None, headers=None, timeout=60, retries=4):
    """Send a request and return the parsed JSON (or {} for an empty body).
    Retries 429 (always: the server did nothing) with backoff, honouring Retry-After. 5xx and
    network errors are retried only for requests that are safe to repeat: never for a POST that
    creates something, because the first one may have worked and a repeat would make a duplicate."""
    check_allowed(method, url)
    repeatable = method != "POST" or url.startswith(ALLOWED_POST_PREFIXES)
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
            with _serial(method, url):
                _wait_turn(url)
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    raw = r.read().decode("utf-8", "replace")
            _paced(url, True)
            return json.loads(raw) if raw.strip() else {}
        except urllib.error.HTTPError as e:
            text = e.read().decode("utf-8", "replace")
            if e.code == 429:
                _paced(url, False)
            if (e.code == 429 or (e.code >= 500 and repeatable)) and attempt < retries:
                wait = e.headers.get("Retry-After")
                time.sleep(min(float(wait), 60) if wait and wait.isdigit() else 2 ** attempt * 2)
                continue
            raise HttpError(e.code, text, url) from None
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if repeatable and attempt < retries:
                time.sleep(2 ** attempt * 2)
                continue
            raise


def request_bytes(method, url, *, data=None, content_type=None, headers=None, timeout=60, retries=2):
    """Like request(), for images: returns the body as bytes, or None for 404 (no photo).
    Same write guard; GETs are retried on network errors and 5xx."""
    check_allowed(method, url)
    hdrs = {"User-Agent": USER_AGENT}
    if content_type:
        hdrs["Content-Type"] = content_type
    hdrs.update(headers or {})
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
        try:
            with _serial(method, url):
                _wait_turn(url)
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    body = r.read()
            _paced(url, True)
            return body
        except urllib.error.HTTPError as e:
            text = e.read().decode("utf-8", "replace")
            if e.code == 404:
                return None
            if e.code == 429:
                _paced(url, False)
            if (e.code == 429 or (e.code >= 500 and method == "GET")) and attempt < retries:
                time.sleep(2 ** attempt * 2)
                continue
            raise HttpError(e.code, text, url) from None
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if method == "GET" and attempt < retries:
                time.sleep(2 ** attempt * 2)
                continue
            raise
