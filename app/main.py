"""Contact Sync: backs up Microsoft 365 and Google contacts and reports what a cleanup would do.
This build is read-only (see net.py)."""
import datetime as dt
import json
import logging
import os
import re
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
import web
from jobs import Runner
from version import VERSION

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("main")

PORT = int(os.environ.get("PORT", "80"))
runner = Runner()
device = msgraph.DeviceLogin()
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
    while True:
        try:
            s = store.load_settings()
            now = dt.datetime.now()
            key = now.strftime("%Y-%m-%d ") + s["backup_time"]
            if s["backup_enabled"] and now.strftime("%H:%M") == s["backup_time"] and last != key:
                last = key
                if msgraph.status()["state"] == "ok" or google.status()["state"] == "ok":
                    runner.start("nightly")
        except Exception:
            log.exception("scheduler")
        time.sleep(20)


def status_json():
    last = (store.q("SELECT * FROM runs ORDER BY id DESC LIMIT 1") or [None])[0]
    r = runner.report
    return {"version": VERSION, "read_only": True,
            "microsoft": msgraph.status(), "google": google.status(), "job": runner.progress,
            "last_run": last, "report_counts": r["counts"] if r else None,
            "report_created": r.get("created") if r else None, "next_backup": next_backup_text()}


class Handler(BaseHTTPRequestHandler):
    server_version = "contact-sync/" + VERSION

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="text/html; charset=utf-8", headers=None):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, default=str), "application/json")

    def _redirect(self, to, cookie=None):
        self._send(303, "", headers={"Location": to, **({"Set-Cookie": cookie} if cookie else {})})

    def _ip(self):
        return self.client_address[0]

    def _gate(self, path):
        """True if the request was answered here. /api/ also accepts the API key."""
        if path.startswith("/api/") and auth.api_key_ok(self.headers.get("Authorization")):
            return False
        if not auth.is_set():
            if path == "/setup":
                return False
            if path.startswith("/api/"):
                self._json({"error": "password not set"}, 401)
            else:
                self._redirect("/setup")
            return True
        if path == "/login" or auth.logged_in(self.headers.get("Cookie")):
            return False
        if path.startswith("/api/"):
            self._json({"error": "log in or send the API key"}, 401)
        else:
            self._redirect("/login?next=" + urllib.parse.quote(path if self.command == "GET" else "/"))
        return True

    @staticmethod
    def _safe_next(nxt):
        return nxt if nxt.startswith("/") and not nxt.startswith("//") and "\\" not in nxt else "/"

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        path, qs = url.path, urllib.parse.parse_qs(url.query)
        if path == "/healthz":
            return self._send(200, "ok", "text/plain")
        found = icon.get(path.lstrip("/"))
        if found:
            return self._send(200, found[0], found[1], {"Cache-Control": "public, max-age=86400"})
        if self._gate(path):
            return
        if path == "/setup":
            return self._redirect("/") if auth.is_set() else self._send(200, web.login_page(setup=True))
        if path == "/login":
            return self._send(200, web.login_page(False, nxt=self._safe_next(qs.get("next", ["/"])[0])))
        if path == "/":
            last = (store.q("SELECT * FROM runs ORDER BY id DESC LIMIT 1") or [None])[0]
            return self._send(200, web.dashboard(msgraph.status(), google.status(), runner.progress,
                                                 runner.report, last, next_backup_text(), bool(auth.API_KEY)))
        if path == "/report":
            return self._send(200, web.report_page(runner.report))
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
            return self._send(200, web.settings_page(store.load_settings(), saved="saved" in qs))
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
        if path == "/api/signin/microsoft":
            return self._json({"status": device.state.get("status", "")})
        if path == "/api/report":
            return self._json(runner.report or {"error": "no report yet"}, 200 if runner.report else 404)
        if path == "/api/backups":
            return self._json(backup.listing())
        self._send(404, "Not found", "text/plain")

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        n = int(self.headers.get("Content-Length") or 0)
        form = urllib.parse.parse_qs(self.rfile.read(min(n, 65536)).decode(), keep_blank_values=True) if n else {}
        if self._gate(path):
            return
        f = lambda k: form.get(k, [""])[0]
        if path == "/setup":
            if auth.is_set():
                return self._redirect("/")
            pw = f("password")
            if len(pw) < auth.MIN_LEN:
                return self._send(400, web.login_page(True, f"Use at least {auth.MIN_LEN} characters."))
            if pw != f("password2"):
                return self._send(400, web.login_page(True, "The two passwords don't match."))
            auth.set_password(pw)
            log.info("website password set from %s", self._ip())
            return self._redirect("/", auth.make_cookie())
        if path == "/login":
            nxt = self._safe_next(f("next") or "/")
            if auth.too_many_failures(self._ip()):
                return self._send(429, web.login_page(False, "Too many tries. Wait 5 minutes.", nxt))
            if not auth.check_password(f("password")):
                auth.note_failure(self._ip())
                log.warning("failed login from %s", self._ip())
                return self._send(401, web.login_page(False, "Wrong password.", nxt))
            return self._redirect(nxt, auth.make_cookie())
        if path == "/logout":
            return self._redirect("/login", auth.clear_cookie())
        if path == "/password":
            if not auth.check_password(f("current")):
                return self._send(400, web.settings_page(store.load_settings(), error="Current password is wrong."))
            if len(f("password")) < auth.MIN_LEN or f("password") != f("password2"):
                return self._send(400, web.settings_page(
                    store.load_settings(), error=f"New password must be at least {auth.MIN_LEN} characters and match."))
            auth.set_password(f("password"))
            return self._redirect("/settings?saved=1", auth.make_cookie())
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
                new.update(backup_time=t, keep_days=keep, default_region=region)
            except ValueError as e:
                return self._send(400, web.settings_page(s, error=str(e)))
            store.save_settings(new)
            return self._redirect("/settings?saved=1")
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
    store.x("UPDATE runs SET finished=started, error='Interrupted (container restarted)' WHERE finished IS NULL")
    if not os.path.exists(store.SETTINGS_PATH):
        store.save_settings(store.load_settings())
    runner.load_latest()
    threading.Thread(target=scheduler, daemon=True).start()
    log.info("contact-sync %s on :%d (read-only). Microsoft %s, Google %s, API key %s", VERSION, PORT,
             "configured" if msgraph.configured() else "NOT CONFIGURED",
             "configured" if google.configured() else "NOT CONFIGURED", "on" if auth.API_KEY else "off")
    ThreadingHTTPServer(("", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
