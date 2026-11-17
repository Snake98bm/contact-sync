"""Who may use the site.

- People sign in with a passkey (Windows Hello, a phone, a security key). The very first passkey
  needs the one-time setup code from the container log (or from /api/setup-code with the API
  key), so nobody else on the network can claim the site first. More passkeys can be added from
  Settings while signed in. Lost them all? Delete /data/auth.json and restart: a new setup code
  is printed.
- Scripts (and Claude) use the API key from the API_KEY env var, sent as
  `Authorization: Bearer <key>`. It only opens /api/ routes.

Sessions are a signed cookie, valid 30 days.
"""
import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
import time

import store
import webauthn

log = logging.getLogger("auth")
AUTH_PATH = os.path.join(store.DATA_DIR, "auth.json")
COOKIE = "cs_session"
SESSION_DAYS = 30
CHALLENGE_SECONDS = 300
API_KEY = os.environ.get("API_KEY", "").strip()

_lock = threading.Lock()
_failed = {}            # ip -> [timestamps]
_challenges = {}        # challenge -> (purpose, expires)
_setup = {"code": ""}


# ---------- storage ----------

def _load():
    try:
        with open(AUTH_PATH) as f:
            d = json.load(f)
    except (OSError, ValueError):
        d = {}
    d.setdefault("credentials", [])
    return d


def _save(d):
    os.makedirs(os.path.dirname(AUTH_PATH), exist_ok=True)
    tmp = AUTH_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(d, f, indent=1)
    os.chmod(tmp, 0o600)
    os.replace(tmp, AUTH_PATH)


def _init_file():
    d = _load()
    changed = False
    if not d.get("key"):
        d["key"] = secrets.token_hex(32)
        changed = True
    if not d.get("user_id"):
        d["user_id"] = webauthn.b64u(secrets.token_bytes(16))
        changed = True
    if changed:
        _save(d)
    return d


def is_set():
    return bool(_load()["credentials"])


def credentials():
    return [{k: c.get(k) for k in ("id", "name", "created", "last_used")} for c in _load()["credentials"]]


# ---------- setup code ----------

def setup_code():
    """The one-time code that allows the first passkey. Made on demand while none exist."""
    with _lock:
        if is_set():
            _setup["code"] = ""
            return ""
        if not _setup["code"]:
            alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
            _setup["code"] = "".join(secrets.choice(alphabet) for _ in range(8))
            log.warning("No passkey yet. Setup code for the first passkey: %s", _setup["code"])
        return _setup["code"]


def check_setup_code(code):
    want = setup_code()
    got = "".join(ch for ch in (code or "").upper() if ch.isalnum())
    return bool(want) and hmac.compare_digest(got.encode(), want.encode())


# ---------- challenges ----------

def _new_challenge(purpose):
    now = time.time()
    with _lock:
        for c in [c for c, (_, exp) in _challenges.items() if exp < now]:
            _challenges.pop(c, None)
        ch = webauthn.b64u(secrets.token_bytes(32))
        _challenges[ch] = (purpose, now + CHALLENGE_SECONDS)
        return ch


def _take_challenge(client_data_json, purpose):
    """Find the challenge the browser signed, check it's ours and unused, and use it up."""
    try:
        ch = json.loads(client_data_json).get("challenge", "")
    except ValueError:
        raise webauthn.Invalid("bad clientDataJSON") from None
    with _lock:
        got = _challenges.pop(ch, None)
    if not got or got[0] != purpose or got[1] < time.time():
        raise webauthn.Invalid("this sign-in request expired. Reload the page and try again")
    return ch


# ---------- ceremonies ----------

def register_options(rp_id, user_label):
    d = _init_file()
    return {
        "challenge": _new_challenge("register"),
        "rp": {"id": rp_id, "name": "Contact Sync"},
        "user": {"id": d["user_id"], "name": user_label, "displayName": user_label},
        "pubKeyCredParams": [{"type": "public-key", "alg": webauthn.ES256},
                             {"type": "public-key", "alg": webauthn.RS256}],
        "authenticatorSelection": {"residentKey": "preferred", "requireResidentKey": False,
                                   "userVerification": "required"},
        "attestation": "none",
        "timeout": CHALLENGE_SECONDS * 1000,
        "excludeCredentials": [{"type": "public-key", "id": c["id"]} for c in d["credentials"]],
    }


