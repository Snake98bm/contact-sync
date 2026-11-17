"""The backup-and-report job: read both accounts, save a backup, build the cleanup report.
Read-only: nothing in either account is changed."""
import json
import logging
import os
import threading
import time

import backup
import clean
import google
import model
import msgraph
import store

log = logging.getLogger("jobs")


class Runner:
    def __init__(self):
        self.progress = {"running": False, "step": "", "error": ""}
        self.report = None              # latest report, kept in memory for the pages
        self._lock = threading.Lock()

    def start(self, trigger):
        with self._lock:
            if self.progress["running"]:
                return False
            self.progress = {"running": True, "step": "Starting", "error": ""}
        threading.Thread(target=self._run, args=(trigger,), daemon=True).start()
        return True

    def _step(self, text):
        self.progress["step"] = text
        log.info(text)

    def _photos(self, raw_ms, raw_g):
        """Every contact's own photo, base64, by side and id (Outlook needs one read per contact)."""
        import base64
        import writer
        out = {"microsoft": {}, "google": {}}
        if raw_g:
            people = [p for p in raw_g["people"] if google.photo_url(p)]
            for n, p in enumerate(people, 1):
                self._step(f"Saving photos (Google {n} of {len(people)})")
                data = writer.get_photo("google", p["resourceName"], p)
                if data:
                    out["google"][p["resourceName"]] = base64.b64encode(data).decode()
        if raw_ms:
            items = raw_ms["contacts"]
            for n, c in enumerate(items, 1):
                if n % 25 == 1:
                    self._step(f"Saving photos (Microsoft 365 {n} of {len(items)})")
                data = writer.get_photo("microsoft", c["id"])
                if data:
                    out["microsoft"][c["id"]] = base64.b64encode(data).decode()
        log.info("photos saved: %d Microsoft 365, %d Google", len(out["microsoft"]), len(out["google"]))
        return out

    def apply_change(self, removed=(), upserted=()):
        """After a write: update the in-memory report from the raw replies the write already got,
        instead of downloading every contact again. removed = [(side, id)], upserted = [(side, raw)]."""
        r = self.report
        if not r or "people" not in r:
            return
        gone = {(s, i) for s, i in removed}
        new = {}
        for side, raw in upserted:
            c = (model.from_graph(dict(raw, _folder=raw.get("_folder", "Contacts"))) if side == "microsoft"
                 else model.from_people(raw, r.get("group_names") or {}))
            new[(side, c["id"])] = c
        contacts = [c for p in r["people"] for c in p["members"]
                    if (c["side"], c["id"]) not in gone and (c["side"], c["id"]) not in new]
        contacts += list(new.values())
        report = clean.analyse(contacts, store.load_settings().get("default_region", "US"), _link_pairs())
        for k in ("created", "backup", "notes", "group_names"):
            report[k] = r.get(k)
        self.report = report

    def _run(self, trigger):
        rid = store.x("INSERT INTO runs(started, trigger) VALUES(?, ?)", (time.time(), trigger))
        settings = store.load_settings()
        raw_ms = raw_g = None
        contacts, notes, names = [], [], {}
        try:
            if msgraph.status()["state"] == "ok":
                self._step("Reading Microsoft 365 contacts")
                items, folders = msgraph.fetch_all()
                raw_ms = {"contacts": items, "folders": folders}
                contacts += [model.from_graph(c) for c in items]
            else:
                notes.append("Microsoft not signed in: skipped")
            if google.status()["state"] == "ok":
                self._step("Reading Google contacts")
                people, groups = google.fetch_all()
                raw_g = {"people": people, "groups": groups}
                names = {g["resourceName"]: g.get("formattedName") or g.get("name") for g in groups}
                contacts += [model.from_people(p, names) for p in people]
            else:
                notes.append("Google not signed in: skipped")
            if raw_ms is None and raw_g is None:
                raise RuntimeError("Sign in to at least one account first.")
            photos = None
            if settings.get("backup_photos", True):
                photos = self._photos(raw_ms, raw_g)
            self._step("Saving the backup")
            name = backup.save(raw_ms, raw_g, contacts, {"trigger": trigger, "notes": notes}, photos)
            self._step("Building the cleanup report")
            report = clean.analyse(contacts, settings.get("default_region", "US"), _link_pairs())
            report["created"] = time.time()
            report["backup"] = name
            report["notes"] = notes
            report["group_names"] = names
            with open(os.path.join(store.BACKUP_DIR, name, "report.json"), "w") as f:
                json.dump(report, f)
            os.chmod(os.path.join(store.BACKUP_DIR, name, "report.json"), 0o600)
            self.report = report
            backup.prune(int(settings.get("keep_days", 30)))
            store.x("UPDATE runs SET finished=?, ms_count=?, google_count=?, folder=? WHERE id=?",
                    (time.time(), report["counts"]["microsoft"], report["counts"]["google"], name, rid))
            log.info("backup %s: %d Microsoft, %d Google", name, report["counts"]["microsoft"],
                     report["counts"]["google"])
        except Exception as e:
            log.exception("run failed")
            self.progress["error"] = str(e)[:400]
            store.x("UPDATE runs SET finished=?, error=? WHERE id=?", (time.time(), str(e)[:400], rid))
        finally:
            self.progress.update(running=False, step="")

    def load_latest(self):
        """After a restart, show the last saved report."""
        for b in backup.listing():
            p = os.path.join(store.BACKUP_DIR, b["name"], "report.json")
            if os.path.exists(p):
                try:
                    with open(p) as f:
                        self.report = json.load(f)
                    return
                except (OSError, ValueError):
                    continue


def _link_pairs():
    """(ms_id, g_id) of every sync link, so linked copies count as one person on the pages."""
    try:
        import sync
        return sync.link_pairs()
    except Exception:            # no sync tables yet
        return []
