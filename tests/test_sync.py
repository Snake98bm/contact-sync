"""Sync engine against fake accounts. Run: python tests/test_sync.py"""
import copy
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))
os.environ["DATA_DIR"] = tempfile.mkdtemp()
import google  # noqa: E402
import msgraph  # noqa: E402
import net  # noqa: E402
import store  # noqa: E402
import sync  # noqa: E402
import writer  # noqa: E402

store.init()
writer.init()
sync.init()
writer.listener = sync.after_write          # as in main.py

MS, G, TRASH = {}, {}, {}          # TRASH: Outlook's Deleted Items
HIDE = set()                       # ids Google's list doesn't show yet (lag)
CLOCK = [0]


def tick():
    CLOCK[0] += 1
    return f"2026-02-01T00:{CLOCK[0] // 60:02d}:{CLOCK[0] % 60:02d}Z"


def _notfound(url):
    return net.HttpError(404, '{"error":"not found"}', url)


# ---- fake Microsoft ----
def ms_put(cid, body):
    MS[cid] = dict(copy.deepcopy(body), id=cid, parentFolderId="contacts", lastModifiedDateTime=tick())
    return copy.deepcopy(MS[cid])


def ms_get(cid):
    if cid in MS:
        return copy.deepcopy(MS[cid])
    if cid in TRASH:
        return copy.deepcopy(TRASH[cid])
    raise _notfound(cid)


msgraph.fetch_all = lambda: ([copy.deepcopy(c) for c in MS.values()], [])
msgraph.get_contact = ms_get
msgraph.create_contact = lambda body: ms_put(f"AAM{len(MS) + len(TRASH) + 1}x{CLOCK[0]}", body)
msgraph.update_contact = lambda cid, body: ms_put(cid, {**MS[cid], **body})


def ms_delete(cid):
    TRASH[cid] = dict(MS.pop(cid), parentFolderId="deleteditems")


msgraph.delete_contact = ms_delete


# ---- fake Google ----
def g_put(rn, body):
    G[rn] = dict(copy.deepcopy(body), resourceName=rn, etag=f"e{CLOCK[0]}",
                 metadata={"sources": [{"type": "CONTACT", "updateTime": tick()}]})
    return copy.deepcopy(G[rn])


def g_get(rn):
    if rn in G:
        return copy.deepcopy(G[rn])
    raise _notfound(rn)


def g_update(rn, body, fields):
    assert body["etag"] == G[rn]["etag"]
    cur = dict(G[rn])
    for f in fields.split(","):
        cur[f] = copy.deepcopy(body.get(f, []))
    return g_put(rn, cur)


google.fetch_all = lambda: ([copy.deepcopy(p) for rn, p in G.items() if rn not in HIDE], [])
google.get_person = g_get
google.create_person = lambda body: g_put(f"people/c{len(G) + 100}x{CLOCK[0]}", body)
google.update_person = g_update
google.add_to_my_contacts = lambda rns: None          # fake: never the real Google
google.delete_person = lambda rn: G.pop(rn)


# ---- helpers ----
def person(name, email, phone, notes=""):
    first, last = name.split(" ", 1)
    ms = ms_put(f"AAM-{email}", {"givenName": first, "surname": last, "displayName": name,
                                 "emailAddresses": [{"address": email, "name": email}], "mobilePhone": phone,
                                 "personalNotes": notes})
    g = g_put(f"people/c-{email}", {"names": [{"givenName": first, "familyName": last}],
                                    "emailAddresses": [{"value": email}],
                                    "phoneNumbers": [{"value": phone, "type": "mobile"}],
                                    "biographies": [{"value": notes, "contentType": "TEXT_PLAIN"}] if notes else []})
    return ms["id"], g["resourceName"]


def edit_ms(cid, **kw):
    ms_put(cid, {**MS[cid], **kw})


def edit_g(rn, **kw):
    g_put(rn, {**G[rn], **kw})


def notes_g(rn):
    return (G[rn].get("biographies") or [{}])[0].get("value", "")


