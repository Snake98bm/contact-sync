"""Two ways in:
- People: a website password chosen on the first visit, then a 30-day login cookie.
  Stored hashed in /data/auth.json. Forgot it? Delete that file and restart the container.
- Scripts (and Claude): an API key from the API_KEY env var, sent as `Authorization: Bearer <key>`.
  It only opens /api/ routes; it can't change the password or sign the accounts in or out."""
import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time

import store

AUTH_PATH = os.path.join(store.DATA_DIR, "auth.json")
COOKIE = "cs_session"
SESSION_DAYS = 30
ITERATIONS = 300_000
MIN_LEN = 8
API_KEY = os.environ.get("API_KEY", "").strip()

_lock = threading.Lock()
_failed = {}            # ip -> [timestamps]


def _load():
    try:
        with open(AUTH_PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _save(d):
    os.makedirs(os.path.dirname(AUTH_PATH), exist_ok=True)
    tmp = AUTH_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(d, f)
    os.chmod(tmp, 0o600)
    os.replace(tmp, AUTH_PATH)


def is_set():
    d = _load()
    return bool(d and d.get("hash"))


def _hash(password, salt):
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, ITERATIONS)


def set_password(password):
    """Set or change the password. Also rotates the signing key, which logs out every other browser."""
    salt = secrets.token_bytes(16)
    _save({"salt": salt.hex(), "hash": _hash(password, salt).hex(), "key": secrets.token_hex(32),
           "set": time.time()})


def check_password(password):
    d = _load()
    if not d:
        return False
    return hmac.compare_digest(_hash(password, bytes.fromhex(d["salt"])).hex(), d["hash"])


def too_many_failures(ip):
    now = time.time()
    with _lock:
        recent = [t for t in _failed.get(ip, []) if now - t < 300]
        _failed[ip] = recent
        return len(recent) >= 5


def note_failure(ip):
    with _lock:
        _failed.setdefault(ip, []).append(time.time())


def make_cookie():
    d = _load()
    exp = int(time.time()) + SESSION_DAYS * 86400
    payload = f"{exp}.{secrets.token_hex(8)}"
    sig = hmac.new(bytes.fromhex(d["key"]), payload.encode(), hashlib.sha256).digest()
    token = payload + "." + base64.urlsafe_b64encode(sig).decode().rstrip("=")
    return f"{COOKIE}={token}; Path=/; Max-Age={SESSION_DAYS * 86400}; HttpOnly; SameSite=Strict"


def clear_cookie():
    return f"{COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict"


def logged_in(cookie_header):
    d = _load()
    if not d or not cookie_header:
        return False
    token = None
    for part in cookie_header.split(";"):
        k, _, v = part.strip().partition("=")
        if k == COOKIE:
            token = v
    if not token or token.count(".") != 2:
        return False
    exp, nonce, sig = token.split(".")
    want = base64.urlsafe_b64encode(hmac.new(bytes.fromhex(d["key"]), f"{exp}.{nonce}".encode(),
                                             hashlib.sha256).digest()).decode().rstrip("=")
    if not hmac.compare_digest(sig, want):
        return False
    try:
        return int(exp) > time.time()
    except ValueError:
        return False


def api_key_ok(auth_header):
    """True for `Authorization: Bearer <API_KEY>`. No key configured = API keys are off."""
    if len(API_KEY) < 20 or not auth_header:
        return False
    scheme, _, value = auth_header.partition(" ")
    return scheme.lower() == "bearer" and hmac.compare_digest(value.strip().encode(), API_KEY.encode())
