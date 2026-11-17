"""Every change to a contact goes through here.

- Write mode (Settings): "off" = nothing can be written; "test" = only contacts whose name starts
  with "Contact-Sync Test"; "on" = any contact.
- Before an edit or delete, a fresh copy of each side is read and saved: edits to the contact's
  history (any version can be put back), deletes to the recycle bin (kept 90 days, restorable to
  both accounts).
- Outlook also keeps deleted contacts in Deleted Items, and Google in Trash for 30 days.
"""
import json
import logging
import threading
import time

import google
import model
import msgraph
import net
import store

log = logging.getLogger("writer")
TEST_PREFIX = "Contact-Sync Test"
BIN_DAYS = 90
MODES = ("off", "test", "on")

SCHEMA = """
CREATE TABLE IF NOT EXISTS versions(
  id INTEGER PRIMARY KEY, ts REAL, side TEXT, cid TEXT, name TEXT, action TEXT,
  before TEXT, after TEXT, note TEXT);
CREATE INDEX IF NOT EXISTS versions_cid ON versions(cid);
CREATE TABLE IF NOT EXISTS bin(
  id INTEGER PRIMARY KEY, ts REAL, name TEXT, ms_raw TEXT, g_raw TEXT, restored REAL, note TEXT);
"""

_lock = threading.Lock()


class Refused(Exception):
    pass


def init():
    with store.connect() as db:
        db.executescript(SCHEMA)


def mode():
    m = store.load_settings().get("write_mode", "test")
    return m if m in MODES else "off"


_scope = threading.local()


class test_only:
    """`with writer.test_only():` limits writes to test contacts whatever the mode (API-key calls)."""
    def __enter__(self):
        _scope.test_only = True

    def __exit__(self, *exc):
        _scope.test_only = False


def _check(*names):
    m = mode()
    if m == "off":
        raise Refused("Writing is off (Settings → Write mode).")
    if m == "test" or getattr(_scope, "test_only", False):
        bad = [n for n in names if not (n or "").startswith(TEST_PREFIX)]
        if bad:
            raise Refused(f"Write mode is “test”: only contacts named “{TEST_PREFIX}…” can be changed "
                          f"(not “{bad[0]}”). Switch to “on” in Settings.")


def _version(side, cid, name, action, before=None, after=None, note=""):
    return store.x("INSERT INTO versions(ts, side, cid, name, action, before, after, note) VALUES(?,?,?,?,?,?,?,?)",
                   (time.time(), side, cid, name, action, json.dumps(before) if before is not None else None,
                    json.dumps(after) if after is not None else None, note))


def _fresh(member):
    """Current raw copy and common shape of one side's contact."""
    if member["side"] == "microsoft":
        raw = msgraph.get_contact(member["id"])
        return raw, model.from_graph(raw)
    raw = google.get_person(member["id"])
    return raw, model.from_people(raw, {})


def _write_side(side, cid, rec):
    """Create (cid None) or update one side from an editable record. Returns (raw after, warnings)."""
    if side == "microsoft":
        body, warn = model.to_graph(rec)
        with net.writes_allowed():
            raw = msgraph.update_contact(cid, body) if cid else msgraph.create_contact(body)
        return raw, warn
    body = model.to_people(rec)
    with net.writes_allowed():
        if cid:
            body["etag"] = google.get_person(cid)["etag"]
            raw = google.update_person(cid, body, model.GOOGLE_FIELDS)
        else:
            raw = google.create_person(body)
    return raw, []


def _one_per_side(members):
    sides = [m["side"] for m in members]
    if len(sides) != len(set(sides)):
        raise Refused("This person has duplicate copies on one side. Merge them first (cleanup).")


def update(members, rec, action="edit", note=""):
    """Write an edited record to every copy of a person (one per side). Returns warnings."""
    _one_per_side(members)
    with _lock:
        fresh = [(m, *_fresh(m)) for m in members]
        _check(model.display_name(rec), *[c["name"] for _, _, c in fresh])
        warnings = []
        for m, raw, c in fresh:
            vid = _version(m["side"], m["id"], c["name"], action, before=raw, note=note)
            after, warn = _write_side(m["side"], m["id"], rec)
            store.x("UPDATE versions SET after=? WHERE id=?", (json.dumps(after), vid))
            warnings += [f"{'Outlook' if m['side'] == 'microsoft' else 'Google'}: {w}" for w in warn]
            log.info("%s %s on %s", action, c["name"], m["side"])
        return warnings


