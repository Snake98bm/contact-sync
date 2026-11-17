"""The one-time cleanup against fake accounts: plan, defaults, and applying it.
Run: python tests/test_cleanup.py"""
import copy
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))
os.environ["DATA_DIR"] = tempfile.mkdtemp()
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
    for f in fields.split(","):
        cur[f] = copy.deepcopy(body.get(f, []))
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


def _del_group(rn):
    assert net.check_allowed("DELETE", f"https://people.googleapis.com/v1/{rn}?deleteContacts=false") is None
    DELETED_GROUPS.append(rn)
    GROUPS.pop(rn)


google.delete_group = _del_group

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
    assert [i["title"] for i in by("copy")] == ["Solo Example"], [i["title"] for i in by("copy")]
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
    selected = cleanup.choices_from_form(plan["plan_id"], lambda k: form.get(k, ""), lambda k: multi.get(k, []))
    assert selected[jackie["id"]] == {"mode": "combine", "name": {"given": "Jacqueline", "middle": "", "family": "Sample",
                                                                  "company": ""}}
    assert selected[sh["id"]] == {"mode": "strip", "people": [bob]}
    cleanup.state["log"] = []
    cleanup._apply(plan["plan_id"], selected, None)
    log = "\n".join(cleanup.state["log"])
    assert "✗" not in log, log
    g = G["people/c-greg"]
    assert g["biographies"][0]["value"] == "Gate code 12", g["biographies"]
    assert [p["value"] for p in g["phoneNumbers"]] == ["(559) 555-1234"], g["phoneNumbers"]
    assert "unstructuredName" not in g["names"][0]
    assert MS["AAM-greg"]["personalNotes"] == "Gate code 12" and MS["AAM-greg"]["mobilePhone"] == "(559) 555-1234"
    assert MS["AAM-greg"]["emailAddresses"][0]["address"] == "greg@example.com"
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
    assert MS["AAM-a"]["mobilePhone"] == "(559) 555-0300" and G["people/c-a"]["phoneNumbers"]   # Anna keeps it
    assert not MS["AAM-b"].get("mobilePhone") and not G["people/c-b"]["phoneNumbers"]            # removed from Bob
    assert MS["AAM-b"]["emailAddresses"][0]["address"] == "bob@example.com"                      # nothing else lost
    lisas = [c for c in MS.values() if c.get("givenName") == "Lisa"]
    assert len(lisas) == 1 and not any((p.get("names") or [{}])[0].get("givenName") == "Lisa" and p["resourceName"] != "people/c-l"
                                       for p in G.values())
    assert store.q("SELECT 1 FROM links WHERE g_id='people/c-l' AND ms_id=?", (lisas[0]["id"],))
    # label removed (contacts kept), Cal-Com copied to Outlook
    assert DELETED_GROUPS == ["contactGroups/imp"] and "people/c-greg" in G
    assert MS["AAM-greg"].get("categories") == ["Cal-Com"]
    # a sync right after copies nothing back and forth
    r = store.q("SELECT * FROM sync_runs WHERE id=?", (sync.run("test"),))[0]
    assert (r["updated"], r["created"], r["deleted"], r["error"]) == (0, 0, 0, None), dict(r)
    # the plan shows as applied
    assert cleanup.latest_plan()["applied"]


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
