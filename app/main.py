"""Contact Sync: backs up Microsoft 365 and Google contacts and reports what a cleanup would do.
This build is read-only (see net.py)."""
import datetime as dt
import json
import logging
import os
import re
import ssl
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import auth
import backup
import google
import icon
import msgraph
import store
import tls
import webauthn
import web
import sync
import writer
from jobs import Runner
from version import VERSION

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("main")

PORT = int(os.environ.get("PORT", "80"))
HTTPS_PORT = int(os.environ.get("HTTPS_PORT", "443"))
TLS_ON = os.environ.get("TLS", "1").strip() not in ("0", "false", "no", "off")
# port the browser uses for HTTPS (differs from HTTPS_PORT when Docker maps it, e.g. 8443:443)
PUBLIC_HTTPS_PORT = int(os.environ.get("PUBLIC_HTTPS_PORT", str(HTTPS_PORT)))
runner = Runner()
device = msgraph.DeviceLogin()
def _on_change(removed=(), upserted=()):
    runner.apply_change(removed=removed, upserted=upserted)     # keep the pages current, no re-download
    sync.after_write(removed=removed, upserted=upserted)       # page edits become the new sync base


writer.listener = _on_change
_google_url = {"url": ""}


def next_backup_text():
    s = store.load_settings()
    if not s["backup_enabled"]:
        return "off"
    h, m = (int(x) for x in s["backup_time"].split(":"))
    now = dt.datetime.now()
    t = now.replace(hour=h, minute=m, second=0, microsecond=0)
    t = t if t > now else t + dt.timedelta(days=1)
    return t.strftime("%a ") + f"{t.hour % 12 or 12}:{t.minute:02d} {'AM' if t.hour < 12 else 'PM'}"


def scheduler():
    last = None
    cert_day = None
    while True:
        try:
            s = store.load_settings()
            now = dt.datetime.now()
            if TLS_ON and cert_day != now.date():
                cert_day = now.date()
                tls.refresh()               # renews the site certificate when < 60 days are left
            key = now.strftime("%Y-%m-%d ") + s["backup_time"]
            if s["backup_enabled"] and now.strftime("%H:%M") == s["backup_time"] and last != key:
                last = key
                if msgraph.status()["state"] == "ok" or google.status()["state"] == "ok":
                    runner.start("nightly")
            if s.get("sync_enabled") and not runner.progress["running"]:
                last_sync = (store.q("SELECT started FROM sync_runs ORDER BY id DESC LIMIT 1") or [{"started": 0}])[0]
                if time.time() - (last_sync["started"] or 0) >= int(s.get("sync_minutes", 60)) * 60 \
                        and msgraph.status()["state"] == "ok" and google.status()["state"] == "ok":
                    threading.Thread(target=sync.run, args=("schedule",), daemon=True).start()
        except Exception:
            log.exception("scheduler")
        time.sleep(20)


def status_json():
    last = (store.q("SELECT * FROM runs ORDER BY id DESC LIMIT 1") or [None])[0]
    r = runner.report
    return {"version": VERSION, "write_mode": writer.mode(),
            "microsoft": msgraph.status(), "google": google.status(), "job": runner.progress,
            "last_run": last, "report_counts": r["counts"] if r else None,
            "report_created": r.get("created") if r else None, "next_backup": next_backup_text()}


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        """Failed TLS handshakes, scanners and dropped connections are normal: one log line, no trace."""
        import sys
        err = sys.exc_info()[1]
        if isinstance(err, (ssl.SSLError, ConnectionError, TimeoutError, OSError)):
            log.debug("connection from %s dropped: %s", client_address[0], err)
            return
        super().handle_error(request, client_address)


def _person(key):
    return next((x for x in (runner.report or {}).get("people", []) if x["key"] == key), None)


def _best(p):
    """Editable record from a fresh read of the most recently changed copy of a person (never
    from the report, which may be older or from a version without every field)."""
    import model
    fresh = [writer._fresh(m)[1] for m in p["members"]]
    return model.editable(max(fresh, key=lambda c: c.get("updated") or ""))


