"""Two-way sync between Microsoft 365 and Google.

Start: every person matched one-to-one (same email or phone with a matching name) becomes a
*link*, holding what each side looked like after the last sync (its "base"). Every contact that
exists at that moment is marked *seen*: one-sided contacts and duplicates are left alone for the
cleanup, and only contacts added later count as new.

Each run reads both accounts and, per link:
- changed on one side since the base: the changed fields are copied to the other side;
- changed on both: merged field by field; where both changed the same field differently, the
  newer edit wins and the losing value goes to the conflict log;
- gone from one side: confirmed with a direct read (Google's list can lag; Outlook keeps deleted
  contacts in Deleted Items), then the other copy is deleted. Both sides go to the recycle bin.
  More than `sync_max_deletes` in one run stops the deletes until you allow them on the Sync page.
New contacts are created on the other side, or linked when a matching unlinked contact is there.
Every write goes through writer.py: the write mode applies ("test" = only "Contact-Sync Test..."
contacts sync) and every change keeps a copy first.
"""
import json
import logging
import threading
import time

import clean
import google
import model
import msgraph
import net
import store
import writer

log = logging.getLogger("sync")
SIDES = ("microsoft", "google")
OTHER = {"microsoft": "google", "google": "microsoft"}
LABEL = {"microsoft": "Microsoft 365", "google": "Google"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS links(
  id INTEGER PRIMARY KEY, ms_id TEXT UNIQUE, g_id TEXT UNIQUE, name TEXT,
  base_ms TEXT, base_g TEXT, raw_ms TEXT, raw_g TEXT, upd_ms TEXT, upd_g TEXT, synced REAL);
CREATE TABLE IF NOT EXISTS seen(side TEXT, id TEXT, ts REAL, PRIMARY KEY(side, id));
CREATE TABLE IF NOT EXISTS conflicts(
  id INTEGER PRIMARY KEY, ts REAL, link_id INTEGER, name TEXT, field TEXT,
  kept_side TEXT, kept_value TEXT, lost_side TEXT, lost_value TEXT);
CREATE TABLE IF NOT EXISTS sync_runs(
  id INTEGER PRIMARY KEY, started REAL, finished REAL, trigger TEXT, linked INTEGER,
  created INTEGER DEFAULT 0, updated INTEGER DEFAULT 0, deleted INTEGER DEFAULT 0,
  linked_new INTEGER DEFAULT 0, conflicts INTEGER DEFAULT 0, skipped INTEGER DEFAULT 0,
  blocked TEXT, error TEXT, log TEXT);
"""

_lock = threading.Lock()
state = {"running": False, "step": ""}


def init():
    with store.connect() as db:
        db.executescript(SCHEMA)


# ---------- small helpers ----------

COL = {"microsoft": ("ms_id", "base_ms", "raw_ms", "upd_ms"), "google": ("g_id", "base_g", "raw_g", "upd_g")}


def common(side, raw, group_names=None):
    return model.from_graph(raw) if side == "microsoft" else model.from_people(raw, group_names or {})


def rec_of(side, raw):
    return model.editable(common(side, raw))


def updated_of(side, raw):
    return raw.get("lastModifiedDateTime", "") if side == "microsoft" else common(side, raw)["updated"]


def _norm(field, v):
    """For deciding whether two sides changed a field to the same thing."""
    if field == "emails":
        return sorted(e.strip().lower() for e in v or [])
    if field == "phones":
        return sorted(clean.norm_phone(p["value"])[0] or p["value"] for p in v or [])
    if field == "addr":
        return sorted(model.addr_line(a).lower() for a in v or [])
    return " ".join(str(v or "").split()).lower()


def is_test(name):
    return (name or "").startswith(writer.TEST_PREFIX)


def links():
    return store.q("SELECT * FROM links")


def enabled():
    return bool(store.load_settings().get("sync_enabled"))


def _save_link(link_id, side, raw):
    id_col, base_col, raw_col, upd_col = COL[side]
    store.x(f"UPDATE links SET {id_col}=?, {base_col}=?, {raw_col}=?, {upd_col}=?, synced=? WHERE id=?",
            (_id(side, raw), json.dumps(rec_of(side, raw)), json.dumps(raw), updated_of(side, raw), time.time(), link_id))


def _id(side, raw):
    return raw.get("id") if side == "microsoft" else raw.get("resourceName")


def _new_link(ms_raw, g_raw):
    name = common("microsoft", ms_raw)["name"] or common("google", g_raw)["name"]
    lid = store.x("INSERT INTO links(ms_id, g_id, name) VALUES(?,?,?)", (ms_raw["id"], g_raw["resourceName"], name))
    _save_link(lid, "microsoft", ms_raw)
    _save_link(lid, "google", g_raw)
    for side, raw in (("microsoft", ms_raw), ("google", g_raw)):
        store.x("INSERT OR IGNORE INTO seen(side, id, ts) VALUES(?,?,?)", (side, _id(side, raw), time.time()))
    return lid


# ---------- reading ----------

def _fetch():
    items, folders = msgraph.fetch_all()
    people, groups = google.fetch_all()
    folder_ids = {f["id"] for f in folders} | {c.get("parentFolderId") for c in items if c.get("parentFolderId")}
    names = {g["resourceName"]: g.get("formattedName") or g.get("name") for g in groups}
    return {"microsoft": {c["id"]: c for c in items}, "google": {p["resourceName"]: p for p in people}}, folder_ids, names


def really_gone(side, cid, folder_ids):
    """The list says it's gone: check directly. Returns the raw copy if it still exists, else None.
    Outlook: a deleted contact sits in Deleted Items, outside every contact folder.
    Google: the list can lag behind; a trashed contact reads as not found or deleted."""
    try:
        if side == "microsoft":
            raw = msgraph.get_contact(cid)
            return raw if raw.get("parentFolderId") in folder_ids else None
        raw = google.get_person(cid)
        return None if (raw.get("metadata") or {}).get("deleted") else raw
    except net.HttpError as e:
        if e.status in (404, 410):
            return None
        raise


# ---------- start ----------

def start():
    """Link every one-to-one match and mark everything that exists now as seen, then turn sync on."""
    with _lock:
        if store.q("SELECT COUNT(*) AS n FROM links")[0]["n"]:
            raise writer.Refused("Sync was already started. Pause and resume it instead.")
        data, _, names = _fetch()
        contacts = [common(s, raw, names) for s in SIDES for raw in data[s].values()]
        report = clean.analyse(contacts, store.load_settings().get("default_region", "US"))
        n = 0
        for p in report["people"]:
            if p["ms"] == 1 and p["google"] == 1:
                ms = next(c for c in p["members"] if c["side"] == "microsoft")
                g = next(c for c in p["members"] if c["side"] == "google")
                _new_link(data["microsoft"][ms["id"]], data["google"][g["id"]])
                n += 1
        now = time.time()
        with store.connect() as db:
            db.executemany("INSERT OR IGNORE INTO seen(side, id, ts) VALUES(?,?,?)",
                           [(s, cid, now) for s in SIDES for cid in data[s]])
        s = store.load_settings()
        s["sync_enabled"] = True
        store.save_settings(s)
        log.info("sync started: %d linked, %d contacts seen", n, sum(len(data[x]) for x in SIDES))
        return n


# ---------- a run ----------

def run(trigger="schedule", allow_deletes=False):
    """One sync pass. Returns the run row id."""
    if not _lock.acquire(blocking=False):
        return None
    state.update(running=True, step="Reading both accounts")
    rid = store.x("INSERT INTO sync_runs(started, trigger) VALUES(?,?)", (time.time(), trigger))
    c = {"created": 0, "updated": 0, "deleted": 0, "linked_new": 0, "conflicts": 0, "skipped": 0}
    lines, blocked = [], None
    try:
        if not enabled():
            raise RuntimeError("Sync is paused.")
        if writer.mode() == "off":
            raise RuntimeError("Write mode is off, so nothing can be synced.")
        test = writer.mode() == "test"
        data, folder_ids, names = _fetch()
        all_links = links()
        for side in SIDES:          # a side that suddenly reads (nearly) empty is an outage, not deletes
            have = sum(1 for lk in all_links if lk[COL[side][0]])
            if have >= 10 and len(data[side]) < have * 0.5:
                raise RuntimeError(f"{LABEL[side]} returned only {len(data[side])} contacts for {have} linked: "
                                   "stopped, nothing changed.")
        state["step"] = "Comparing"

        # 1) links where a side is missing from the list
        gone = []
        for lk in all_links:
            missing = [s for s in SIDES if lk[COL[s][0]] not in data[s]]
            for s in missing:
                raw = really_gone(s, lk[COL[s][0]], folder_ids)
                if raw is not None:                         # still there: the list was just behind
                    data[s][lk[COL[s][0]]] = raw
            missing = [s for s in SIDES if lk[COL[s][0]] not in data[s]]
            if missing:
                gone.append((lk, missing))
        to_delete = [(lk, m[0]) for lk, m in gone if len(m) == 1]
        max_del = int(store.load_settings().get("sync_max_deletes", 10))
        if len(to_delete) > max_del and not allow_deletes:
            blocked = (f"{len(to_delete)} linked contacts were deleted on one side (more than {max_del}). "
                       "Deletes were held back; check them on the Sync page and allow them if that's right.")
            to_delete = []
        for lk, m in gone:
            if len(m) == 2:                                 # gone from both: just forget the link
                store.x("DELETE FROM links WHERE id=?", (lk["id"],))
        for lk, gone_side in to_delete:
            keep = OTHER[gone_side]
            if test and not is_test(lk["name"]):
                c["skipped"] += 1
                continue
            try:
                writer.delete([{"side": keep, "id": lk[COL[keep][0]]}],
                              also_saved={gone_side: [json.loads(lk[COL[gone_side][2]] or "{}")]},
                              note=f"sync: deleted in {LABEL[gone_side]}")
                store.x("DELETE FROM links WHERE id=?", (lk["id"],))
                c["deleted"] += 1
                lines.append(f"Deleted “{lk['name']}” from {LABEL[keep]} (deleted in {LABEL[gone_side]})")
            except writer.Refused as e:
                c["skipped"] += 1
                lines.append(f"Skipped deleting “{lk['name']}”: {e}")

        # 2) links present on both sides
        state["step"] = "Copying changes"
        for lk in links():
            a, b = data["microsoft"].get(lk["ms_id"]), data["google"].get(lk["g_id"])
            if a is None or b is None:
                continue
            _merge(lk, a, b, test, c, lines)

        # 3) contacts that appeared since the last run
        state["step"] = "Adding new contacts"
        _new_contacts(data, names, test, c, lines)
    except Exception as e:
        log.exception("sync run failed")
        store.x("UPDATE sync_runs SET error=? WHERE id=?", (str(e)[:500], rid))
    finally:
        store.x("UPDATE sync_runs SET finished=?, linked=?, created=?, updated=?, deleted=?, linked_new=?, "
                "conflicts=?, skipped=?, blocked=?, log=? WHERE id=?",
                (time.time(), store.q("SELECT COUNT(*) AS n FROM links")[0]["n"], c["created"], c["updated"],
                 c["deleted"], c["linked_new"], c["conflicts"], c["skipped"], blocked, json.dumps(lines[:300]), rid))
        store.x("DELETE FROM sync_runs WHERE started < ?", (time.time() - 30 * 86400,))
        state.update(running=False, step="")
        _lock.release()
    return rid


def _merge(lk, a, b, test, c, lines):
    ra, rb = rec_of("microsoft", a), rec_of("google", b)
    base_a, base_b = json.loads(lk["base_ms"] or "{}"), json.loads(lk["base_g"] or "{}")
    ca = {f for f in model.EDIT_FIELDS if ra.get(f) != base_a.get(f)}
    cb = {f for f in model.EDIT_FIELDS if rb.get(f) != base_b.get(f)}
    if not ca and not cb:
        if updated_of("microsoft", a) != lk["upd_ms"]:
            _save_link(lk["id"], "microsoft", a)        # something the sync doesn't copy changed: keep the copy current
        if updated_of("google", b) != lk["upd_g"]:
            _save_link(lk["id"], "google", b)
        return
    name = common("microsoft", a)["name"] or lk["name"]
    if test and not (is_test(name) and is_test(common("google", b)["name"])):
        c["skipped"] += 1
        return
    newer_ms = updated_of("microsoft", a) >= updated_of("google", b)
    to_a, to_b = dict(ra), dict(rb)
    for f in sorted(ca | cb):
        if f in ca and f not in cb:
            to_b[f] = ra[f]
        elif f in cb and f not in ca:
            to_a[f] = rb[f]
        elif _norm(f, ra[f]) != _norm(f, rb[f]):
            win, lose = ("microsoft", "google") if newer_ms else ("google", "microsoft")
            val = ra[f] if newer_ms else rb[f]
            lost = rb[f] if newer_ms else ra[f]
            to_a[f], to_b[f] = val, val
            store.x("INSERT INTO conflicts(ts, link_id, name, field, kept_side, kept_value, lost_side, lost_value) "
                    "VALUES(?,?,?,?,?,?,?,?)", (time.time(), lk["id"], name, f, win, json.dumps(val),
                                                lose, json.dumps(lost)))
            c["conflicts"] += 1
            lines.append(f"Conflict on “{name}” ({f}): kept the newer {LABEL[win]} value")
    try:
        for side, rec, cur, src in (("google", to_b, rb, "microsoft"), ("microsoft", to_a, ra, "google")):
            if rec != cur:
                writer.update([{"side": side, "id": lk[COL[side][0]]}], rec, action="sync",
                              note=f"sync: changes from {LABEL[src]}")
                c["updated"] += 1
                changed = sorted(f for f in model.EDIT_FIELDS if rec.get(f) != cur.get(f))
                lines.append(f"Updated “{name}” in {LABEL[side]}: {', '.join(changed)}")
    except writer.Refused as e:
        c["skipped"] += 1
        lines.append(f"Skipped “{name}”: {e}")
    # the new bases are what each side looks like now
    for side in SIDES:
        raw, _ = writer._fresh({"side": side, "id": lk[COL[side][0]]})
        _save_link(lk["id"], side, raw)


def _new_contacts(data, names, test, c, lines):
    seen = {(r["side"], r["id"]) for r in store.q("SELECT side, id FROM seen")}
    linked = {("microsoft", lk["ms_id"]) for lk in links()} | {("google", lk["g_id"]) for lk in links()}
    region = store.load_settings().get("default_region", "US")
    unlinked = {s: [common(s, raw, names) for cid, raw in data[s].items() if (s, cid) not in linked] for s in SIDES}
    for side in SIDES:
        other = OTHER[side]
        for cid, raw in data[side].items():
            if (side, cid) in seen or (side, cid) in linked:
                continue
            new = common(side, raw, names)
            if test and not is_test(new["name"]):
                c["skipped"] += 1
                continue                        # not marked seen: it syncs once writing is "on"
            keys = clean.keys(new, region)
            match = next((o for o in unlinked[other] if (other, o["id"]) not in linked
                          and keys & clean.keys(o, region) and clean.names_match(new, o)), None)
            try:
                if match:
                    pair = {side: raw, other: data[other][match["id"]]}
                    _new_link(pair["microsoft"], pair["google"])
                    linked |= {("microsoft", _id("microsoft", pair["microsoft"])), ("google", _id("google", pair["google"]))}
                    c["linked_new"] += 1
                    lines.append(f"Linked new “{new['name']}” in {LABEL[side]} to the same contact in {LABEL[other]}")
                else:
                    ids, _ = writer.create(model.editable(new), sides=(other,), action="sync",
                                           note=f"sync: new in {LABEL[side]}")
                    made, _ = writer._fresh({"side": other, "id": ids[other]})
                    pair = {side: raw, other: made}
                    _new_link(pair["microsoft"], pair["google"])
                    linked |= {(side, cid), (other, ids[other])}
                    c["created"] += 1
                    lines.append(f"Copied new “{new['name']}” from {LABEL[side]} to {LABEL[other]}")
            except writer.Refused as e:
                c["skipped"] += 1
                lines.append(f"Skipped new “{new['name']}”: {e}")
                continue
            store.x("INSERT OR IGNORE INTO seen(side, id, ts) VALUES(?,?,?)", (side, cid, time.time()))


# ---------- keeping links right after changes made from the pages ----------

def after_write(removed=(), upserted=()):
    """Called after every write (pages, restores, sync itself). An edited contact's base becomes
    what was just written, so the next run doesn't copy it again; deleted contacts lose their
    link; a contact restored to both sides at once is linked."""
    if not store.q("SELECT 1 FROM sqlite_master WHERE name='links'"):
        return
    for side, cid in removed:
        col = COL[side][0]
        store.x(f"DELETE FROM links WHERE {col}=? AND {COL[OTHER[side]][0]} IS NULL", (cid,))
        store.x(f"UPDATE links SET {col}=NULL WHERE {col}=?", (cid,))
        store.x("DELETE FROM links WHERE ms_id IS NULL AND g_id IS NULL")
    for side, raw in upserted:
        rows = store.q(f"SELECT id FROM links WHERE {COL[side][0]}=?", (_id(side, raw),))
        for r in rows:
            _save_link(r["id"], side, raw)
        store.x("INSERT OR IGNORE INTO seen(side, id, ts) VALUES(?,?,?)", (side, _id(side, raw), time.time()))
    by_side = {s: [raw for x, raw in upserted if x == s] for s in SIDES}
    if len(by_side["microsoft"]) == 1 and len(by_side["google"]) == 1:
        ms, g = by_side["microsoft"][0], by_side["google"][0]
        if not store.q("SELECT 1 FROM links WHERE ms_id=? OR g_id=?", (ms["id"], g["resourceName"])):
            _new_link(ms, g)


# ---------- for the pages ----------

def status():
    last = (store.q("SELECT * FROM sync_runs ORDER BY id DESC LIMIT 1") or [None])[0]
    s = store.load_settings()
    return {"enabled": bool(s.get("sync_enabled")), "minutes": int(s.get("sync_minutes", 60)),
            "max_deletes": int(s.get("sync_max_deletes", 10)), "running": state["running"], "step": state["step"],
            "linked": store.q("SELECT COUNT(*) AS n FROM links")[0]["n"],
            "started": bool(store.q("SELECT COUNT(*) AS n FROM seen")[0]["n"]), "last": last}


def recent_runs(n=20):
    return store.q("SELECT * FROM sync_runs ORDER BY id DESC LIMIT ?", (n,))


def recent_conflicts(n=50):
    return store.q("SELECT * FROM conflicts ORDER BY id DESC LIMIT ?", (n,))


def pause(on):
    s = store.load_settings()
    s["sync_enabled"] = bool(on)
    store.save_settings(s)
