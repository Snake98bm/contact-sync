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
import re
import logging
import threading
import time

import addr
import clean
import google
import model
import msgraph
import store
import sync
import writer

log = logging.getLogger("cleanup")
LABEL = {"microsoft": "Microsoft 365", "google": "Google"}
ORDER = ("tidy", "address", "combine", "shared", "copy", "label", "category")
PAUSE = 0.6               # between writes: stays well under Google's per-minute write limit

SCHEMA = """
CREATE TABLE IF NOT EXISTS cleanup_plans(id INTEGER PRIMARY KEY, created REAL, plan TEXT, applied REAL, result TEXT);
"""

state = {"running": False, "step": "", "done": 0, "total": 0, "log": [], "plan_id": None,
         "building": False, "build_step": "", "build_error": ""}
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
    # the same address listed twice (left by an older sync tool), even under two types (Work + Other):
    # keep the most complete one, and Work/Home over Other
    kept, repeats = [], 0
    for a in rec.get("addr") or []:
        same = next((k for k in kept if _street_key(k) and _street_key(k) == _street_key(a)
                     and _no_clash(k, a)), None)
        if same is None:
            kept.append(dict(a))
            continue
        repeats += 1
        typ = same.get("type") if (same.get("type") or "other") != "other" else (a.get("type") or same.get("type"))
        if len(model.addr_line(a)) > len(model.addr_line(same)):
            same.update(a)
        same["type"] = typ
    if repeats:
        rec["addr"] = kept
        reasons.append(f"remove repeated address{'es' if repeats > 1 else ''}")
    comp = (rec.get("company") or "").strip()
    if comp and re.fullmatch(r"[\d\s().+\-]{7,}", comp) and len(re.sub(r"\D", "", comp)) >= 7:
        # a phone number typed into the company box: move it to the phones
        rec["company"] = ""
        rec["phones"] = list(rec.get("phones") or []) + [{"value": comp, "type": "mobile"}]
        reasons.append("move phone number out of the company field")
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


def name_options(contacts):
    """The different names the copies have, for the owner to pick from when combining."""
    out, seen = [], set()
    for c in sorted(contacts, key=lambda c: -len(c.get("name") or "")):
        given, middle, family = (_unescape(c.get(k)) for k in ("given", "middle", "family"))
        company = _unescape(c.get("company"))
        label = " ".join(x for x in (given, middle, family) if x) or company
        if label and label.lower() not in seen:
            seen.add(label.lower())
            out.append({"label": label, "given": given, "middle": middle, "family": family,
                        "company": company if not (given or family) else ""})
    return out


def apply_name(rec, choice):
    """Put the chosen name on a combined record. choice: {"given","middle","family"} or a company-only
    name {"company"} (then the person-name fields are cleared)."""
    if not choice:
        return rec
    rec = dict(rec)
    c = {k: _unescape(choice.get(k)) for k in ("given", "middle", "family", "company")}
    if c["given"] or c["family"] or c["middle"]:
        rec.update(given=c["given"], middle=c["middle"], family=c["family"])
        if c["company"]:
            rec["company"] = c["company"]
    elif c["company"]:
        rec.update(given="", middle="", family="", company=c["company"])
    return rec


# ---------- plan ----------

