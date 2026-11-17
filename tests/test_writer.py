"""Write layer against fake Microsoft/Google back ends: mode rules, history, recycle bin, restore,
format round trips and the network guard. Run: python tests/test_writer.py"""
import copy
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))
os.environ["DATA_DIR"] = tempfile.mkdtemp()
import google  # noqa: E402
import model  # noqa: E402
import msgraph  # noqa: E402
import net  # noqa: E402
import store  # noqa: E402
import writer  # noqa: E402

store.init()
writer.init()

# ---------- fake back ends ----------
MS, G = {}, {}
CALLS = []


def _ms_store(cid, body):
    raw = copy.deepcopy(body)
    raw.update(id=cid, lastModifiedDateTime=f"2026-01-01T00:00:{len(CALLS):02d}Z")
    MS[cid] = raw
    return copy.deepcopy(raw)


def ms_create(body):
    CALLS.append(("ms", "create"))
    assert net.check_allowed("POST", "https://graph.microsoft.com/v1.0/me/contacts") is None
    return _ms_store(f"AAM{len(MS) + 1}", body)


def ms_update(cid, body):
    CALLS.append(("ms", "update"))
    return _ms_store(cid, body)


def ms_delete(cid):
    CALLS.append(("ms", "delete"))
    MS.pop(cid)


def g_store(rn, body):
    raw = copy.deepcopy(body)
    raw.update(resourceName=rn, etag=f"e{len(CALLS)}",
               metadata={"sources": [{"type": "CONTACT", "updateTime": f"2026-01-01T00:00:{len(CALLS):02d}Z"}]})
    raw.pop("etag_check", None)
    G[rn] = raw
    return copy.deepcopy(raw)


def g_create(body):
    CALLS.append(("g", "create"))
    return g_store(f"people/c{len(G) + 1}", body)


def g_update(rn, body, fields):
    CALLS.append(("g", "update"))
    assert body.get("etag") == G[rn]["etag"], "update must send the current etag"
    assert fields == model.GOOGLE_FIELDS
    return g_store(rn, body)


def g_delete(rn):
    CALLS.append(("g", "delete"))
    G.pop(rn)


msgraph.create_contact, msgraph.update_contact, msgraph.delete_contact = ms_create, ms_update, ms_delete
msgraph.get_contact = lambda cid: copy.deepcopy(MS[cid])
google.create_person, google.update_person, google.delete_person = g_create, g_update, g_delete
google.get_person = lambda rn: copy.deepcopy(G[rn])


def members(ids):
    return [{"side": "microsoft", "id": ids["microsoft"]}, {"side": "google", "id": ids["google"]}]


def set_mode(m):
    s = store.load_settings()
    s["write_mode"] = m
    store.save_settings(s)


def refused(fn):
    try:
        fn()
    except writer.Refused:
        return True
    return False


# ---------- tests ----------

def test_round_trip():
    rec = dict(writer.TEST_REC, middle="Q", birthday="1980-02-03", title="Owner",
               phones=[{"value": "(555) 555-0100", "type": "mobile"}, {"value": "(555) 555-0101", "type": "home"},
                       {"value": "(555) 555-0102", "type": "work"}],
               addr=writer.TEST_REC["addr"] + [{"type": "work", "street": "2 Sample Rd\nSuite 5", "city": "Exampleville",
                                                "region": "CA", "postal": "90001", "country": "USA"}])
    g = model.from_people(dict(model.to_people(rec), resourceName="people/c1"), {})
    body, warn = model.to_graph(rec)
    ms = model.from_graph(dict(body, id="x"))
    for side in (g, ms):
        got = model.editable(side)
        for k in model.EDIT_FIELDS:
            want = rec[k]
            if k == "phones":
                assert sorted((p["value"], p["type"]) for p in got[k]) == sorted((p["value"], p["type"]) for p in want), (k, got[k])
            elif k == "addr":
                assert sorted(a["street"] for a in got[k]) == sorted(a["street"] for a in want), (k, got[k])
            else:
                assert got[k] == want, (side["side"], k, got[k], want)
    assert not warn
    # Outlook limits produce warnings, not silent loss
    _, warn = model.to_graph(dict(rec, emails=["a@x.test", "b@x.test", "c@x.test", "d@x.test"], birthday="--02-03"))
    assert any("3 emails" in w for w in warn) and any("year" in w for w in warn)


