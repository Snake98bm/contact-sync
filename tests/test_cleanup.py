"""The one-time cleanup against fake accounts: plan, defaults, and applying it.
Run: python tests/test_cleanup.py"""
import copy
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))
os.environ["DATA_DIR"] = tempfile.mkdtemp()
import addr  # noqa: E402
import cleanup  # noqa: E402
import google  # noqa: E402
import msgraph  # noqa: E402
import net  # noqa: E402
import store  # noqa: E402
import sync  # noqa: E402
import writer  # noqa: E402

store.init()
writer.init()
sync.init()
cleanup.init()
writer.listener = sync.after_write
cleanup.PAUSE = 0
LOOKUPS = []


def _fake_census(a, **kw):    # never the real service in tests
    LOOKUPS.append(a)
    return {"city": "Springfield", "region": "CA", "postal": "90000", "matched": "1 EXAMPLE AVE, SPRINGFIELD, CA, 90000"}


addr.census = _fake_census

MS, G, TRASH, GROUPS, DELETED_GROUPS = {}, {}, {}, {}, []
CLOCK = [0]


def tick():
    CLOCK[0] += 1
    return f"2026-03-01T00:{CLOCK[0] // 60:02d}:{CLOCK[0] % 60:02d}Z"


def ms_put(cid, body):
    MS[cid] = dict(copy.deepcopy(body), id=cid, parentFolderId="contacts", lastModifiedDateTime=tick())
    return copy.deepcopy(MS[cid])


def ms_get(cid):
    if cid in MS:
        return copy.deepcopy(MS[cid])
    raise net.HttpError(404, "{}", cid)


msgraph.fetch_all = lambda: ([copy.deepcopy(c) for c in MS.values()], [])
msgraph.get_contact = ms_get
msgraph.create_contact = lambda body: ms_put(f"AAM-new{CLOCK[0]}", body)
msgraph.update_contact = lambda cid, body: ms_put(cid, {**MS[cid], **body})
msgraph.delete_contact = lambda cid: TRASH.__setitem__(cid, MS.pop(cid))


def g_put(rn, body):
    G[rn] = dict(copy.deepcopy(body), resourceName=rn, etag=f"e{CLOCK[0]}",
                 metadata={"sources": [{"type": "CONTACT", "updateTime": tick()}]})
    return copy.deepcopy(G[rn])


def g_get(rn):
    if rn in G:
        return copy.deepcopy(G[rn])
    raise net.HttpError(404, "{}", rn)


def g_update(rn, body, fields):
    assert body["etag"] == G[rn]["etag"]
    cur = dict(G[rn])
    old_names = (cur.get("names") or [{}])[0]
    for f in fields.split(","):
        cur[f] = copy.deepcopy(body.get(f, []))
    if cur.get("names"):          # like Google: the old combined name stays unless a new one is sent
        n = cur["names"][0]
        if "unstructuredName" not in n and old_names.get("unstructuredName"):
            n["unstructuredName"] = old_names["unstructuredName"]
        n["displayName"] = n.get("unstructuredName") or " ".join(
            x for x in (n.get("givenName"), n.get("middleName"), n.get("familyName")) if x)
    return g_put(rn, cur)


google.fetch_all = lambda: ([copy.deepcopy(p) for p in G.values()],
                            [{"resourceName": rn, "name": n, "formattedName": n} for rn, n in GROUPS.items()])
google.get_person = g_get
google.create_person = lambda body: g_put(f"people/c-new{CLOCK[0]}", body)
google.update_person = g_update
google.delete_person = lambda rn: G.pop(rn)
google.group_members = lambda rn: [p["resourceName"] for p in G.values()
                                   if any((m.get("contactGroupMembership") or {}).get("contactGroupResourceName") == rn
                                          for m in p.get("memberships") or [])]


def _groups_of(p):
    return [(m.get("contactGroupMembership") or {}).get("contactGroupResourceName") for m in p.get("memberships") or []]


def _del_group(rn):
    assert net.check_allowed("DELETE", f"https://people.googleapis.com/v1/{rn}?deleteContacts=false") is None
    DELETED_GROUPS.append(rn)
    GROUPS.pop(rn)
    # like the real Google: a contact whose only group was this label goes with it, deleteContacts=false or not
    for pid, p in list(G.items()):
        gs = _groups_of(p)
        if rn in gs and not [g for g in gs if g != rn]:
            G.pop(pid)
        elif rn in gs:
            p["memberships"] = [m for m in p["memberships"]
                                if (m.get("contactGroupMembership") or {}).get("contactGroupResourceName") != rn]