def create(rec, sides=("microsoft", "google"), action="create", note=""):
    """Create the contact on the given sides. Returns {side: new id}, warnings."""
    with _lock:
        _check(model.display_name(rec))
        ids, warnings = {}, []
        for side in sides:
            after, warn = _write_side(side, None, rec)
            cid = after.get("id") if side == "microsoft" else after.get("resourceName")
            ids[side] = cid
            _version(side, cid, model.display_name(rec), action, after=after, note=note)
            warnings += warn
            log.info("%s %s on %s", action, model.display_name(rec), side)
        return ids, warnings


def delete(members):
    """Delete every copy of a person; both sides' data go to the recycle bin first. Returns the bin id."""
    with _lock:
        fresh = [(m, *_fresh(m)) for m in members]
        _check(*[c["name"] for _, _, c in fresh])
        ms_raw = [raw for m, raw, _ in fresh if m["side"] == "microsoft"]
        g_raw = [raw for m, raw, _ in fresh if m["side"] == "google"]
        name = next((c["name"] for _, _, c in fresh if c["name"]), "(no name)")
        bin_id = store.x("INSERT INTO bin(ts, name, ms_raw, g_raw) VALUES(?,?,?,?)",
                         (time.time(), name, json.dumps(ms_raw), json.dumps(g_raw)))
        for m, raw, c in fresh:
            _version(m["side"], m["id"], c["name"], "delete", before=raw, note=f"recycle bin #{bin_id}")
            with net.writes_allowed():
                if m["side"] == "microsoft":
                    msgraph.delete_contact(m["id"])
                else:
                    google.delete_person(m["id"])
            log.info("deleted %s on %s (bin #%d)", c["name"], m["side"], bin_id)
        return bin_id


def _best_rec(ms_raws, g_raws):
    """Editable record from saved raw copies: the most recently changed side wins."""
    cands = [model.from_graph(r) for r in ms_raws] + [model.from_people(r, {}) for r in g_raws]
    if not cands:
        raise Refused("Nothing saved for this contact.")
    best = max(cands, key=lambda c: c.get("updated") or "")
    return model.editable(best)


def restore_bin(bin_id):
    """Put a deleted contact back in both accounts. Returns (ids, warnings)."""
    row = (store.q("SELECT * FROM bin WHERE id=?", (bin_id,)) or [None])[0]
    if not row:
        raise Refused("No such item in the recycle bin.")
    if row["restored"]:
        raise Refused("Already restored.")
    rec = _best_rec(json.loads(row["ms_raw"] or "[]"), json.loads(row["g_raw"] or "[]"))
    ids, warnings = create(rec, action="restore", note=f"from recycle bin #{bin_id}")
    store.x("UPDATE bin SET restored=?, note=? WHERE id=?", (time.time(), json.dumps(ids), bin_id))
    return ids, warnings


def restore_version(version_id, members):
    """Put an earlier version back on every copy of the person."""
    row = (store.q("SELECT * FROM versions WHERE id=?", (version_id,)) or [None])[0]
    if not row or not row["before"]:
        raise Refused("That version can't be restored.")
    raw = json.loads(row["before"])
    rec = _best_rec([raw] if row["side"] == "microsoft" else [], [raw] if row["side"] == "google" else [])
    return update(members, rec, action="restore", note=f"version #{version_id}")


def history(ids):
    if not ids:
        return []
    marks = ",".join("?" * len(ids))
    return store.q(f"SELECT id, ts, side, cid, name, action, note, before IS NOT NULL AS has_before "
                   f"FROM versions WHERE cid IN ({marks}) ORDER BY id DESC LIMIT 200", tuple(ids))


def bin_items():
    return store.q("SELECT id, ts, name, restored, note, ms_raw, g_raw FROM bin ORDER BY id DESC")


def prune():
    store.x("DELETE FROM bin WHERE ts < ?", (time.time() - BIN_DAYS * 86400,))


TEST_REC = {"given": "Contact-Sync", "middle": "", "family": "Test", "company": "Example Test Co",
            "title": "", "emails": ["contact-sync-test@example.com"],
            "phones": [{"value": "(555) 555-0100", "type": "mobile"}],
            "addr": [{"type": "home", "street": "1 Example St", "city": "Springfield", "region": "CA",
                      "postal": "90000", "country": "USA"}],
            "birthday": "", "notes": "Made by contact-sync to test writing. Safe to delete."}