def test_guard():
    for method, url in (("POST", "https://graph.microsoft.com/v1.0/me/contacts"),
                        ("DELETE", "https://people.googleapis.com/v1/people/c1:deleteContact")):
        try:
            net.check_allowed(method, url)
        except net.WriteBlocked:
            pass
        else:
            raise AssertionError("allowed outside writes_allowed()")
    with net.writes_allowed():
        net.check_allowed("PATCH", "https://graph.microsoft.com/v1.0/me/contacts/AAMk%3D1")
        net.check_allowed("PATCH", "https://people.googleapis.com/v1/people/c1:updateContact?updatePersonFields=names")
        for method, url in (("DELETE", "https://people.googleapis.com/v1/contactGroups/abc"),
                            ("POST", "https://graph.microsoft.com/v1.0/me/sendMail"),
                            ("DELETE", "https://graph.microsoft.com/v1.0/me/contactFolders/x")):
            try:
                net.check_allowed(method, url)
            except net.WriteBlocked:
                continue
            raise AssertionError(f"{method} {url} allowed")


def test_modes_and_flow():
    set_mode("off")
    assert refused(lambda: writer.create(dict(writer.TEST_REC)))
    set_mode("test")
    ids, _ = writer.create(dict(writer.TEST_REC))
    assert set(ids) == {"microsoft", "google"} and len(MS) == 1 and len(G) == 1
    real = {"given": "Jane", "family": "Example", "middle": "", "company": "", "title": "", "emails": [],
            "phones": [], "addr": [], "birthday": "", "notes": ""}
    assert refused(lambda: writer.create(dict(real)))                         # test mode: real names refused
    # edit the test contact on both sides, history keeps the old version
    rec = writer._best_rec([MS[ids["microsoft"]]], [])
    rec["notes"] = "edited"
    writer.update(members(ids), rec)
    assert MS[ids["microsoft"]]["personalNotes"] == "edited"
    assert G[ids["google"]]["biographies"][0]["value"] == "edited"
    hist = writer.history([ids["microsoft"], ids["google"]])
    assert [h["action"] for h in hist][:2] == ["edit", "edit"]
    # renaming a test contact to a real name is refused in test mode
    assert refused(lambda: writer.update(members(ids), dict(rec, given="Jane", family="Example")))
    # restore the version before the edit
    v = next(h for h in hist if h["side"] == "google" and h["action"] == "edit")
    writer.restore_version(v["id"], members(ids))
    assert "edited" not in MS[ids["microsoft"]]["personalNotes"] and "Safe to delete" in G[ids["google"]]["biographies"][0]["value"]
    # delete -> recycle bin -> restore to both
    bin_id = writer.delete(members(ids))
    assert not MS and not G
    item = writer.bin_items()[0]
    assert item["id"] == bin_id and item["name"] == "Contact-Sync Test" and not item["restored"]
    new_ids, _ = writer.restore_bin(bin_id)
    assert len(MS) == 1 and len(G) == 1 and set(new_ids) == {"microsoft", "google"}
    assert model.from_graph(MS[new_ids["microsoft"]])["emails"] == ["contact-sync-test@example.com"]
    assert refused(lambda: writer.restore_bin(bin_id))                       # only once
    # test_only() keeps API calls on test contacts even in "on" mode
    set_mode("on")
    with writer.test_only():
        assert refused(lambda: writer.create(dict(real)))
    jane, _ = writer.create(dict(real))                                       # signed-in page, mode on: allowed
    assert model.from_graph(MS[jane["microsoft"]])["name"] == "Jane Example"
    # duplicates on one side can't be edited
    assert refused(lambda: writer.update([{"side": "google", "id": jane["google"]},
                                          {"side": "google", "id": new_ids["google"]}], real))


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