def _add_my(rns):
    assert net.check_allowed("POST", "https://people.googleapis.com/v1/contactGroups/myContacts/members:modify") is None
    for rn in rns:
        if "contactGroups/myContacts" not in _groups_of(G[rn]):
            G[rn].setdefault("memberships", []).append(
                {"contactGroupMembership": {"contactGroupResourceName": "contactGroups/myContacts"}})


google.delete_group = _del_group
google.add_to_my_contacts = _add_my

BLOCK = "-----------------------------\nContact Imported:\nHome Street : 1 Example Ave\n"


def ms(cid, first, last, email=None, phone=None, notes="", **kw):
    return ms_put(cid, {"givenName": first, "surname": last, "displayName": f"{first} {last}".strip(),
                        "emailAddresses": [{"address": email, "name": email}] if email else [],
                        "mobilePhone": phone, "personalNotes": notes,
                        "homeAddress": {"street": "1 Example Ave", "city": "Springfield", "state": "CA"}, **kw})


def gg(rn, first, last, email=None, phones=(), notes="", shown=None, groups=()):
    names = {"givenName": first, "familyName": last}
    if shown:
        names.update(displayName=shown, unstructuredName=shown)
    return g_put(rn, {"names": [names], "emailAddresses": [{"value": email}] if email else [],
                      "phoneNumbers": [{"value": p, "type": "mobile"} for p in phones],
                      "biographies": [{"value": notes, "contentType": "TEXT_PLAIN"}] if notes else [],
                      "addresses": [{"type": "home", "streetAddress": "1 Example Ave", "city": "Springfield"}],
                      "memberships": [{"contactGroupMembership": {"contactGroupResourceName": g}} for g in groups]})


def mode(m):
    s = store.load_settings()      # always re-read: sync.start() changes the settings too
    s["write_mode"] = m
    store.save_settings(s)


def setup():
    GROUPS.update({"contactGroups/imp": "Imported on 9/6", "contactGroups/cal": "Cal-Com"})
    # a linked pair with every tidy problem
    ms("AAM-greg", "Greg", "Example", "Greg@Example.com", "+15595551234", "Gate code 12\n" + BLOCK)
    gg("people/c-greg", "Greg", "Example", "greg@example.com", ["+1 (559) 555-1234", "559-555-1234"],
       "Gate code 12\n" + BLOCK, shown="Example, Greg", groups=["contactGroups/cal", "contactGroups/imp"])
    # duplicates in Microsoft
    ms("AAM-j1", "Jackie", "Sample", "jackie@example.com", "(559) 555-0101", "note one")
    ms("AAM-j2", "Jackie", "Sample", None, "559-555-0101", "note two")
    gg("people/c-j", "Jackie", "Sample", "jackie@example.com", ["(559) 555-0101"])
    # same name, nothing shared, one in each
    ms("AAM-v", "Vikki", "", None, "281-7719")
    gg("people/c-v", "Vikki", "", None, ["281-7719"])
    # only in Microsoft
    ms("AAM-solo", "Solo", "Example", "solo@example.com", None)
    # only in Google, and only in the label that gets deleted (not in My Contacts)
    gg("people/c-lonely", "Lonely", "Example", "lonely@example.com", ["(559) 555-0500"], groups=["contactGroups/imp"])
    # a linked pair plus a second Outlook copy with nothing shared (must merge, not be copied as new)
    ms("AAM-l1", "Lisa", "Example", None, "(559) 555-0400")
    gg("people/c-l", "Lisa", "Example", None, ["(559) 555-0400"])
    ms("AAM-l2", "Lisa", "Example", None, "155935660177")
    # different names, same phone (a household line), already paired each
    ms("AAM-a", "Anna", "Example", "anna@example.com", "(559) 555-0300")
    gg("people/c-a", "Anna", "Example", "anna@example.com", ["(559) 555-0300"])
    ms("AAM-b", "Bob", "Example", "bob@example.com", "(559) 555-0300")
    gg("people/c-b", "Bob", "Example", "bob@example.com", ["(559) 555-0300"])


