"""The one-time cleanup: plan every change, let the owner tick what to do, then apply it.

Plan (built from a fresh read of both accounts):
- tidy:    per contact: remove the "Contact Imported" notes block, rebuild "Last, First" names,
           phones as (559) 555-1234, lowercase emails, decode &amp; and similar codes.
- combine: one person with copies to merge (duplicates on a side, and/or a one-sided contact
           in each account that is the same person): every email, phone, address and note is
           kept, one copy per side remains, extra copies go to the recycle bin, and the two sides
           are linked for sync.
- copy:    a contact on one side only is created on the other and linked.
- label:   delete a Google label (its contacts stay).
- category: give the Outlook copies of a Google label's members an Outlook category.
Safe items start ticked; judgement calls start unticked.

Apply: a fresh backup first, then the ticked items in that order. Every change goes through
writer.py (history, recycle bin, write mode) and each item is re-read before it is changed.
"""
import html
import json
import logging
import threading
import time

import clean
import google
import model
import msgraph
import store
import sync
import writer

log = logging.getLogger("cleanup")
LABEL = {"microsoft": "Microsoft 365", "google": "Google"}
ORDER = ("tidy", "combine", "shared", "copy", "label", "category")
PAUSE = 0.6               # between writes: stays well under Google's per-minute write limit

SCHEMA = """
CREATE TABLE IF NOT EXISTS cleanup_plans(id INTEGER PRIMARY KEY, created REAL, plan TEXT, applied REAL, result TEXT);
"""

state = {"running": False, "step": "", "done": 0, "total": 0, "log": [], "plan_id": None}
_lock = threading.Lock()


def init():
    with store.connect() as db:
        db.executescript(SCHEMA)


# ---------- record transforms ----------

def _unescape(s):
    return " ".join(html.unescape(s or "").split())


def tidy(c, region="US"):
    """(tidied editable record, reasons) for one contact in the common shape."""
    rec = model.editable(c)
    reasons = []
    own, block = clean.split_import_block(rec.get("notes") or "")
    if block:
        if rec.get("addr") or "street" not in block.lower():
            rec["notes"] = own
            reasons.append("remove “Contact Imported” block")
        else:
            reasons.append("keep “Contact Imported” block (its address isn't in the address fields)")
    phones, reformatted, dupes = [], False, 0
    for p in rec.get("phones") or []:
        e164, ext = clean.norm_phone(p["value"], region)
        v = clean.pretty_phone(e164, ext) if e164 else p["value"]
        key = (e164, ext) if e164 else " ".join(p["value"].split()).lower()
        if any(((clean.norm_phone(x["value"], region)[0], clean.norm_phone(x["value"], region)[1])
                if clean.norm_phone(x["value"], region)[0] else " ".join(x["value"].split()).lower()) == key
               for x in phones):
            dupes += 1                                       # same number listed twice: keep the first
            continue
        reformatted |= v != p["value"]
        phones.append(dict(p, value=v))
    if reformatted or dupes:
        rec["phones"] = phones
        if reformatted:
            reasons.append("phone format")
        if dupes:
            reasons.append(f"remove duplicate phone{'s' if dupes > 1 else ''}")
    emails = []
    for e in rec.get("emails") or []:
        if e.strip().lower() not in emails:
            emails.append(e.strip().lower())
    if emails != (rec.get("emails") or []):
        rec["emails"] = emails
        reasons.append("lowercase emails")
    for k in ("given", "middle", "family", "company", "title"):
        v = _unescape(rec.get(k))
        if v != (rec.get(k) or ""):
            rec[k] = v
            if "fix name codes/spaces" not in reasons:
                reasons.append("fix name codes/spaces")
    if c["side"] == "google" and ", " in (c.get("shown_as") or "") and (rec.get("given") or rec.get("family")):
        reasons.append("name order “Last, First” → “First Last”")
    return rec, [r for r in reasons if not r.startswith("keep")], [r for r in reasons if r.startswith("keep")]