def _site():
    """(rp_id, allowed origins) for passkeys: the one canonical HTTPS address."""
    host = tls.canonical_host()
    port = "" if PUBLIC_HTTPS_PORT == 443 else f":{PUBLIC_HTTPS_PORT}"
    return host, {f"https://{host}{port}"}


# reachable without signing in
OPEN_PATHS = {"/login", "/setup", "/cert", "/ca.crt", "/auth/login/options", "/auth/login",
              "/auth/register/options", "/auth/register", "/setup/password", "/login/password"}


class Handler(BaseHTTPRequestHandler):
    server_version = "contact-sync/" + VERSION
    timeout = 60            # a stalled client (or TLS handshake) can't hold a thread forever

    def log_message(self, fmt, *args):
        pass

    @property
    def secure(self):
        return isinstance(self.connection, ssl.SSLSocket)

    def _canonical(self):
        """With HTTPS on, everything is used at https://<canonical host> (passkeys are tied to
        one name). Plain HTTP still answers /healthz (health check) and the certificate download
        (needed before a device trusts HTTPS). Returns True if it redirected."""
        if not TLS_ON:
            return False
        path = urllib.parse.urlparse(self.path).path
        if path == "/healthz" or (not self.secure and path in ("/cert", "/ca.crt")):
            return False
        host = (self.headers.get("Host") or "").lower()
        want_host, origins = _site()
        want = next(iter(origins))
        if self.secure and (host == want_host and PUBLIC_HTTPS_PORT == 443 or want.endswith("//" + host)):
            return False
        self._send(308, "", headers={"Location": want + self.path})
        return True

    def _send(self, code, body, ctype="text/html; charset=utf-8", headers=None):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj, code=200, headers=None):
        self._send(code, json.dumps(obj, default=str), "application/json", headers)

    def _redirect(self, to, cookie=None):
        self._send(303, "", headers={"Location": to, **({"Set-Cookie": cookie} if cookie else {})})

    def _ip(self):
        return self.client_address[0]

    def _signed_in(self):
        return auth.logged_in(self.headers.get("Cookie"))

    @staticmethod
    def _access(new_key=""):
        return {"passkeys": auth.credentials(), "has_password": auth.has_password(),
                "api_keys": auth.api_keys(), "env_key": bool(auth.API_KEY), "new_key": new_key,
                "min_len": auth.MIN_LEN}

    def _gate(self, path):
        """True if the request was answered here. /api/ also accepts the API key."""
        if path.startswith("/api/") and auth.api_key_ok(self.headers.get("Authorization")):
            return False
        if path in OPEN_PATHS or self._signed_in():
            return False
        if path.startswith("/api/"):
            self._json({"error": "sign in or send the API key"}, 401)
        elif not auth.is_set():
            self._redirect("/setup")
        else:
            self._redirect("/login?next=" + urllib.parse.quote(path if self.command == "GET" else "/"))
        return True

    @staticmethod
    def _safe_next(nxt):
        return nxt if nxt.startswith("/") and not nxt.startswith("//") and "\\" not in nxt else "/"

    def do_GET(self):
        if self._canonical():
            return
        url = urllib.parse.urlparse(self.path)
        path, qs = url.path, urllib.parse.parse_qs(url.query)
        if path == "/healthz":
            return self._send(200, "ok", "text/plain")
        found = icon.get(path.lstrip("/"))
        if found:
            return self._send(200, found[0], found[1], {"Cache-Control": "public, max-age=86400"})
        if self._gate(path):
            return
        if path == "/ca.crt":
            return self._send(200, tls.ca_pem(), "application/x-x509-ca-cert",
                              {"Content-Disposition": 'attachment; filename="contact-sync-ca.crt"'})
        if path == "/cert":
            return self._send(200, web.cert_page(tls.fingerprint(), tls.canonical_host(), next(iter(_site()[1]))))
        if path == "/setup":
            return self._redirect("/") if auth.is_set() else self._send(200, web.setup_page(next(iter(_site()[1]))))
        if path == "/login":
            if not auth.is_set():
                return self._redirect("/setup")
            return self._send(200, web.login_page(self._safe_next(qs.get("next", ["/"])[0]),
                                                  has_password=auth.has_password(), has_passkey=bool(auth.credentials())))
        if path == "/":
            last = (store.q("SELECT * FROM runs ORDER BY id DESC LIMIT 1") or [None])[0]
            return self._send(200, web.dashboard(msgraph.status(), google.status(), runner.progress,
                                                 runner.report, last, next_backup_text(), bool(auth.API_KEY)))
        if path == "/report":
            return self._send(200, web.report_page(runner.report))
        if path == "/contacts":
            return self._send(200, web.contacts_page(runner.report, qs.get("q", [""])[0].strip()[:100],
                                                     qs.get("show", [""])[0]))
        m = re.fullmatch(r"/contacts/([0-9a-f]{12})(/edit)?", path)
        if m:
            p = _person(m.group(1))
            if not p:
                return self._send(404, web.page("Not found", "/contacts", "<h1>Not found</h1><p>That contact isn't in "
                                                "the latest copy. <a href='/contacts'>Back to Contacts</a></p>"))
            if m.group(2):
                try:
                    rec = _best(p)
                except Exception as e:      # deleted or changed elsewhere since the last read, or a network error
                    log.warning("couldn't read %s for editing: %s", p["name"], e)
                    return self._redirect(f"/contacts/{m.group(1)}?" + urllib.parse.urlencode(
                        {"error": "Couldn't read the current copy to edit (it may have been deleted or changed in "
                                  f"Outlook or Google). Run a backup to refresh. Details: {e}"[:400]}))
                return self._send(200, web.contact_edit_page(p, rec, write_mode=writer.mode()))
            return self._send(200, web.contact_page(p, runner.report, writer.history([c["id"] for c in p["members"]]),
                                                    msg=qs.get("msg", [""])[0][:500], error=qs.get("error", [""])[0][:500],
                                                    write_mode=writer.mode()))
        if path == "/sync":
            return self._send(200, web.sync_page(sync.status(), sync.recent_runs(), sync.recent_conflicts(),
                                                 writer.mode(), qs.get("msg", [""])[0][:300], qs.get("error", [""])[0][:300]))
        if path == "/api/sync":
            return self._json({**sync.status(), "runs": sync.recent_runs(5), "conflicts": sync.recent_conflicts(20)})
        if path == "/bin":
            return self._send(200, web.bin_page(writer.bin_items(), qs.get("msg", [""])[0][:300],
                                                qs.get("error", [""])[0][:300], writer.mode()))
        if path == "/api/contacts":
            q = qs.get("q", [""])[0].strip()
            people = [x for x in (runner.report or {}).get("people", []) if web._match(x, q)]
            return self._json({"count": len(people), "people": people[:200], "write_mode": writer.mode()})
        m = re.fullmatch(r"/api/contacts/([0-9a-f]{12})/history", path)
        if m:
            p = _person(m.group(1))
            return self._json(writer.history([c["id"] for c in p["members"]]) if p else {"error": "not found"},
                              200 if p else 404)
        if path == "/api/bin":
            return self._json([{k: v for k, v in it.items() if k not in ("ms_raw", "g_raw")} for it in writer.bin_items()])
        if path == "/backups":
            return self._send(200, web.backups_page(backup.listing()))
        m = re.fullmatch(r"/backups/([^/]+)/([^/]+)", path)
        if m:
            p = backup.path_of(m.group(1), m.group(2))
            if not p:
                return self._send(404, "Not found", "text/plain")
            with open(p, "rb") as f:
                data = f.read()
            return self._send(200, data, "application/octet-stream",
                              {"Content-Disposition": f'attachment; filename="{m.group(1)}_{m.group(2)}"'})
        if path == "/settings":
            return self._send(200, web.settings_page(store.load_settings(), self._access(),
                                                     saved="saved" in qs, error=qs.get("error", [""])[0],
                                                     cert=tls.fingerprint()))
        if path == "/signin/microsoft":
            if not msgraph.configured():
                return self._redirect("/")
            return self._send(200, web.signin_microsoft(device.state))
        if path == "/signin/microsoft/done":
            if device.state.get("status") == "done":
                device.state = {}
                return self._redirect("/")
            return self._send(200, web.signin_microsoft(device.state))
        if path == "/signin/google":
            if not google.configured():
                return self._redirect("/")
            _google_url["url"] = google.start_login()
            return self._send(200, web.signin_google(_google_url["url"]))
        # ----- API (cookie or API key) -----
        if path == "/api/status":
            return self._json(status_json())
        if path == "/api/setup-code":
            return self._json({"code": auth.setup_code(), "passkeys": len(auth.credentials())})
        if path == "/api/signin/microsoft":
            return self._json({"status": device.state.get("status", "")})
        if path == "/api/report":
            return self._json(runner.report or {"error": "no report yet"}, 200 if runner.report else 404)
        if path == "/api/backups":
            return self._json(backup.listing())
        self._send(404, "Not found", "text/plain")

    def _passkey_post(self, path, body):
        """JSON endpoints for passkey sign-in and registration."""
        rp_id, origins = _site()
        ip = self._ip()
        if path in ("/auth/login/options", "/auth/login", "/auth/register/options") and auth.too_many_failures(ip):
            return self._json({"error": "Too many tries. Wait 5 minutes."}, 429)
        try:
            if path == "/auth/login/options":
                if not auth.is_set():
                    return self._json({"error": "No passkey yet. Set one up first."}, 400)
                return self._json(auth.login_options(rp_id))
            if path == "/auth/login":
                name = auth.finish_login(body, origins, rp_id)
                log.info("signed in with passkey '%s' from %s", name, ip)
                return self._json({"ok": True, "next": self._safe_next(body.get("next") or "/")},
                                  headers={"Set-Cookie": auth.make_cookie(self.secure)})
            if path == "/auth/register/options":
                if auth.is_set():
                    if not self._signed_in():
                        return self._json({"error": "Sign in first."}, 401)
                elif not auth.check_setup_code(body.get("code")):
                    auth.note_failure(ip)
                    log.warning("wrong setup code from %s", ip)
                    return self._json({"error": "That setup code is wrong."}, 403)
                return self._json(auth.register_options(rp_id, f"Contact Sync ({rp_id})"))
            if path == "/auth/register":
                first = not auth.is_set()
                if not first and not self._signed_in():
                    return self._json({"error": "Sign in first."}, 401)
                # the challenge was only handed out after the setup code / sign-in check above
                auth.finish_register(body, origins, rp_id, body.get("name"))
                log.info("passkey '%s' added from %s", body.get("name"), ip)
                return self._json({"ok": True}, headers={"Set-Cookie": auth.make_cookie(self.secure)} if first else None)
        except webauthn.Invalid as e:
            auth.note_failure(ip)
            log.warning("passkey check failed from %s: %s", ip, e)
            return self._json({"error": str(e)}, 400)
        return self._json({"error": "not found"}, 404)

    def _contact_write(self, path, f):
        """Edit / delete / restore from the pages (signed in). Returns None if the path isn't one of these."""
        m = re.fullmatch(r"/contacts/([0-9a-f]{12})/(edit|delete|history/(\d+)/restore)", path)
        if m:
            key, what = m.group(1), m.group(2)
            p = _person(key)
            if not p:
                return self._redirect("/contacts")
            back = lambda q: self._redirect(f"/contacts/{key}?" + urllib.parse.urlencode(q))
            try:
                if what == "edit":
                    try:
                        rec = web.parse_edit_form(f)
                    except ValueError as e:     # show the form again with what was typed, not the old data
                        return self._send(400, web.contact_edit_page(p, web.parse_edit_form(f, validate=False),
                                                                     str(e), writer.mode()))
                    warns = writer.update(p["members"], rec)
                    newp = next((x for x in runner.report.get("people", []) if
                                 {c["id"] for c in x["members"]} & {c["id"] for c in p["members"]}), None)
                    dest = f"/contacts/{newp['key']}" if newp else "/contacts"
                    msg = "Saved to both." + (" " + " ".join(warns) if warns else "")
                    return self._redirect(dest + "?" + urllib.parse.urlencode({"msg": msg}))
                if what == "delete":
                    bin_id = writer.delete(p["members"])
                    return self._redirect("/bin?" + urllib.parse.urlencode(
                        {"msg": f"Deleted “{p['name']}” from both accounts. It's in the recycle bin (#{bin_id})."}))
                warns = writer.restore_version(int(m.group(3)), p["members"])
                return back({"msg": "Earlier version restored on both sides." + (" " + " ".join(warns) if warns else "")})
            except writer.Refused as e:
                return back({"error": str(e)})
            except Exception as e:
                log.exception("contact write failed")
                return back({"error": f"Failed: {e}"[:400]})
        m = re.fullmatch(r"/bin/(\d+)/restore", path)
        if m:
            try:
                ids, warns = writer.restore_bin(int(m.group(1)))
            except (writer.Refused, Exception) as e:
                log.exception("restore failed")
                return self._redirect("/bin?" + urllib.parse.urlencode({"error": str(e)[:400]}))
            return self._redirect("/bin?" + urllib.parse.urlencode(
                {"msg": "Restored to " + " and ".join("Microsoft 365" if s == "microsoft" else "Google" for s in ids)
                 + "." + (" " + " ".join(warns) if warns else "")}))
        if path.startswith("/sync/"):
            back = lambda k, v: self._redirect("/sync?" + urllib.parse.urlencode({k: v}))
            try:
                if path == "/sync/start":
                    n = sync.start()
                    threading.Thread(target=sync.run, args=("first",), daemon=True).start()
                    return back("msg", f"Sync started: {n} contacts linked. The first run is going now.")
                if path in ("/sync/run", "/sync/allow-deletes"):
                    threading.Thread(target=sync.run, args=("manual", path.endswith("deletes")), daemon=True).start()
                    return back("msg", "Sync run started.")
                if path in ("/sync/pause", "/sync/resume"):
                    sync.pause(path.endswith("resume"))
                    return back("msg", "Sync resumed." if path.endswith("resume") else "Sync paused.")
                if path == "/sync/settings":
                    s = store.load_settings()
                    mins, maxd = int(f("sync_minutes") or 60), int(f("sync_max_deletes") or 10)
                    if not (5 <= mins <= 1440 and 0 <= maxd <= 1000):
                        return back("error", "Every 5–1440 minutes; held-back deletes 0–1000.")
                    s.update(sync_minutes=mins, sync_max_deletes=maxd)
                    store.save_settings(s)
                    return back("msg", "Saved.")
            except (writer.Refused, ValueError) as e:
                return back("error", str(e))
            except Exception as e:
                log.exception("sync action failed")
                return back("error", f"Failed: {e}"[:300])
        if path == "/test-contact":
            try:
                writer.create(dict(writer.TEST_REC))
            except Exception as e:
                return self._redirect("/settings?" + urllib.parse.urlencode({"error": str(e)[:400]}))
            return self._redirect("/contacts?q=Contact-Sync")
        return None

    def _api_write(self, path, raw):
        """The same actions for scripts with an API key, always limited to test contacts."""
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            return self._json({"error": "bad JSON"}, 400)
        try:
            with writer.test_only():
                if path == "/api/sync/run":         # inside test_only(): only test contacts are written
                    rid = sync.run("api")
                    run = (store.q("SELECT * FROM sync_runs WHERE id=?", (rid,)) or [None])[0] if rid else None
                    return self._json({"run": run} if run else {"error": "a sync run is already going"},
                                      200 if run else 409)
                if path == "/api/test-contact":
                    ids, warns = writer.create(dict(writer.TEST_REC))
                    return self._json({"ids": ids, "warnings": warns})
                m = re.fullmatch(r"/api/contacts/([0-9a-f]{12})/(edit|delete|history/(\d+)/restore)", path)
                if m:
                    p = _person(m.group(1))
                    if not p:
                        return self._json({"error": "not found"}, 404)
                    if m.group(2) == "edit":
                        rec = _best(p)
                        rec.update({k: v for k, v in body.items() if k in rec})
                        warns = writer.update(p["members"], rec)
                    elif m.group(2) == "delete":
                        warns = {"bin_id": writer.delete(p["members"])}
                    else:
                        warns = writer.restore_version(int(m.group(3)), p["members"])
                    return self._json({"ok": True, "result": warns})
                m = re.fullmatch(r"/api/bin/(\d+)/restore", path)
                if m:
                    ids, warns = writer.restore_bin(int(m.group(1)))
                    return self._json({"ids": ids, "warnings": warns})
        except writer.Refused as e:
            return self._json({"error": str(e)}, 403)
        except Exception as e:
            log.exception("API write failed")
            return self._json({"error": str(e)[:400]}, 500)
        return self._json({"error": "not found"}, 404)

    def do_POST(self):
        if self._canonical():
            return
        path = urllib.parse.urlparse(self.path).path
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(min(n, 65536)) if n else b""
        if self._gate(path):
            return
        if path.startswith("/auth/"):
            try:
                body = json.loads(raw or b"{}")
                if not isinstance(body, dict):
                    raise ValueError
            except ValueError:
                return self._json({"error": "bad request"}, 400)
            return self._passkey_post(path, body)
        form = urllib.parse.parse_qs(raw.decode(), keep_blank_values=True)
        f = lambda k: form.get(k, [""])[0]
        if path == "/logout":
            return self._redirect("/login", auth.clear_cookie(self.secure))
        if path == "/logout-everywhere":
            auth.logout_everywhere()
            return self._redirect("/login", auth.clear_cookie(self.secure))
        back = lambda err: self._redirect("/settings?" + (urllib.parse.urlencode({"error": err}) if err else "saved=1"))
        if path == "/setup/password":
            if auth.is_set():
                return self._redirect("/")
            if auth.too_many_failures(self._ip()):
                return self._send(429, web.setup_page(next(iter(_site()[1])), "Too many tries. Wait 5 minutes."))
            if not auth.check_setup_code(f("code")):
                auth.note_failure(self._ip())
                log.warning("wrong setup code from %s", self._ip())
                return self._send(403, web.setup_page(next(iter(_site()[1])), "That setup code is wrong."))
            if f("password") != f("password2"):
                return self._send(400, web.setup_page(next(iter(_site()[1])), "The two passwords don't match."))
            try:
                auth.set_password(f("password"))
            except ValueError as e:
                return self._send(400, web.setup_page(next(iter(_site()[1])), str(e)))
            log.info("first sign-in method: password, from %s", self._ip())
            return self._redirect("/", auth.make_cookie(self.secure))
        if path == "/login/password":
            nxt = self._safe_next(f("next") or "/")
            if auth.too_many_failures(self._ip()):
                return self._send(429, web.login_page(nxt, "Too many tries. Wait 5 minutes.", auth.has_password(), bool(auth.credentials())))
            if not auth.check_password(f("password")):
                auth.note_failure(self._ip())
                log.warning("wrong password from %s", self._ip())
                return self._send(401, web.login_page(nxt, "Wrong password.", auth.has_password(), bool(auth.credentials())))
            log.info("signed in with the password from %s", self._ip())
            return self._redirect(nxt, auth.make_cookie(self.secure))
        if path == "/password":
            if auth.has_password() and not auth.check_password(f("current")):
                auth.note_failure(self._ip())
                return back("Current password is wrong.")
            if f("password") != f("password2"):
                return back("The two new passwords don't match.")
            try:
                auth.set_password(f("password"))
            except ValueError as e:
                return back(str(e))
            auth.logout_everywhere()            # other browsers must sign in again
            return self._redirect("/settings?saved=1", auth.make_cookie(self.secure))
        if path == "/password/remove":
            return back(auth.remove_password())
        if path == "/apikeys/new":
            key = auth.create_api_key(f("name"))
            return self._send(200, web.settings_page(store.load_settings(), self._access(key), cert=tls.fingerprint()))
        if path == "/apikeys/revoke":
            return back(auth.revoke_api_key(f("id")))
        if path == "/passkeys/remove":
            return back(auth.remove_credential(f("id")))
        if path == "/settings":
            s = store.load_settings()
            try:
                new = dict(s, backup_enabled="backup_enabled" in form)
                t = f("backup_time") or s["backup_time"]
                if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", t):
                    raise ValueError("Time must look like 02:30")
                keep = int(f("keep_days") or s["keep_days"])
                if not 3 <= keep <= 365:
                    raise ValueError("Keep backups: 3–365 days")
                region = (f("default_region") or "US").strip().upper()
                if not re.fullmatch(r"[A-Z]{2}", region):
                    raise ValueError("Country must be two letters, like US")
                wm = f("write_mode") or s.get("write_mode", "test")
                if wm not in writer.MODES:
                    raise ValueError("Unknown write mode")
                new.update(backup_time=t, keep_days=keep, default_region=region, write_mode=wm)
            except ValueError as e:
                return self._send(400, web.settings_page(s, self._access(), error=str(e), cert=tls.fingerprint()))
            store.save_settings(new)
            return self._redirect("/settings?saved=1")
        if path.startswith("/api/") and path != "/api/run":
            return self._api_write(path, raw)
        r = self._contact_write(path, f)
        if r is not None:
            return r
        if path in ("/run", "/api/run"):
            started = runner.start("manual" if path == "/run" else "api")
            return self._json({"started": started, "job": runner.progress}) if path == "/api/run" else self._redirect("/")
        if path == "/signin/microsoft":
            try:
                device.start()
            except Exception as e:
                return self._send(502, web.signin_microsoft({}, f"Couldn't get a code from Microsoft: {e}"))
            return self._redirect("/signin/microsoft")
        if path == "/signin/google":
            err = google.finish_login(f("pasted"))
            if err:
                _google_url["url"] = google.start_login()
                return self._send(400, web.signin_google(_google_url["url"], err))
            return self._redirect("/")
        m = re.fullmatch(r"/signout/(microsoft|google)", path)
        if m:
            store.save_token(m.group(1), None)
            log.info("%s sign-in forgotten from %s", m.group(1), self._ip())
            return self._redirect("/settings?saved=1")
        self._send(404, "Not found", "text/plain")