def test_cleanup():
    setup()
    mode("on")
    sync.start()
    plan = cleanup.build_plan()
    items = plan["items"]
    by = lambda kind: [i for i in items if i["kind"] == kind]
    tidy_titles = {(i["members"][0]["side"], i["title"]): i for i in by("tidy")}
    g_tidy = tidy_titles[("google", "Greg Example")]
    assert "remove “Contact Imported” block" in g_tidy["detail"] and "duplicate phone" in g_tidy["detail"]
    assert "Last, First" in g_tidy["detail"] and g_tidy["default"]
    assert "lowercase emails" in tidy_titles[("microsoft", "Greg Example")]["detail"]
    combos = {i["title"]: i for i in by("combine")}
    assert combos["Jackie Sample"]["default"] and len(combos["Jackie Sample"]["members"]) == 3
    assert combos["Vikki"]["default"]
    shared = by("shared")
    assert len(shared) == 1 and not shared[0]["default"] and "Anna" in shared[0]["title"]
    assert [i["title"] for i in by("copy")] == ["Lonely Example", "Solo Example"], [i["title"] for i in by("copy")]
    assert combos["Lisa Example"]["default"] and len(combos["Lisa Example"]["members"]) == 3
    assert by("label")[0]["default"] and by("category")[0]["default"]

    # test mode refuses to apply
    mode("test")
    cleanup._apply(plan["plan_id"], {i["id"] for i in items if i["default"]}, None)
    assert "Write mode must be" in cleanup.state["log"][-1] and "AAM-j2" in MS
    mode("on")

    # what the page sends: everything that starts ticked, Jackie combined under an edited name,
    # and the shared household number removed from Bob only
    jackie = combos["Jackie Sample"]
    assert {o["label"] for o in jackie["names"]} == {"Jackie Sample"}
    sh = shared[0]
    bob = next(k for k, p in enumerate(sh["people"]) if p["name"] == "Bob Example")
    form = {"plan_id": str(plan["plan_id"]), f"given_{jackie['id']}": "Jacqueline", f"family_{jackie['id']}": "Sample",
            f"shared_{sh['id']}": "strip"}
    multi = {"item": [str(i["id"]) for i in items if i["default"]], f"strip_{sh['id']}": [str(bob)]}
    # addresses: Greg's (same on both copies) is one item; its ZIP was looked up; Solo's is edited by hand
    adds = {i["title"]: i for i in by("address")}
    g_addr = adds["Greg Example"]
    assert len(g_addr["members"]) == 2 and g_addr["after"]["postal"] == "90000" and g_addr["default"]
    assert "looked up ZIP" in g_addr["detail"] and LOOKUPS
    solo = adds["Solo Example"]
    form.update({f"addr_{solo['id']}_street": "5 Other St", f"addr_{solo['id']}_city": "Shelbyville",
                 f"addr_{solo['id']}_region": "california", f"addr_{solo['id']}_postal": "90001"})
    selected = cleanup.choices_from_form(plan["plan_id"], lambda k: form.get(k, ""), lambda k: multi.get(k, []))
    assert selected[solo["id"]]["mode"] == "address" and selected[solo["id"]]["fields"]["city"] == "Shelbyville"
    assert selected[jackie["id"]] == {"mode": "combine", "name": {"given": "Jacqueline", "middle": "", "family": "Sample",
                                                                  "company": ""}}
    assert selected[sh["id"]] == {"mode": "strip", "people": [bob]}
    cleanup.state["log"] = []
    cleanup._apply(plan["plan_id"], selected, object())          # with a backup first: only the affected contacts
    log = "\n".join(cleanup.state["log"])
    assert "✗" not in log and "Saving a copy of the" in log, log
    import backup
    import json as _json
    info = _json.load(open(os.path.join(store.BACKUP_DIR, backup.listing()[0]["name"], "info.json")))
    assert info["reason"] == "before cleanup" and info["contacts"] > 0 and "report.json" not in backup.listing()[0]["files"], info
    g = G["people/c-greg"]
    assert g["biographies"][0]["value"] == "Gate code 12", g["biographies"]
    assert [p["value"] for p in g["phoneNumbers"]] == ["+1 (559) 555-1234"], g["phoneNumbers"]
    assert g["names"][0]["displayName"] == "Greg Example", g["names"]      # really shown as First Last
    assert MS["AAM-greg"]["personalNotes"] == "Gate code 12" and MS["AAM-greg"]["mobilePhone"] == "+1 (559) 555-1234"
    assert MS["AAM-greg"]["emailAddresses"][0]["address"] == "greg@example.com"
    assert MS["AAM-greg"]["homeAddress"]["postalCode"] == "90000" and g["addresses"][0]["postalCode"] == "90000"
    sa = MS["AAM-solo"]["homeAddress"]
    assert (sa["street"], sa["city"], sa["state"], sa["postalCode"]) == ("5 Other St", "Shelbyville", "CA", "90001"), sa
    # Jackie: one Outlook copy left, with both notes and the email; the other in the bin; linked
    jackies = [c for c in MS.values() if c.get("surname") == "Sample"]
    assert len(jackies) == 1 and "note one" in jackies[0]["personalNotes"] and "note two" in jackies[0]["personalNotes"]
    assert jackies[0]["givenName"] == "Jacqueline" and G["people/c-j"]["names"][0]["givenName"] == "Jacqueline"
    assert any(b["name"] == "Jackie Sample" for b in writer.bin_items())
    assert store.q("SELECT 1 FROM links WHERE ms_id=? AND g_id='people/c-j'", (jackies[0]["id"],))
    assert store.q("SELECT 1 FROM links WHERE ms_id='AAM-v' AND g_id='people/c-v'")
    # Solo copied to Google and linked; the household line left alone
    solo_g = [p for p in G.values() if (p.get("names") or [{}])[0].get("givenName") == "Solo"]
    assert len(solo_g) == 1 and store.q("SELECT 1 FROM links WHERE ms_id='AAM-solo'")
    assert "AAM-a" in MS and "AAM-b" in MS
    assert MS["AAM-a"]["mobilePhone"] == "+1 (559) 555-0300" and G["people/c-a"]["phoneNumbers"]   # Anna keeps it
    assert not MS["AAM-b"].get("mobilePhone") and not G["people/c-b"]["phoneNumbers"]            # removed from Bob
    assert MS["AAM-b"]["emailAddresses"][0]["address"] == "bob@example.com"                      # nothing else lost
    lisas = [c for c in MS.values() if c.get("givenName") == "Lisa"]
    assert len(lisas) == 1 and not any((p.get("names") or [{}])[0].get("givenName") == "Lisa" and p["resourceName"] != "people/c-l"
                                       for p in G.values())
    assert store.q("SELECT 1 FROM links WHERE g_id='people/c-l' AND ms_id=?", (lisas[0]["id"],))
    # label removed (contacts kept), Cal-Com copied to Outlook
    assert DELETED_GROUPS == ["contactGroups/imp"] and "people/c-greg" in G
    assert "people/c-lonely" in G, "a contact only in the deleted label was lost"
    assert "contactGroups/myContacts" in _groups_of(G["people/c-lonely"])
    assert MS["AAM-greg"].get("categories") == ["Cal-Com"]
    # a sync right after copies nothing back and forth
    r = store.q("SELECT * FROM sync_runs WHERE id=?", (sync.run("test"),))[0]
    assert (r["updated"], r["created"], r["deleted"], r["error"]) == (0, 0, 0, None), dict(r)
    # a rebuilt plan no longer offers the Cal-Com category (all linked Outlook copies have it), and
    # the build reports progress
    steps = []
    plan2 = cleanup.build_plan(progress=steps.append)
    assert not [i for i in plan2["items"] if i["kind"] == "category"] and any("addresses" in s for s in steps)
    # the plan shows as applied
    assert cleanup.latest_plan()["plan_id"] == plan2["plan_id"] and not cleanup.latest_plan()["applied"]
    assert store.q("SELECT applied FROM cleanup_plans WHERE id=?", (plan["plan_id"],))[0]["applied"]
    assert not writer.busy["cleanup"]


