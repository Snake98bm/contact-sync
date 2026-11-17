"""Google Contacts through the People API.

Sign-in uses Google's "Desktop app" client with a loopback redirect. The container can't receive
that redirect, so the page gives a link; after signing in, the browser lands on an http://localhost
address that doesn't load, and the person pastes that address back into the page.
"""
import base64
import hashlib
import json
import logging
import os
import re
import secrets
import threading
import time
import urllib.parse

import net
import store

log = logging.getLogger("google")

SCOPE = "https://www.googleapis.com/auth/contacts"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
PEOPLE = "https://people.googleapis.com/v1"
REDIRECT = "http://localhost"
PERSON_FIELDS = ",".join([
    "addresses", "biographies", "birthdays", "calendarUrls", "clientData", "emailAddresses", "events",
    "externalIds", "imClients", "interests", "locations", "memberships", "metadata", "miscKeywords",
    "names", "nicknames", "occupations", "organizations", "phoneNumbers", "photos", "relations",
    "sipAddresses", "urls", "userDefined"])


def _client():
    """Client id/secret from GOOGLE_CLIENT_ID + GOOGLE_CLIENT_SECRET, or the whole downloaded
    client JSON in GOOGLE_CLIENT_JSON."""
    cid = os.environ.get("GOOGLE_CLIENT_ID", "").strip()
    sec = os.environ.get("GOOGLE_CLIENT_SECRET", "").strip()
    if not cid and os.environ.get("GOOGLE_CLIENT_JSON", "").strip():
        try:
            j = json.loads(os.environ["GOOGLE_CLIENT_JSON"])
            j = j.get("installed") or j.get("web") or {}
            cid, sec = j.get("client_id", ""), j.get("client_secret", "")
        except ValueError:
            log.error("GOOGLE_CLIENT_JSON is not valid JSON")
    return cid, sec


def configured():
    return all(_client())


_pending = {}        # state -> code_verifier, kept in memory (a restart just means "start again")


def start_login():
    """Return the Google sign-in URL (with PKCE)."""
    cid, _ = _client()
    state = secrets.token_urlsafe(16)
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    _pending.clear()
    _pending[state] = verifier
    return AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": cid, "redirect_uri": REDIRECT, "response_type": "code", "scope": SCOPE,
        "access_type": "offline", "prompt": "consent", "state": state,
        "code_challenge": challenge, "code_challenge_method": "S256"})


def parse_pasted(text):
    """Pull (code, state, error) out of the address the browser landed on. Accepts the full URL
    or just the query part."""
    text = (text or "").strip()
    query = urllib.parse.urlsplit(text).query if "://" in text else text.lstrip("?/")
    qs = urllib.parse.parse_qs(query)
    first = lambda k: (qs.get(k) or [""])[0]
    return first("code"), first("state"), first("error")


def finish_login(pasted):
    """Exchange the pasted address for tokens. Returns an error message, or '' on success."""
    code, state, error = parse_pasted(pasted)
    if error:
        return f"Google said: {error}"
    if not code:
        return "That address has no code in it. Copy the whole address from the browser's address bar."
    verifier = _pending.get(state)
    if not verifier:
        return "That sign-in link is old (or the container restarted). Click Sign in to Google again."
    cid, sec = _client()
    try:
        tok = net.request("POST", TOKEN_URL, form={
            "code": code, "client_id": cid, "client_secret": sec, "redirect_uri": REDIRECT,
            "grant_type": "authorization_code", "code_verifier": verifier})
    except net.HttpError as e:
        return "Google refused the code: " + (e.json().get("error_description") or str(e))[:200]
    _pending.clear()
    if not tok.get("refresh_token"):
        return "Google didn't return a refresh token. Remove contact-sync's access in your Google account and try again."
    if SCOPE not in tok.get("scope", ""):
        return "The contacts permission wasn't granted. Sign in again and tick the contacts box."
    _save(tok, signed_in=time.time())
    try:
        me = get(f"{PEOPLE}/people/me?personFields=emailAddresses")
        account = next((e["value"] for e in me.get("emailAddresses", [])), None)
        store.save_token("google", {**store.load_tokens()["google"], "account": account})
    except Exception as e:
        log.warning("signed in, but reading the account name failed: %s", e)
    log.info("Google sign-in complete")
    return ""