def combine_recs(recs, region="US"):
    """One record holding everything from several copies of the same person."""
    if len(recs) == 1:
        return dict(recs[0])
    filled = lambda r: sum(1 for k in model.EDIT_FIELDS if r.get(k))
    out = dict(max(recs, key=filled))
    named = max(recs, key=lambda r: len(model.display_name(r)))
    for k in ("given", "middle", "family"):
        out[k] = named.get(k) or out.get(k) or ""
    for k in ("company", "title", "birthday"):
        out[k] = out.get(k) or next((r[k] for r in recs if r.get(k)), "")
    emails, phones, addr, notes = [], [], [], []
    for r in recs:
        for e in r.get("emails") or []:
            if e.lower() not in [x.lower() for x in emails]:
                emails.append(e)
        for p in r.get("phones") or []:
            key = clean.norm_phone(p["value"], region)[0] or p["value"]
            if key not in [clean.norm_phone(x["value"], region)[0] or x["value"] for x in phones]:
                phones.append(p)
        for a in r.get("addr") or []:
            if model.addr_line(a).lower() not in [model.addr_line(x).lower() for x in addr]:
                addr.append(a)
        n = (r.get("notes") or "").strip()
        if n and not any(n in x or x in n for x in notes):
            notes = [x for x in notes if x not in n] + [n]
    out.update(emails=emails, phones=phones, addr=addr, notes="\n\n".join(notes))
    return out


# ---------- plan ----------

def build_plan():
    data, _, names = sync._fetch()
    region = store.load_settings().get("default_region", "US")
    contacts = [sync.common(s, raw, names) for s in sync.SIDES for raw in data[s].values()]
    rep = clean.analyse(contacts, region)
    linked = {("microsoft", lk["ms_id"]) for lk in sync.links()} | {("google", lk["g_id"]) for lk in sync.links()}
    items = []

    def add(kind, title, detail, members, default, **extra):
        items.append({"id": len(items) + 1, "kind": kind, "title": title, "detail": detail, "default": default,
                      "members": [{"side": c["side"], "id": c["id"], "name": c["name"]} for c in members], **extra})

    # combine: duplicates, then possible matches between one-sided contacts
    in_combine = set()
    for p in rep["duplicates"]:
        keys = {clean.name_key(c) for c in p["members"]} - {""}
        same = len(keys) <= 1
        add("combine", p["name"] or "(no name)",
            f"{p['ms']} in Microsoft 365, {p['google']} in Google → one in each"
            + ("" if same else ". Names differ: check they are really the same"),
            p["members"], same)
        in_combine |= {(c["side"], c["id"]) for c in p["members"]}
    for g in rep["possible"]:
        mem = [c for pp in g["people"] for c in pp["members"] if (c["side"], c["id"]) not in in_combine]
        sides = {c["side"] for c in mem}
        if len(sides) < 2:               # (an already linked copy is fine: the extra one merges into it)
            continue
        same = not g["name"].startswith("same ")
        add("combine", mem[0]["name"] or "(no name)",
            ("Same name, one in each account → link them" if same
             else "Same phone or email, different names → link them as one person"),
            mem, same)
        in_combine |= {(c["side"], c["id"]) for c in mem}

    # different names sharing a phone or email: the owner decides (households, a business and its staff)
    for s in rep["shared_info"]:
        mem = s["contacts"]
        if any((c["side"], c["id"]) in in_combine for c in mem):
            continue
        names_ = sorted({c["name"] or "(no name)" for c in mem})
        add("shared", " / ".join(names_), f"Same {s['kind']} {s['value']} on different names. Tick to combine them "
            "into one contact; leave unticked to keep them as separate people.", mem, False)
        in_combine |= {(c["side"], c["id"]) for c in mem}

    # copy: one side only, not part of any combine
    for p in rep["people"]:
        if (p["ms"] == 0 or p["google"] == 0) and not any((c["side"], c["id"]) in in_combine for c in p["members"]):
            c = p["members"][0]
            if len(p["members"]) == 1:
                add("copy", c["name"] or "(no name)", f"Only in {LABEL[c['side']]} → copy to "
                    f"{LABEL[sync.OTHER[c['side']]]}", [c], True)

    # tidy: every contact that needs a fix. Tidy runs first; combine and copy re-read afterwards.
    for c in contacts:
        _, reasons, notes = tidy(c, region)
        if reasons:
            add("tidy", c["name"] or "(no name)", "; ".join(reasons), [c], True, side=c["side"])
        elif notes:
            add("tidy", c["name"] or "(no name)", "; ".join(notes) + " — nothing to change", [c], False,
                side=c["side"], noop=True)

    # labels
    groups = {name: rn for rn, name in names.items()}
    if "Imported on 9/6" in groups:
        n = sum(1 for c in contacts if "Imported on 9/6" in c["groups"])
        add("label", "Imported on 9/6", f"Delete this Google label ({n} contacts keep everything else)", [], True,
            group=groups["Imported on 9/6"])
    if "Cal-Com" in groups:
        n = sum(1 for c in contacts if c["side"] == "google" and "Cal-Com" in c["groups"])
        add("category", "Cal-Com", f"Give the Outlook copies of the {n} Google “Cal-Com” contacts the Outlook "
            "category “Cal-Com”", [], True, group=groups["Cal-Com"], category="Cal-Com")
    items = [i for i in items if not i.get("noop")]
    for n, i in enumerate(sorted(items, key=lambda i: (ORDER.index(i["kind"]), (i["title"] or "").lower())), 1):
        i["id"] = n
    plan = {"created": time.time(), "items": sorted(items, key=lambda i: i["id"]),
            "counts": {k: sum(1 for i in items if i["kind"] == k) for k in ORDER}}
    pid = store.x("INSERT INTO cleanup_plans(created, plan) VALUES(?,?)", (plan["created"], json.dumps(plan)))
    plan["plan_id"] = pid
    return plan