def test_repeated_number_keeps_best_label():
    c = {"side": "microsoft", "id": "x", "given": "Pat", "family": "Example", "emails": [], "addr": [], "notes": "",
         "phones": [{"value": "+15555550123", "type": "home"}, {"value": "(555) 555-0123", "type": "mobile"},
                    {"value": "(555) 555-0188", "type": "other"}, {"value": "555-555-0188", "type": "work"}]}
    rec, reasons, _ = cleanup.tidy(c)
    assert [(p["value"], p["type"]) for p in rec["phones"]] == [("+1 (555) 555-0123", "mobile"),
                                                                 ("+1 (555) 555-0188", "work")]
    assert "remove duplicate phones" in reasons
    both = cleanup.combine_recs([{"phones": [{"value": "(555) 555-0123", "type": "home"}]},
                                 {"phones": [{"value": "+15555550123", "type": "other"}]}])
    assert both["phones"] == [{"value": "(555) 555-0123", "type": "other"}]


def test_shared_number_marked_right():
    mode("on")
    ms("AAM-cara", "Cara", "Sample", "cara@example.com", "(559) 555-0777")
    ms("AAM-drew", "Drew", "Sample", "drew@example.com", "(559) 555-0777")
    plan = cleanup.build_plan()
    sh = [i for i in plan["items"] if i["kind"] == "shared" and "Cara" in i["title"]]
    assert len(sh) == 1, [i["title"] for i in plan["items"] if i["kind"] == "shared"]
    choice = cleanup.choices_from_form(plan["plan_id"], {f"shared_{sh[0]['id']}": "ok"}.get, lambda k: [])
    assert choice[sh[0]["id"]] == {"mode": "ok"}
    msg = cleanup._do_shared(dict(sh[0], choice=choice[sh[0]["id"]]), "US", {})
    assert "won't be asked again" in msg and "Home" in msg, msg
    assert MS["AAM-cara"]["homePhones"] and not MS["AAM-cara"].get("mobilePhone"), MS["AAM-cara"]
    again = [i for i in cleanup.build_plan()["items"] if i["kind"] == "shared" and "Cara" in i["title"]]
    assert not again, "asked again after it was marked right"