def set(**kw):
    s = store.load_settings()
    s.update(kw)
    store.save_settings(s)


def last_run():
    return store.q("SELECT * FROM sync_runs ORDER BY id DESC LIMIT 1")[0]


def run(**kw):
    sync.run("test", **kw)
    r = last_run()
    assert not r["error"], r["error"]
    return r


# ---- the story ----
def test_sync():
    set(write_mode="on")
    t_ms, t_g = person("Contact-Sync Test", "test@example.com", "(555) 555-0100", "first")
    j_ms, j_g = person("Jane Example", "jane@example.com", "(555) 555-0101")
    ms_put("AAM-solo", {"givenName": "Solo", "surname": "Example", "displayName": "Solo Example",
                        "emailAddresses": [{"address": "solo@example.com"}]})
    assert sync.start() == 2
    r = run()
    assert (r["updated"], r["created"], r["deleted"]) == (0, 0, 0), dict(r)
    assert not any(c.get("givenName") == "Solo" for c in G.values()), "pre-existing one-sided contact was copied"

    # changed in Outlook -> Google
    edit_ms(t_ms, personalNotes="changed in outlook")
    r = run()
    assert r["updated"] == 1 and notes_g(t_g) == "changed in outlook"
    assert run()["updated"] == 0, "no ping-pong"

    # changed in Google -> Outlook
    edit_g(j_g, phoneNumbers=[{"value": "(555) 555-0199", "type": "mobile"}])
    run()
    assert MS[j_ms]["mobilePhone"] == "(555) 555-0199"

    # different fields changed on each side: both kept
    edit_ms(j_ms, jobTitle="Owner")
    edit_g(j_g, biographies=[{"value": "from google", "contentType": "TEXT_PLAIN"}])
    run()
    assert MS[j_ms]["jobTitle"] == "Owner" and MS[j_ms]["personalNotes"] == "from google"
    assert G[j_g]["organizations"][0]["title"] == "Owner" and notes_g(j_g) == "from google"

    # same field on both: newer wins, older value in the conflict log
    edit_g(j_g, biographies=[{"value": "older google", "contentType": "TEXT_PLAIN"}])
    edit_ms(j_ms, personalNotes="newer outlook")
    r = run()
    assert r["conflicts"] == 1 and notes_g(j_g) == "newer outlook" and MS[j_ms]["personalNotes"] == "newer outlook"
    cf = sync.recent_conflicts()[0]
    assert cf["kept_side"] == "microsoft" and json.loads(cf["lost_value"]) == "older google"

    # new in Google -> copied to Outlook and linked
    g_put("people/c-new", {"names": [{"givenName": "New", "familyName": "Person"}],
                           "emailAddresses": [{"value": "new@example.com"}]})
    r = run()
    assert r["created"] == 1 and any(c.get("surname") == "Person" for c in MS.values())
    assert run()["created"] == 0

    # new in Outlook that matches the old one-sided Google... (here: a new Google + new Outlook pair) -> linked, not copied
    g_put("people/c-pair", {"names": [{"givenName": "Pat", "familyName": "Pair"}], "emailAddresses": [{"value": "pat@example.com"}]})
    ms_put("AAM-pair", {"givenName": "Pat", "surname": "Pair", "displayName": "Pat Pair",
                        "emailAddresses": [{"address": "pat@example.com"}]})
    r = run()
    assert r["linked_new"] == 1 and r["created"] == 0, dict(r)
    assert sum(1 for c in MS.values() if c.get("surname") == "Pair") == 1

    # Google's list lags: a linked contact missing from the list but still there is NOT deleted
    HIDE.add(t_g)
    r = run()
    assert r["deleted"] == 0 and t_ms in MS
    HIDE.clear()

    # deleted in Outlook (goes to Deleted Items) -> deleted in Google, both copies in the bin
    ms_delete(j_ms)
    r = run()
    assert r["deleted"] == 1 and j_g not in G
    item = writer.bin_items()[0]
    assert json.loads(item["ms_raw"]) and json.loads(item["g_raw"]) and item["name"]

    # restoring it links the two new copies again
    ids, _ = writer.restore_bin(item["id"])
    assert store.q("SELECT 1 FROM links WHERE ms_id=? AND g_id=?", (ids["microsoft"], ids["google"]))
    assert run()["updated"] == 0

    # a page edit becomes the new base: the next run copies nothing
    lk = store.q("SELECT * FROM links WHERE ms_id=?", (t_ms,))[0]
    rec = sync.rec_of("microsoft", MS[t_ms])
    writer.listener = sync.after_write
    try:
        writer.update([{"side": "microsoft", "id": t_ms}, {"side": "google", "id": t_g}], dict(rec, notes="page edit"))
    finally:
        writer.listener = sync.after_write
    assert run()["updated"] == 0 and notes_g(t_g) == "page edit"

    # mass-delete safety stop
    set(sync_max_deletes=1)
    a = person("Contact-Sync Test A", "a@example.com", "(555) 555-0111")
    b = person("Contact-Sync Test B", "b@example.com", "(555) 555-0112")
    run()                                                 # links them as new pairs
    ms_delete(a[0])
    ms_delete(b[0])
    r = run()
    assert r["deleted"] == 0 and r["blocked"] and a[1] in G and b[1] in G
    r = run(allow_deletes=True)
    assert r["deleted"] == 2 and a[1] not in G and b[1] not in G
    set(sync_max_deletes=10)

    # test mode: real contacts are skipped, test contacts still sync
    set(write_mode="test")
    p_ms = next(c["id"] for c in MS.values() if c.get("surname") == "Pair")
    edit_ms(p_ms, personalNotes="should wait")
    edit_ms(t_ms, personalNotes="test mode works")
    r = run()
    assert r["skipped"] >= 1 and notes_g(t_g) == "test mode works"
    p_g = next(rn for rn, p in G.items() if (p.get("names") or [{}])[0].get("familyName") == "Pair")
    assert notes_g(p_g) != "should wait"
    set(write_mode="on")
    run()
    assert notes_g(p_g) == "should wait", "skipped change should go through once writing is on"

    # outage guard: a side that suddenly reads nearly empty changes nothing
    for i in range(10):
        person(f"Contact-Sync Bulk {i}", f"bulk{i}@example.com", f"(555) 555-02{i:02d}")
    run()
    saved = dict(G)
    G.clear()
    sync.run("test")
    assert "returned only 0" in last_run()["error"] and MS, "outage treated as deletes"
    G.update(saved)
    assert run()["deleted"] == 0