def latest_plan():
    row = (store.q("SELECT * FROM cleanup_plans ORDER BY id DESC LIMIT 1") or [None])[0]
    if not row:
        return None
    plan = json.loads(row["plan"])
    plan.update(plan_id=row["id"], applied=row["applied"], result=json.loads(row["result"] or "null"))
    return plan


# ---------- apply ----------

def start_apply(plan_id, selected, backup_runner=None):
    with _lock:
        if state["running"]:
            return False
        state.update(running=True, step="Starting", done=0, total=len(selected), log=[], plan_id=plan_id)
    threading.Thread(target=_apply, args=(plan_id, set(selected), backup_runner), daemon=True).start()
    return True


def _say(line):
    state["log"].append(line)
    log.info(line)


def _apply(plan_id, selected, backup_runner):
    region = store.load_settings().get("default_region", "US")
    ok = failed = skipped = 0
    try:
        if writer.mode() != "on":
            raise writer.Refused("Write mode must be “on” (Settings) to clean up real contacts.")
        row = store.q("SELECT * FROM cleanup_plans WHERE id=?", (plan_id,))[0]
        items = [i for i in json.loads(row["plan"])["items"] if i["id"] in selected]
        if backup_runner is not None:
            state["step"] = "Taking a backup first"
            backup_runner.start("before cleanup")
            time.sleep(1)
            while backup_runner.progress["running"]:
                time.sleep(1)
            if backup_runner.progress.get("error"):
                raise RuntimeError("The backup failed, so nothing was changed: " + backup_runner.progress["error"])
        names = {}
        for i in items:
            state["step"] = f"{i['kind']}: {i['title']}"
            try:
                result = {"tidy": _do_tidy, "combine": _do_combine, "shared": _do_combine, "copy": _do_copy,
                          "label": _do_label, "category": _do_category}[i["kind"]](i, region, names)
                if result:
                    _say(f"✓ {i['title']}: {result}")
                    ok += 1
                else:
                    skipped += 1
            except writer.Refused as e:
                _say(f"– {i['title']}: skipped ({e})")
                skipped += 1
            except Exception as e:
                log.exception("cleanup item failed")
                _say(f"✗ {i['title']}: failed ({e})"[:300])
                failed += 1
            state["done"] += 1
            time.sleep(PAUSE)
    except Exception as e:
        _say(f"Stopped: {e}")
        failed += 1
    finally:
        store.x("UPDATE cleanup_plans SET applied=?, result=? WHERE id=?",
                (time.time(), json.dumps({"ok": ok, "failed": failed, "skipped": skipped, "log": state["log"][-500:]}),
                 plan_id))
        state.update(running=False, step="")