def test_nothing_found_means_no_row():
    mode("on")
    ms("AAM-nofix", "Nofix", "Example", "nofix@example.com", homeAddress={"street": "just a note", "city": "", "state": ""})
    real = addr.census
    addr.census = lambda a, **kw: None                          # nothing known
    try:
        rows = [i for i in cleanup.build_plan()["items"] if i["kind"] == "address" and i["title"] == "Nofix Example"]
    finally:
        addr.census = real
    assert not rows, rows


def test_parallel_pieces():
    import threading
    import time as t
    m = lambda s, i: {"side": s, "id": i}
    items = [{"id": 1, "members": [m("google", "a")]}, {"id": 2, "members": [m("google", "b")]},
             {"id": 3, "members": [m("microsoft", "x"), m("google", "a")]}, {"id": 4, "members": [m("google", "c")],
             "person": [m("microsoft", "x")]}]
    groups = sorted([[i["id"] for i in g] for g in cleanup._groups(items)])
    assert groups == [[1, 3, 4], [2]], groups                 # same contact: one worker, plan order
    # the writer locks one contact at a time: two people at once, the same person never
    events = []

    def hold(cid, tag):
        with writer._hold([m("google", cid)]):
            events.append(("in", tag))
            t.sleep(0.2)
            events.append(("out", tag))
    th = [threading.Thread(target=hold, args=a) for a in (("p1", "A"), ("p1", "B"), ("p2", "C"))]
    for x in th:
        x.start()
    for x in th:
        x.join()
    order = [e for e in events if e[1] in "AB"]
    assert order in ([("in", "A"), ("out", "A"), ("in", "B"), ("out", "B")],
                     [("in", "B"), ("out", "B"), ("in", "A"), ("out", "A")]), events
    assert events.index(("in", "C")) < events.index(("out", events[0][1])), events    # p2 didn't wait
    # pacing: a 429 widens the gap for that service, successes narrow it again
    url = "https://people.googleapis.com/v1/x"
    net._paced(url, False)
    assert net._pace["people.googleapis.com"][0] >= 0.5
    for _ in range(200):
        net._paced(url, True)
    assert net._pace["people.googleapis.com"][0] == net.MIN_GAP["people.googleapis.com"]