def _save(tok, signed_in=None):
    old = store.load_tokens().get("google", {})
    store.save_token("google", {
        "access_token": tok["access_token"],
        "refresh_token": tok.get("refresh_token") or old.get("refresh_token"),
        "expires": time.time() + int(tok.get("expires_in", 3600)) - 120,
        "account": old.get("account"), "signed_in": signed_in or old.get("signed_in")})


_refresh_lock = threading.Lock()


def access_token():
    with _refresh_lock:
        t = store.load_tokens().get("google")
        if not t or not t.get("refresh_token"):
            raise RuntimeError("Not signed in to Google")
        if t.get("expires", 0) > time.time():
            return t["access_token"]
        cid, sec = _client()
        try:
            tok = net.request("POST", TOKEN_URL, form={
                "grant_type": "refresh_token", "client_id": cid, "client_secret": sec,
                "refresh_token": t["refresh_token"]})
        except net.HttpError as e:
            if e.json().get("error") == "invalid_grant":
                store.save_token("google", {**t, "broken": "Google sign-in expired or was revoked."})
                raise RuntimeError("Google sign-in expired or was revoked. Sign in again.") from None
            raise
        _save(tok)
        t = store.load_tokens()["google"]
        t.pop("broken", None)
        store.save_token("google", t)
        return t["access_token"]


def get(url):
    return net.request("GET", url, headers={"Authorization": f"Bearer {access_token()}"})


def _send(method, url, body=None):
    return net.request(method, url, body=body, headers={"Authorization": f"Bearer {access_token()}"})


def _rn(resource_name):
    if not re.fullmatch(r"people/[A-Za-z0-9_-]+", resource_name or ""):
        raise ValueError(f"not a contact id: {resource_name!r}")
    return resource_name


def get_person(resource_name):
    return get(f"{PEOPLE}/{_rn(resource_name)}?personFields={PERSON_FIELDS}")


def create_person(body):
    """Only works inside net.writes_allowed() (see writer.py)."""
    return _send("POST", f"{PEOPLE}/people:createContact?personFields={PERSON_FIELDS}", body)


def update_person(resource_name, body, fields):
    """body must carry the current etag."""
    return _send("PATCH", f"{PEOPLE}/{_rn(resource_name)}:updateContact?updatePersonFields={fields}"
                          f"&personFields={PERSON_FIELDS}", body)


def delete_person(resource_name):
    """Google moves it to Trash (kept 30 days)."""
    return _send("DELETE", f"{PEOPLE}/{_rn(resource_name)}:deleteContact")


def group_members(group_rn):
    """resourceNames of the contacts in a label."""
    if not re.fullmatch(r"contactGroups/[A-Za-z0-9_-]+", group_rn or ""):
        raise ValueError(f"not a label id: {group_rn!r}")
    return get(f"{PEOPLE}/{group_rn}?maxMembers=10000").get("memberResourceNames", [])


def delete_group(group_rn):
    """Delete a label, keeping its contacts (deleteContacts=false)."""
    if not re.fullmatch(r"contactGroups/[A-Za-z0-9_-]+", group_rn or ""):
        raise ValueError(f"not a label id: {group_rn!r}")
    return _send("DELETE", f"{PEOPLE}/{group_rn}?deleteContacts=false")


def fetch_all():
    """All saved contacts (not 'Other contacts') plus the contact groups (labels)."""
    people, token = [], None
    while True:
        url = (f"{PEOPLE}/people/me/connections?pageSize=1000&personFields={PERSON_FIELDS}"
               + (f"&pageToken={urllib.parse.quote(token)}" if token else ""))
        r = get(url)
        people.extend(r.get("connections", []))
        token = r.get("nextPageToken")
        if not token:
            break
    groups, token = [], None
    while True:
        r = get(f"{PEOPLE}/contactGroups?pageSize=1000&groupFields=name,groupType,memberCount,clientData"
                + (f"&pageToken={urllib.parse.quote(token)}" if token else ""))
        groups.extend(r.get("contactGroups", []))
        token = r.get("nextPageToken")
        if not token:
            break
    return people, groups


def status():
    t = store.load_tokens().get("google")
    if not configured():
        return {"state": "unconfigured"}
    if not t or not t.get("refresh_token"):
        return {"state": "signed_out"}
    if t.get("broken"):
        return {"state": "broken", "account": t.get("account"), "error": t["broken"]}
    return {"state": "ok", "account": t.get("account")}