def build_plan(progress=None):
    say = progress or (lambda text: None)
    say("Reading both accounts")
    data, _, names = sync._fetch()
    region = store.load_settings().get("default_region", "US")
    contacts = [sync.common(s, raw, names) for s in sync.SIDES for raw in data[s].values()]
    rep = clean.analyse(contacts, region, sync.link_pairs())
    linked = {("microsoft", lk["ms_id"]) for lk in sync.links()} | {("google", lk["g_id"]) for lk in sync.links()}
    items = []

    by_id = {(c["side"], c["id"]): c for c in contacts}

    def add(kind, title, detail, members, default, **extra):
        item = {"id": len(items) + 1, "kind": kind, "title": title, "detail": detail, "default": default,
                "members": [{"side": c["side"], "id": c["id"], "name": c["name"]} for c in members], **extra}
        if kind in ("combine", "shared"):
            item["names"] = name_options([by_id[(c["side"], c["id"])] for c in members
                                          if (c["side"], c["id"]) in by_id])
        items.append(item)

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
    person_of = {(c["side"], c["id"]): p for p in rep["people"] for c in p["members"]}
    for s in rep["shared_info"]:
        mem = s["contacts"]
        if any((c["side"], c["id"]) in in_combine for c in mem):
            continue
        people, seen_keys = [], set()
        for c in mem:                                    # each person with all their copies (both accounts)
            p = person_of[(c["side"], c["id"])]
            if p["key"] not in seen_keys:
                seen_keys.add(p["key"])
                people.append({"name": p["name"] or "(no name)",
                               "members": [{"side": x["side"], "id": x["id"], "name": x["name"]} for x in p["members"]]})
        shown = clean.pretty_phone(s["value"]) if s["kind"] == "phone" else s["value"]
        all_members = [x for p in people for x in p["members"]]
        add("shared", " / ".join(p["name"] for p in people),
            f"Same {s['kind']} {shown} on different names: keep them separate, remove it from some of them, "
            "or combine them into one contact.",
            [dict(x, id=x["id"]) for x in all_members], False, kind_of=s["kind"], value=s["value"], shown=shown,
            people=people)
        in_combine |= {(x["side"], x["id"]) for x in all_members}

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

    # addresses: one item per person per address (both copies, when they hold the same address)
    lookup = bool(store.load_settings().get("address_lookup", True))
    n_people = len(rep["people"])
    zip_city = addr.zip_cities([a for c in contacts for a in c.get("addr") or []])
    for n_done, p in enumerate(rep["people"], 1):
        if n_done % 10 == 1:
            say(f"Checking addresses ({n_done} of {n_people} people)")
        seen_addr = {}
        for c in p["members"]:
            for a in c.get("addr") or []:
                k = _street_key(a) or f"(no street) {a.get('type')}"   # one row per place, whatever its type
                g = seen_addr.setdefault(k, {"addr": a, "members": []})
                if (g["addr"].get("type") or "other") == "other" and (a.get("type") or "other") != "other":
                    g["addr"] = a                                    # show Work/Home over Other
                if all(m is not c for m in g["members"]):      # a contact listing it twice counts once
                    g["members"].append(c)
        rows = {}          # fixed address -> one row, even when the copies typed it differently
        for skey, g in seen_addr.items():
            a = g["addr"]
            after, how, confident = addr.fix(a, lookup=lookup, zip_city=zip_city)
            changed = any((after.get(x) or "") != (a.get(x) or "") for x in ("street", "city", "region", "postal", "country"))
            missing = [x for x in ("street", "city", "region", "postal") if not (after.get(x) or "").strip()]
            if not changed and not missing:
                continue
            detail = "; ".join(how) if how else ""
            if missing:
                detail += ("; " if detail else "") + "still missing " + ", ".join(
                    {"street": "street", "city": "city", "region": "state", "postal": "ZIP"}[x] for x in missing)
            fixed = (_street_key(after), (after.get("city") or "").strip().lower(), (after.get("postal") or "")[:5])
            row = rows.get(fixed) if fixed[0] and not missing else None
            if row is None:
                rows[fixed if fixed[0] and not missing else ("", skey)] = {
                    "a": a, "after": after, "detail": detail, "members": list(g["members"]),
                    "ok": bool(changed and confident), "keys": [skey], "also": []}
                continue
            row["keys"].append(skey)
            row["also"].append(model.addr_line(a))
            row["ok"] = row["ok"] and bool(changed and confident)
            row["members"] += [m for m in g["members"] if all(m is not x for x in row["members"])]
            if (row["a"].get("type") or "other") == "other" and (a.get("type") or "other") != "other":
                row["a"] = a
        for row in rows.values():
            a, detail = row["a"], row["detail"]
            if row["also"]:
                detail += ("; " if detail else "") + "also written as: " + " | ".join(row["also"])
            add("address", p["name"] or "(no name)", detail, row["members"], row["ok"],
                addr_type=a.get("type"), street_key=row["keys"][0], street_keys=row["keys"],
                before=a, after=row["after"])

    # labels
    groups = {name: rn for rn, name in names.items()}
    if "Imported on 9/6" in groups:
        n = sum(1 for c in contacts if "Imported on 9/6" in c["groups"])
        add("label", "Imported on 9/6", f"Delete this Google label ({n} contacts keep everything else)", [], True,
            group=groups["Imported on 9/6"])
    if "Cal-Com" in groups:
        # only the linked Outlook copies that don't have the category yet
        ms_of = {g: m for m, g in sync.link_pairs()}
        todo = [c for c in contacts if c["side"] == "google" and "Cal-Com" in c["groups"] and ms_of.get(c["id"])
                and "Cal-Com" not in (data["microsoft"].get(ms_of[c["id"]], {}).get("categories") or [])]
        if todo:
            add("category", "Cal-Com", f"Give {len(todo)} Outlook copies of Google “Cal-Com” contacts the Outlook "
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

def choices_from_form(plan_id, f, form_all):
    """Read the Cleanup page's form: ticked items, each shared group's choice, and the name boxes."""
    row = (store.q("SELECT plan FROM cleanup_plans WHERE id=?", (plan_id,)) or [None])[0]
    if not row:
        return {}
    items = {i["id"]: i for i in json.loads(row["plan"])["items"]}
    ticked = {int(x) for x in form_all("item") if str(x).isdigit()}
    out = {}
    for n, i in items.items():
        name = {k: (f(f"{k}_{n}") or "").strip() for k in ("given", "middle", "family", "company")}
        name = name if any(name.values()) else None
        if i["kind"] == "shared":
            mode = f(f"shared_{n}")
            if mode == "combine":
                out[n] = {"mode": "combine", "name": name}
            elif mode == "strip":
                people = sorted({int(x) for x in form_all(f"strip_{n}") if str(x).isdigit()
                                 and int(x) < len(i.get("people") or [])})
                if people:
                    out[n] = {"mode": "strip", "people": people}
        elif n in ticked:
            if i["kind"] == "combine":
                out[n] = {"mode": "combine", "name": name}
            elif i["kind"] == "address":
                fields = {k: (f(f"addr_{n}_{k}") or "").replace("\r\n", "\n").strip()
                          for k in ("street", "city", "region", "postal", "country")}
                out[n] = {"mode": "address", "fields": fields} if any(fields.values()) else True
            else:
                out[n] = True
    return out


def _choices(selected):
    """{item id: choice}. A plain list of ids means "do them"; a shared item takes
    {"mode": "combine"} or {"mode": "strip", "people": [index, ...]}."""
    return dict(selected) if isinstance(selected, dict) else {x: True for x in selected}


def start_apply(plan_id, selected, backup_runner=None):
    with _lock:
        if state["running"]:
            return False
        state.update(running=True, step="Starting", done=0, total=len(selected), log=[], plan_id=plan_id)
    threading.Thread(target=_apply, args=(plan_id, _choices(selected), backup_runner), daemon=True).start()
    return True


def _say(line):
    state["log"].append(line)
    log.info(line)


def _apply(plan_id, selected, backup_runner):
    selected = _choices(selected)
    region = store.load_settings().get("default_region", "US")
    ok = failed = skipped = 0
    writer.busy["cleanup"] = True                    # sync runs skip while this is set
    try:
        if writer.mode() != "on":
            raise writer.Refused("Write mode must be “on” (Settings) to clean up real contacts.")
        waited = 0
        while writer.busy.get("sync") and waited < 900:   # a sync run already going: let it finish first
            state["step"] = "Waiting for a sync run to finish"
            time.sleep(2)
            waited += 2
        if writer.busy.get("sync"):
            raise RuntimeError("A sync run is still going after 15 minutes; nothing was changed. Try again later.")
        row = store.q("SELECT * FROM cleanup_plans WHERE id=?", (plan_id,))[0]
        items = [dict(i, choice=selected[i["id"]]) for i in json.loads(row["plan"])["items"] if i["id"] in selected]
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
                result = {"tidy": _do_tidy, "address": _do_address, "combine": _do_combine, "shared": _do_shared, "copy": _do_copy,
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
        writer.busy["cleanup"] = False


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
    choice = item.get("choice") if isinstance(item.get("choice"), dict) else {}
    rec = apply_name(rec, choice.get("name"))                 # the name the owner picked or typed
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


def _street_key(a):
    """First street line, ignoring capitals, punctuation and spelling ('Drive' = 'Dr', 'North' = 'N')."""
    first = (a.get("street") or "").replace("\r", "").split("\n")[0]
    return " ".join(addr._street_words(first))


def _no_clash(a, b):
    """Two addresses with the same street are one place unless a filled-in city or ZIP disagrees."""
    ca, cb = (a.get("city") or "").strip().lower(), (b.get("city") or "").strip().lower()
    za, zb = (a.get("postal") or "").strip()[:5], (b.get("postal") or "").strip()[:5]
    return not (ca and cb and ca != cb) and not (za and zb and za != zb)


def _do_address(item, region, _names):
    """Write the (possibly edited) address to every copy that holds it."""
    choice = item.get("choice") if isinstance(item.get("choice"), dict) else {}
    fields = choice.get("fields") or item["after"]
    new = {k: (fields.get(k) or "").strip() for k in ("street", "city", "region", "postal", "country")}
    if new["region"]:
        new["region"] = addr.state_code(new["region"])
    done = []
    for m in item["members"]:
        try:
            raw, c = _fresh(m)
        except Exception:
            continue
        rec = model.editable(c)
        same_type = [a for a in rec["addr"] if a.get("type") == item["addr_type"]]
        # every way this place was typed (older plans stored one plainer key)
        want = {_street_key({"street": k}) for k in item.get("street_keys") or [item["street_key"]]}
        targets = [a for a in rec["addr"] if _street_key(a) in want]
        if not targets and len(same_type) == 1:
            targets = same_type
        if not targets:
            continue
        keep, extra = targets[0], targets[1:]          # once fixed they'd be the same address: keep one
        if not extra and all((keep.get(k) or "").strip() == new[k] for k in new):
            continue
        keep.update(new)
        rec["addr"] = [a for a in rec["addr"] if all(a is not x for x in extra)]
        writer.update([m], rec, action="cleanup", note="cleanup: address fixed")
        done.append(LABEL[m["side"]])
    return f"address updated in {' and '.join(done)}" if done else ""


def _do_shared(item, region, names):
    """Different names on one phone/email: combine them, or remove it from the chosen people."""
    choice = item.get("choice") if isinstance(item.get("choice"), dict) else {}
    if choice.get("mode") == "combine":
        return _do_combine(item, region, names)
    if choice.get("mode") != "strip" or not choice.get("people"):
        return ""
    done = []
    for idx in choice["people"]:
        person = item["people"][idx]
        for m in person["members"]:
            try:
                raw, c = _fresh(m)
            except Exception:
                continue
            rec, _, _ = tidy(c, region)
            if item["kind_of"] == "phone":
                keep = [p for p in rec["phones"] if clean.norm_phone(p["value"], region)[0] != item["value"]]
                if len(keep) == len(rec["phones"]):
                    continue
                rec["phones"] = keep
            else:
                keep = [e for e in rec["emails"] if e.strip().lower() != item["value"]]
                if len(keep) == len(rec["emails"]):
                    continue
                rec["emails"] = keep
            writer.update([m], rec, action="cleanup", note=f"cleanup: removed shared {item['kind_of']} {item['shown']}")
        done.append(person["name"])
    return f"removed {item['shown']} from {', '.join(done)}" if done else ""


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


def start_plan():
    """Build the plan in the background (address lookups can take a minute or two)."""
    with _lock:
        if state["building"] or state["running"]:
            return False
        state.update(building=True, build_step="Starting", build_error="")

    def work():
        try:
            build_plan(progress=lambda text: state.update(build_step=text))
        except Exception as e:
            log.exception("building the cleanup plan failed")
            state["build_error"] = str(e)[:300]
        finally:
            state.update(building=False, build_step="")
    threading.Thread(target=work, daemon=True).start()
    return True