def test_address_typed_two_ways_is_one_row():
    mode("on")
    ms("AAM-twin", "Twin", "Example", "twin@example.com",
       homeAddress={"street": "7 Sample", "city": "Springfield", "state": "CA"},
       otherAddress={"street": "7 Sample Rd.", "city": "Springfield", "state": "CA"})
    real = addr.census
    addr.census = lambda a, **kw: {"city": "Springfield", "region": "CA", "postal": "90007", "street": "7 Sample Rd",
                                   "matched": "7 SAMPLE RD, SPRINGFIELD, CA, 90007"}
    try:
        rows = [i for i in cleanup.build_plan()["items"] if i["kind"] == "address" and i["title"] == "Twin Example"]
    finally:
        addr.census = real
    assert len(rows) == 1 and len(rows[0]["street_keys"]) == 2 and "also written as" in rows[0]["detail"], rows
    assert rows[0]["addr_type"] == "home" and rows[0]["after"]["postal"] == "90007"
    cleanup._do_address(rows[0], "US", {})
    assert MS["AAM-twin"]["homeAddress"]["street"] == "7 Sample Rd" and MS["AAM-twin"]["homeAddress"]["postalCode"] == "90007"
    assert not (MS["AAM-twin"].get("otherAddress") or {}).get("street"), MS["AAM-twin"]       # one copy kept
    # the owner can remove an address instead of fixing it
    form = {f"addr_{rows[0]['id']}_remove": "1"}
    plan_id = cleanup.latest_plan()["plan_id"]
    choice = cleanup.choices_from_form(plan_id, form.get, lambda k: [str(rows[0]["id"])] if k == "item" else [])
    assert choice[rows[0]["id"]] == {"mode": "remove"}, choice
    msg = cleanup._do_address(dict(rows[0], choice=choice[rows[0]["id"]], street_keys=["7 Sample Rd"]), "US", {})
    assert "removed" in msg and not (MS["AAM-twin"].get("homeAddress") or {}).get("street"), MS["AAM-twin"]
    # ...or delete the whole contact (to the recycle bin)
    form = {f"addr_{rows[0]['id']}_do": "delete_contact"}
    choice = cleanup.choices_from_form(plan_id, form.get, lambda k: [str(rows[0]["id"])] if k == "item" else [])
    assert choice[rows[0]["id"]] == {"mode": "delete_contact"}, choice
    msg = cleanup._do_address(dict(rows[0], choice=choice[rows[0]["id"]]), "US", {})
    assert "AAM-twin" not in MS and "recycle bin" in msg, msg
    assert any(b["name"] == "Twin Example" for b in writer.bin_items())
    assert cleanup._do_address(dict(rows[0], choice=choice[rows[0]["id"]]), "US", {}) == ""   # already gone: no error


def test_tidy_row_can_delete_the_contact():
    mode("on")
    ms("AAM-tess", "Tess", "Example", "Tess@Example.com", "(559) 555-0700")
    gg("people/c-tess", "Tess", "Example", "tess@example.com", ["(559) 555-0700"], shown="Example, Tess")
    rows = [i for i in cleanup.build_plan()["items"] if i["kind"] == "tidy" and i["title"] == "Tess Example"]
    assert rows, "no tidy row for Tess"
    row = rows[0]
    assert {(m["side"], m["id"]) for m in row["person"]} == {("microsoft", "AAM-tess"), ("google", "people/c-tess")}
    plan_id = cleanup.latest_plan()["plan_id"]
    # left on "Fix it": a plain tidy
    choice = cleanup.choices_from_form(plan_id, {}.get, lambda k: [str(row["id"])] if k == "item" else [])
    assert choice[row["id"]] is True, choice
    # "Delete this contact": every copy, both accounts, to the recycle bin
    form = {f"tidy_{row['id']}_do": "delete_contact"}
    choice = cleanup.choices_from_form(plan_id, form.get, lambda k: [str(row["id"])] if k == "item" else [])
    assert choice[row["id"]] == {"mode": "delete_contact"}, choice
    msg = cleanup._do_tidy(dict(row, choice=choice[row["id"]]), "US", {})
    assert "AAM-tess" not in MS and "people/c-tess" not in G and "recycle bin" in msg, msg
    assert any(b["name"] == "Tess Example" for b in writer.bin_items())
    assert cleanup._do_tidy(dict(row, choice=choice[row["id"]]), "US", {}) == ""     # already gone: no error
    # not ticked: nothing happens, whatever the radio says
    assert row["id"] not in cleanup.choices_from_form(plan_id, form.get, lambda k: [])


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