def _fresh(m):
    raw, c = writer._fresh(m)
    return raw, c


def _do_tidy(item, region, _names):
    m = item["members"][0]
    raw, c = _fresh(m)
    rec, reasons, _ = tidy(c, region)
    if not reasons:
        return ""
    writer.update([m], rec, action="cleanup", note="cleanup: " + "; ".join(reasons))
    return "; ".join(reasons)


def _do_combine(item, region, _names):
    fresh = []
    for m in item["members"]:
        try:
            fresh.append((m, *_fresh(m)))
        except Exception:
            continue                                     # gone since the plan: combine the rest
    if not fresh:
        return ""
    rec = combine_recs([tidy(c, region)[0] for _, _, c in fresh], region)
    keepers, extra = {}, []
    for m, raw, c in sorted(fresh, key=lambda x: x[2].get("updated") or "", reverse=True):
        if m["side"] in keepers:
            extra.append(m)
        else:
            keepers[m["side"]] = m
    writer.update(list(keepers.values()), rec, action="cleanup", note="cleanup: combined copies")
    for m in extra:
        writer.delete([m], note="cleanup: merged into another copy")
    if len(keepers) == 2:
        ms_raw, _ = _fresh(keepers["microsoft"])
        g_raw, _ = _fresh(keepers["google"])
        sync._new_link(ms_raw, g_raw)
    return (f"combined {len(fresh)} copies" + (f", {len(extra)} sent to the recycle bin" if extra else "")
            + (", linked for sync" if len(keepers) == 2 else ""))


def _do_copy(item, region, _names):
    m = item["members"][0]
    raw, c = _fresh(m)
    rec, _, _ = tidy(c, region)
    other = sync.OTHER[m["side"]]
    if tidy(c, region)[1]:                               # tidy the original too, so both match
        writer.update([m], rec, action="cleanup", note="cleanup: tidy before copying")
        raw, c = _fresh(m)
    ids, _ = writer.create(rec, sides=(other,), action="cleanup", note=f"cleanup: copied from {LABEL[m['side']]}")
    made, _ = _fresh({"side": other, "id": ids[other]})
    pair = {m["side"]: raw, other: made}
    sync._new_link(pair["microsoft"], pair["google"])
    return f"copied to {LABEL[other]} and linked"


def _do_label(item, region, _names):
    writer.delete_label(item["group"], item["title"])
    return "label deleted (its contacts are unchanged)"


def _do_category(item, region, _names):
    members = google.group_members(item["group"])
    n = 0
    for rn in members:
        lk = (store.q("SELECT ms_id FROM links WHERE g_id=?", (rn,)) or [None])[0]
        if not lk or not lk["ms_id"]:
            continue
        raw, c = _fresh({"side": "microsoft", "id": lk["ms_id"]})
        cats = list(raw.get("categories") or [])
        if item["category"] in cats:
            continue
        writer.patch_graph(lk["ms_id"], {"categories": cats + [item["category"]]}, c["name"],
                           note=f"cleanup: category {item['category']}")
        n += 1
        time.sleep(PAUSE / 2)
    return f"category added to {n} Outlook contacts"
