"""Microsoft 365 contacts through Microsoft Graph (delegated, device-code sign-in, no client secret)."""
import logging
import os
import threading
import time
import urllib.parse

import net
import store

log = logging.getLogger("microsoft")

CLIENT_ID = os.environ.get("MS_CLIENT_ID", "").strip()
TENANT = os.environ.get("MS_TENANT", "").strip() or "organizations"
SCOPES = "offline_access User.Read Contacts.ReadWrite"
GRAPH = "https://graph.microsoft.com/v1.0"
LOGIN = f"https://login.microsoftonline.com/{urllib.parse.quote(TENANT)}/oauth2/v2.0"


def configured():
    return bool(CLIENT_ID)


class DeviceLogin:
    """One device-code sign-in at a time. start() returns the code to show; a thread polls until done."""

    def __init__(self):
        self.state = {}          # user_code, verification_uri, expires, status, error
        self._lock = threading.Lock()

    def start(self):
        with self._lock:
            if self.state.get("status") == "waiting" and self.state.get("expires", 0) > time.time():
                return self.state
            r = net.request("POST", f"{LOGIN}/devicecode", form={"client_id": CLIENT_ID, "scope": SCOPES})
            self.state = {"user_code": r["user_code"], "verification_uri": r["verification_uri"],
                          "expires": time.time() + int(r.get("expires_in", 900)), "status": "waiting",
                          "error": ""}
            threading.Thread(target=self._poll, args=(r["device_code"], int(r.get("interval", 5))),
                             daemon=True).start()
            return self.state

    def _poll(self, device_code, interval):
        while time.time() < self.state["expires"]:
            time.sleep(interval)
            try:
                tok = net.request("POST", f"{LOGIN}/token", form={
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                    "client_id": CLIENT_ID, "device_code": device_code})
            except net.HttpError as e:
                err = e.json().get("error", "")
                if err == "authorization_pending":
                    continue
                if err == "slow_down":
                    interval += 5
                    continue
                self.state.update(status="failed", error=e.json().get("error_description", str(e))[:300])
                log.warning("Microsoft sign-in failed: %s", err or e)
                return
            except Exception as e:      # network trouble: keep trying until the code expires
                log.warning("Microsoft sign-in poll: %s", e)
                continue
            _save(tok)
            try:
                me = get(f"{GRAPH}/me?$select=displayName,mail,userPrincipalName")
                store.save_token("microsoft", {**store.load_tokens()["microsoft"],
                                               "account": me.get("mail") or me.get("userPrincipalName")})
            except Exception as e:
                log.warning("signed in, but reading the account name failed: %s", e)
            self.state.update(status="done")
            log.info("Microsoft sign-in complete")
            return
        self.state.update(status="failed", error="The code expired. Start again.")


def _save(tok):
    old = store.load_tokens().get("microsoft", {})
    store.save_token("microsoft", {
        "access_token": tok["access_token"],
        "refresh_token": tok.get("refresh_token") or old.get("refresh_token"),
        "expires": time.time() + int(tok.get("expires_in", 3600)) - 120,
        "account": old.get("account"), "signed_in": old.get("signed_in") or time.time()})


_refresh_lock = threading.Lock()


def access_token():
    with _refresh_lock:
        t = store.load_tokens().get("microsoft")
        if not t or not t.get("refresh_token"):
            raise RuntimeError("Not signed in to Microsoft")
        if t.get("expires", 0) > time.time():
            return t["access_token"]
        try:
            tok = net.request("POST", f"{LOGIN}/token", form={
                "grant_type": "refresh_token", "client_id": CLIENT_ID,
                "refresh_token": t["refresh_token"], "scope": SCOPES})
        except net.HttpError as e:
            if e.json().get("error") in ("invalid_grant", "interaction_required"):
                store.save_token("microsoft", {**t, "broken": e.json().get("error_description", "")[:300]})
                raise RuntimeError("Microsoft sign-in expired or was revoked. Sign in again.") from None
            raise
        _save(tok)
        t = store.load_tokens()["microsoft"]
        t.pop("broken", None)
        store.save_token("microsoft", t)
        return t["access_token"]


def get(url):
    return net.request("GET", url, headers={"Authorization": f"Bearer {access_token()}",
                                            "Prefer": 'outlook.body-content-type="text"'})


def _paged(url):
    out = []
    while url:
        r = get(url)
        out.extend(r.get("value", []))
        url = r.get("@odata.nextLink")
    return out


def _folders():
    """Every contact folder, including nested ones, as (id, path)."""
    found = []

    def walk(url, prefix):
        for f in _paged(url):
            path = f"{prefix}/{f['displayName']}" if prefix else f["displayName"]
            found.append((f["id"], path))
            walk(f"{GRAPH}/me/contactFolders/{f['id']}/childFolders?$top=100", path)

    walk(f"{GRAPH}/me/contactFolders?$top=100", "")
    return found


def fetch_all():
    """All contacts: the default Contacts folder plus every other folder. Each raw contact gets
    a '_folder' key with its folder path. Returns (contacts, folders)."""
    contacts, seen = [], set()

    def add(items, path):
        for c in items:
            if c["id"] not in seen:          # never count a contact twice
                seen.add(c["id"])
                c["_folder"] = path
                contacts.append(c)

    add(_paged(f"{GRAPH}/me/contacts?$top=250"), "Contacts")
    folders = _folders()
    for fid, path in folders:
        add(_paged(f"{GRAPH}/me/contactFolders/{fid}/contacts?$top=250"), path)
    return contacts, [{"id": i, "path": p} for i, p in folders]


def status():
    t = store.load_tokens().get("microsoft")
    if not configured():
        return {"state": "unconfigured"}
    if not t or not t.get("refresh_token"):
        return {"state": "signed_out"}
    if t.get("broken"):
        return {"state": "broken", "account": t.get("account"), "error": t["broken"]}
    return {"state": "ok", "account": t.get("account")}