def finish_register(body, origins, rp_id, name):
    """body: {"clientDataJSON", "attestationObject"} (base64url). Returns the stored passkey id."""
    cdj = webauthn.unb64u(body.get("clientDataJSON", ""))
    ch = _take_challenge(cdj, "register")
    new = webauthn.verify_registration(cdj, webauthn.unb64u(body.get("attestationObject", "")),
                                       ch, origins, rp_id)
    with _lock:
        d = _init_file()
        if any(c["id"] == new["id"] for c in d["credentials"]):
            raise webauthn.Invalid("that passkey is already registered")
        d["credentials"].append({**new, "name": (name or "Passkey").strip()[:60] or "Passkey",
                                 "created": time.time(), "last_used": None})
        _save(d)
        _setup["code"] = ""
    log.info("passkey added: %s", name)
    return new["id"]


def login_options(rp_id):
    return {
        "challenge": _new_challenge("login"),
        "rpId": rp_id,
        "allowCredentials": [{"type": "public-key", "id": c["id"]} for c in _load()["credentials"]],
        "userVerification": "required",
        "timeout": CHALLENGE_SECONDS * 1000,
    }


def finish_login(body, origins, rp_id):
    """body: {"id", "clientDataJSON", "authenticatorData", "signature"}. Returns the passkey name."""
    cdj = webauthn.unb64u(body.get("clientDataJSON", ""))
    ch = _take_challenge(cdj, "login")
    with _lock:
        d = _load()
        cred = next((c for c in d["credentials"] if c["id"] == body.get("id")), None)
        if not cred:
            raise webauthn.Invalid("unknown passkey")
        count = webauthn.verify_assertion(cred, cdj, webauthn.unb64u(body.get("authenticatorData", "")),
                                          webauthn.unb64u(body.get("signature", "")), ch, origins, rp_id)
        cred["sign_count"] = count
        cred["last_used"] = time.time()
        _save(d)
    return cred["name"]


def remove_credential(cred_id):
    """Remove a passkey, but never the last one."""
    with _lock:
        d = _load()
        if len(d["credentials"]) <= 1:
            return "You can't remove the last passkey."
        before = len(d["credentials"])
        d["credentials"] = [c for c in d["credentials"] if c["id"] != cred_id]
        if len(d["credentials"]) == before:
            return "No such passkey."
        _save(d)
    log.info("passkey removed")
    return ""


# ---------- rate limit ----------

def too_many_failures(ip):
    now = time.time()
    with _lock:
        recent = [t for t in _failed.get(ip, []) if now - t < 300]
        _failed[ip] = recent
        return len(recent) >= 10


def note_failure(ip):
    with _lock:
        _failed.setdefault(ip, []).append(time.time())


# ---------- sessions ----------

def make_cookie(secure=False):
    d = _init_file()
    exp = int(time.time()) + SESSION_DAYS * 86400
    payload = f"{exp}.{secrets.token_hex(8)}"
    sig = hmac.new(bytes.fromhex(d["key"]), payload.encode(), hashlib.sha256).digest()
    token = payload + "." + base64.urlsafe_b64encode(sig).decode().rstrip("=")
    return (f"{COOKIE}={token}; Path=/; Max-Age={SESSION_DAYS * 86400}; HttpOnly; SameSite=Strict"
            + ("; Secure" if secure else ""))


def clear_cookie(secure=False):
    return f"{COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict" + ("; Secure" if secure else "")


def logged_in(cookie_header):
    d = _load()
    if not d.get("key") or not d["credentials"] or not cookie_header:
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


def logout_everywhere():
    """New signing key: every session cookie stops working."""
    with _lock:
        d = _load()
        d["key"] = secrets.token_hex(32)
        _save(d)


def api_key_ok(auth_header):
    """True for `Authorization: Bearer <API_KEY>`. No key configured = API keys are off."""
    if len(API_KEY) < 20 or not auth_header:
        return False
    scheme, _, value = auth_header.partition(" ")
    return scheme.lower() == "bearer" and hmac.compare_digest(value.strip().encode(), API_KEY.encode())