def main():
    store.init()
    writer.init()
    sync.init()
    writer.prune()
    store.x("UPDATE runs SET finished=started, error='Interrupted (container restarted)' WHERE finished IS NULL")
    if not os.path.exists(store.SETTINGS_PATH):
        store.save_settings(store.load_settings())
    runner.load_latest()
    log.info("contact-sync %s (read-only): HTTP :%d%s. Microsoft %s, Google %s, API key %s", VERSION, PORT,
             f", HTTPS :{HTTPS_PORT}" if TLS_ON else "",
             "configured" if msgraph.configured() else "NOT CONFIGURED",
             "configured" if google.configured() else "NOT CONFIGURED", "on" if auth.API_KEY else "off")
    if TLS_ON:
        tls.ensure()
        log.info("site: https://%s  CA SHA-256 %s", tls.canonical_host(), tls.fingerprint())
        https = Server(("", HTTPS_PORT), Handler)
        # handshake happens in the request thread, so a slow client can't block new connections
        https.socket = tls.context().wrap_socket(https.socket, server_side=True, do_handshake_on_connect=False)
        threading.Thread(target=https.serve_forever, daemon=True).start()
    else:
        log.warning("TLS=0: passkeys need HTTPS, so sign-in only works on http://localhost")
    auth.setup_code()               # logs the one-time code while no passkey exists
    threading.Thread(target=scheduler, daemon=True).start()
    Server(("", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