def test_robustness():
    set(write_mode="on")
    # a broken half link (one side gone) is cleaned up, not a crash
    store.x("INSERT INTO links(ms_id, g_id, name) VALUES(?,?,?)", ("AAM-ghost", None, "Ghost Example"))
    r = run()
    assert not store.q("SELECT 1 FROM links WHERE ms_id='AAM-ghost'") and "broken link" in r["log"]
    # one contact Google rejects doesn't stop the others
    x = person("Contact-Sync Bad", "bad@example.com", "(555) 555-0601")
    y = person("Contact-Sync Good", "good@example.com", "(555) 555-0602")
    run()
    edit_ms(x[0], personalNotes="will fail")
    edit_ms(y[0], personalNotes="goes through")
    real = google.update_person

    def picky(rn, body, fields):
        if rn == x[1]:
            raise net.HttpError(400, '{"error":"bad field"}', rn)
        return real(rn, body, fields)
    google.update_person = picky
    try:
        r = run()
    finally:
        google.update_person = real
    assert notes_g(y[1]) == "goes through" and "failed on Google" in r["log"] and r["error"] is None, r["log"]
    # a cleanup that is running makes sync runs skip
    writer.busy["cleanup"] = True
    try:
        assert sync.run("test") is None
    finally:
        writer.busy["cleanup"] = False
    # newest edit: real time compare (fractional seconds)
    assert sync._when("2026-01-01T00:00:00.5Z") > sync._when("2026-01-01T00:00:00Z")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
