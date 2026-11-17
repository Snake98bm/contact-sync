"""Every change to a contact goes through here.

- Write mode (Settings): "off" = nothing can be written; "test" = only contacts whose name starts
  with "Contact-Sync Test"; "on" = any contact.
- Before an edit or delete, a fresh copy of each side is read and saved: edits to the contact's
  history (any version can be put back), deletes to the recycle bin (kept 2 years, restorable to
  both accounts). Restores write the saved copy back as it was, field for field.
- Edits are merged into the current data, so fields the edit page doesn't show are kept.
- A change that fails half-way (one side done, the other not) is recorded, and a retry of a
  restore only finishes the missing side.
- Outlook also keeps deleted contacts in Deleted Items, and Google in Trash for 30 days.
"""
import json
import logging
import threading
import time
import uuid

import google
import model
import msgraph
import net
import store

log = logging.getLogger("writer")
TEST_PREFIX = "Contact-Sync Test"
BIN_DAYS = 730                  # 2 years
MODES = ("off", "test", "on")
SIDE_NAME = {"microsoft": "Microsoft 365", "google": "Google"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS versions(
  id INTEGER PRIMARY KEY, ts REAL, side TEXT, cid TEXT, name TEXT, action TEXT,
  before TEXT, after TEXT, note TEXT, batch TEXT);
CREATE INDEX IF NOT EXISTS versions_cid ON versions(cid);
CREATE TABLE IF NOT EXISTS bin(
  id INTEGER PRIMARY KEY, ts REAL, name TEXT, ms_raw TEXT, g_raw TEXT, restored REAL, note TEXT);
"""

_lock = threading.Lock()
_scope = threading.local()
listener = None          # set by main: called as listener(removed=[(side, id)], upserted=[(side, raw)])


class Refused(Exception):
    pass


def init():
    with store.connect() as db:
        db.executescript(SCHEMA)
        cols = {r[1] for r in db.execute("PRAGMA table_info(versions)")}
        if "batch" not in cols:                      # tables made by 0.5.0
            db.execute("ALTER TABLE versions ADD COLUMN batch TEXT")


def mode():
    m = store.load_settings().get("write_mode", "test")
    return m if m in MODES else "off"


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


def _notify(removed=(), upserted=()):
    if listener:
        try:
            listener(removed=list(removed), upserted=list(upserted))
        except Exception:
            log.exception("updating the page data after a change failed")


def _version(side, cid, name, action, before=None, after=None, note="", batch=None):
    return store.x("INSERT INTO versions(ts, side, cid, name, action, before, after, note, batch) "
                   "VALUES(?,?,?,?,?,?,?,?,?)",
                   (time.time(), side, cid, name, action, json.dumps(before) if before is not None else None,
                    json.dumps(after) if after is not None else None, note, batch))


def _fresh(member):
    """Current raw copy and common shape of one side's contact."""
    if member["side"] == "microsoft":
        raw = msgraph.get_contact(member["id"])
        return raw, model.from_graph(raw)
    raw = google.get_person(member["id"])
    return raw, model.from_people(raw, {})


def _id_of(side, raw):
    return raw.get("id") if side == "microsoft" else raw.get("resourceName")


def _send(side, cid, rec=None, base=None, raw=None):
    """One write. rec: edit merged onto base (the current raw). raw: a saved copy written back as
    it was. cid None = create. Returns (raw after, warnings)."""
    warn = []
    with net.writes_allowed():
        if side == "microsoft":
            if raw is not None:
                body = model.graph_writable(raw)
            else:
                body, warn = model.to_graph(rec, base)
            out = msgraph.update_contact(cid, body) if cid else msgraph.create_contact(body)
        else:
            if raw is not None:
                body, fields = model.people_writable(raw), model.GOOGLE_ALL_FIELDS
            else:
                body, fields = model.to_people(rec, base), model.GOOGLE_FIELDS
            if cid:
                body["etag"] = (base or {}).get("etag") or google.get_person(cid)["etag"]
                out = google.update_person(cid, body, fields)
            else:
                try:
                    out = google.create_person(body)
                except net.HttpError as e:
                    if raw is not None and body.get("memberships") and e.status in (400, 404):   # a label it was in is gone (Google says 404 or 400)
                        # a label it belonged to no longer exists: restore without labels
                        body["memberships"] = []
                        out = google.create_person(body)
                        warn.append("a Google label it was in no longer exists, so it was restored without labels")
                    else:
                        raise
                # a contact outside "My Contacts" lives only in its labels and vanishes if a label goes
                groups = [(m.get("contactGroupMembership") or {}).get("contactGroupResourceName")
                          for m in out.get("memberships") or []]
                if "contactGroups/myContacts" not in groups and out.get("resourceName"):
                    google.add_to_my_contacts([out["resourceName"]])
    return out, warn


def _one_per_side(members):
    sides = [m["side"] for m in members]
    if len(sides) != len(set(sides)):
        raise Refused("This person has duplicate copies on one side. Merge them first (cleanup).")


def _partial(done, side, e):
    ok = ", ".join(SIDE_NAME[s] for s in done) or "nothing"
    return Refused(f"Done on {ok}; failed on {SIDE_NAME[side]}: {e}. The two copies may differ now: "
                   "check the contact and try again.")


def update(members, rec, action="edit", note=""):
    """Write an edited record to every copy of a person (one per side). Returns warnings."""
    _one_per_side(members)
    with _lock:
        fresh = [(m, *_fresh(m)) for m in members]
        _check(model.display_name(rec), *[c["name"] for _, _, c in fresh])
        batch, warnings, done, upserted = uuid.uuid4().hex, [], [], []
        for m, raw, c in fresh:
            vid = _version(m["side"], m["id"], c["name"], action, before=raw, note=note, batch=batch)
            try:
                after, warn = _send(m["side"], m["id"], rec=rec, base=raw)
            except Exception as e:
                store.x("UPDATE versions SET note=? WHERE id=?", (f"{note} FAILED: {e}"[:400], vid))
                _notify(upserted=upserted)
                raise _partial(done, m["side"], e) from None
            store.x("UPDATE versions SET after=? WHERE id=?", (json.dumps(after), vid))
            done.append(m["side"])
            upserted.append((m["side"], after))
            warnings += [f"{SIDE_NAME[m['side']]}: {w}" for w in warn]
            log.info("%s %s on %s", action, c["name"], m["side"])
        _notify(upserted=upserted)
        return warnings


def create(rec, sides=("microsoft", "google"), action="create", note=""):
    """Create the contact on the given sides. Returns ({side: new id}, warnings)."""
    with _lock:
        _check(model.display_name(rec))
        ids, warnings, upserted, batch = {}, [], [], uuid.uuid4().hex
        try:
            for side in sides:
                try:
                    after, warn = _send(side, None, rec=rec)
                except Exception as e:
                    raise _partial(list(ids), side, e) from None
                ids[side] = _id_of(side, after)
                upserted.append((side, after))
                _version(side, ids[side], model.display_name(rec), action, after=after, note=note, batch=batch)
                warnings += warn
                log.info("%s %s on %s", action, model.display_name(rec), side)
        finally:
            _notify(upserted=upserted)
        return ids, warnings


def delete(members, also_saved=None, note=""):
    """Delete every copy of a person; both sides' data go to the recycle bin first. Returns the bin id.
    also_saved: {side: [raw]} for a copy that is already gone (sync: deleted in the other account),
    so the bin holds both sides."""
    also_saved = also_saved or {}
    with _lock:
        fresh = [(m, *_fresh(m)) for m in members]
        _check(*[c["name"] for _, _, c in fresh])
        ms_raw = [raw for m, raw, _ in fresh if m["side"] == "microsoft"] or list(also_saved.get("microsoft") or [])
        g_raw = [raw for m, raw, _ in fresh if m["side"] == "google"] or list(also_saved.get("google") or [])
        name = next((c["name"] for _, _, c in fresh if c["name"]), "(no name)")
        bin_id = store.x("INSERT INTO bin(ts, name, ms_raw, g_raw) VALUES(?,?,?,?)",
                         (time.time(), name, json.dumps(ms_raw), json.dumps(g_raw)))
        batch, done, removed = uuid.uuid4().hex, [], []
        try:
            for m, raw, c in fresh:
                vid = _version(m["side"], m["id"], c["name"], "delete", before=raw, note=(note + " " if note else "") + f"recycle bin #{bin_id}",
                               batch=batch)
                try:
                    with net.writes_allowed():
                        if m["side"] == "microsoft":
                            msgraph.delete_contact(m["id"])
                        else:
                            google.delete_person(m["id"])
                except Exception as e:
                    store.x("UPDATE versions SET note=? WHERE id=?", (f"recycle bin #{bin_id} FAILED: {e}"[:400], vid))
                    raise _partial(done, m["side"], e) from None
                done.append(m["side"])
                removed.append((m["side"], m["id"]))
                log.info("deleted %s on %s (bin #%d)", c["name"], m["side"], bin_id)
        finally:
            _notify(removed=removed)
        return bin_id


def restore_bin(bin_id):
    """Put a deleted contact back in both accounts, each side from its own saved copy (as it was).
    A side with no saved copy is made from the other side's data. If one side fails, the other's
    new id is remembered, so trying again only finishes the missing side. Returns (ids, warnings)."""
    with _lock:
        row = (store.q("SELECT * FROM bin WHERE id=?", (bin_id,)) or [None])[0]
        if not row:
            raise Refused("No such item in the recycle bin.")
        if row["restored"]:
            raise Refused("Already restored.")
        saved = {"microsoft": json.loads(row["ms_raw"] or "[]"), "google": json.loads(row["g_raw"] or "[]")}
        _check(row["name"])
        try:
            ids = json.loads(row["note"] or "{}")
            ids = ids if isinstance(ids, dict) else {}
        except ValueError:
            ids = {}
        rec = _best_rec(saved["microsoft"], saved["google"])
        warnings, upserted, batch = [], [], uuid.uuid4().hex
        try:
            for side in ("microsoft", "google"):
                if ids.get(side):
                    continue                                   # done by an earlier, half-finished try
                try:
                    if saved[side]:
                        after, warn = _send(side, None, raw=saved[side][0])
                    else:
                        after, warn = _send(side, None, rec=rec)
                except Exception as e:
                    raise _partial([s for s in ids if ids[s]], side, e) from None
                ids[side] = _id_of(side, after)
                store.x("UPDATE bin SET note=? WHERE id=?", (json.dumps(ids), bin_id))
                upserted.append((side, after))
                _version(side, ids[side], row["name"], "restore", after=after, note=f"from recycle bin #{bin_id}",
                         batch=batch)
                warnings += [f"{SIDE_NAME[side]}: {w}" for w in warn]
            store.x("UPDATE bin SET restored=? WHERE id=?", (time.time(), bin_id))
        finally:
            _notify(upserted=upserted)
        return ids, warnings


def _best_rec(ms_raws, g_raws):
    """Editable record from saved raw copies: the most recently changed side wins."""
    cands = [model.from_graph(r) for r in ms_raws] + [model.from_people(r, {}) for r in g_raws]
    if not cands:
        raise Refused("Nothing saved for this contact.")
    return model.editable(max(cands, key=lambda c: c.get("updated") or ""))


def restore_version(version_id, members):
    """Put back the state from before a change, on every copy of the person: each side from its
    own saved copy of that change; a side without one gets the other side's data."""
    _one_per_side(members)
    row = (store.q("SELECT * FROM versions WHERE id=?", (version_id,)) or [None])[0]
    if not row or not row["before"]:
        raise Refused("That version can't be restored.")
    same = store.q("SELECT * FROM versions WHERE batch=? AND before IS NOT NULL", (row["batch"],)) if row["batch"] else [row]
    saved = {r["side"]: json.loads(r["before"]) for r in same}
    rec = _best_rec([saved["microsoft"]] if "microsoft" in saved else [], [saved["google"]] if "google" in saved else [])
    with _lock:
        fresh = [(m, *_fresh(m)) for m in members]
        names = [c["name"] for _, _, c in fresh] + [model.display_name(rec)]
        _check(*names)
        batch, warnings, done, upserted = uuid.uuid4().hex, [], [], []
        for m, raw, c in fresh:
            vid = _version(m["side"], m["id"], c["name"], "restore", before=raw, note=f"version #{version_id}", batch=batch)
            try:
                if m["side"] in saved:
                    after, warn = _send(m["side"], m["id"], raw=saved[m["side"]], base=raw)
                else:
                    after, warn = _send(m["side"], m["id"], rec=rec, base=raw)
            except Exception as e:
                store.x("UPDATE versions SET note=? WHERE id=?", (f"version #{version_id} FAILED: {e}"[:400], vid))
                _notify(upserted=upserted)
                raise _partial(done, m["side"], e) from None
            store.x("UPDATE versions SET after=? WHERE id=?", (json.dumps(after), vid))
            done.append(m["side"])
            upserted.append((m["side"], after))
            warnings += [f"{SIDE_NAME[m['side']]}: {w}" for w in warn]
        _notify(upserted=upserted)
        return warnings


def patch_graph(cid, patch, name, note=""):
    """Change Outlook-only properties (e.g. categories) on one Microsoft contact, keeping a copy first."""
    with _lock:
        _check(name)
        raw = msgraph.get_contact(cid)
        vid = _version("microsoft", cid, name, "cleanup", before=raw, note=note, batch=uuid.uuid4().hex)
        with net.writes_allowed():
            after = msgraph.update_contact(cid, patch)
        store.x("UPDATE versions SET after=? WHERE id=?", (json.dumps(after), vid))
    _notify(upserted=[("microsoft", after)])
    return after


def delete_label(group_rn, name):
    """Delete a Google label; its contacts are kept. Only with write mode "on".
    Google deletes a contact whose only group is the label being deleted, even with deleteContacts=false
    (seen 2026-11-17), so every member is first put in My Contacts and that is checked before the delete."""
    if mode() != "on":
        raise Refused("Deleting a label needs write mode “on”.")
    with _lock:
        members = google.group_members(group_rn)
        with net.writes_allowed():
            google.add_to_my_contacts(members)
        mine = set(google.group_members("contactGroups/myContacts"))
        missing = [m for m in members if m not in mine]
        if missing:
            raise Refused(f"{len(missing)} contacts in “{name}” couldn't be put in My Contacts, so the label was "
                          "left alone (deleting it would delete them).")
        _version("google", group_rn, name, "label delete",
                 note=f"label deleted; its {len(members)} contacts were kept (all in My Contacts first)")
        with net.writes_allowed():
            google.delete_group(group_rn)


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
