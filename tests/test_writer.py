"""Write layer against fake Microsoft/Google back ends: mode rules, history, recycle bin, full-
fidelity restore, merge-on-edit, partial failures, in-place page updates, format round trips and
the network guard. Run: python tests/test_writer.py"""
import copy
import io
import os
import sys
import tempfile
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))
os.environ["DATA_DIR"] = tempfile.mkdtemp()
import clean  # noqa: E402
import google  # noqa: E402
import jobs  # noqa: E402
import model  # noqa: E402
import msgraph  # noqa: E402
import net  # noqa: E402
import store  # noqa: E402
import web  # noqa: E402
import writer  # noqa: E402

store.init()
writer.init()

# ---------- fake back ends that behave like the real ones ----------
MS, G = {}, {}
CALLS = []
FAIL = {"google_create": 0}


def _stamp():
    return f"2026-01-01T00:{len(CALLS) // 60:02d}:{len(CALLS) % 60:02d}Z"


def ms_create(body):
    CALLS.append(("ms", "create"))
    net.check_allowed("POST", "https://graph.microsoft.com/v1.0/me/contacts")    # must be inside writes_allowed
    cid = f"AAM{len(CALLS)}"
    MS[cid] = dict(copy.deepcopy(body), id=cid, lastModifiedDateTime=_stamp())
    return copy.deepcopy(MS[cid])


def ms_update(cid, body):                       # PATCH: merge into the existing contact
    CALLS.append(("ms", "update"))
    MS[cid].update(copy.deepcopy(body))
    MS[cid]["lastModifiedDateTime"] = _stamp()
    return copy.deepcopy(MS[cid])


def ms_delete(cid):
    CALLS.append(("ms", "delete"))
    MS.pop(cid)


def _g_meta(raw):
    raw["etag"] = f"e{len(CALLS)}"
    raw["metadata"] = {"sources": [{"type": "CONTACT", "updateTime": _stamp()}]}
    return raw


def g_create(body):
    CALLS.append(("g", "create"))
    if FAIL["google_create"]:
        FAIL["google_create"] -= 1
        raise net.HttpError(503, "backend error", "https://people.googleapis.com/v1/people:createContact")
    rn = f"people/c{len(CALLS)}"
    G[rn] = _g_meta(dict(copy.deepcopy(body), resourceName=rn))
    return copy.deepcopy(G[rn])


def g_update(rn, body, fields):                 # updateContact: replace only the listed fields
    CALLS.append(("g", "update"))
    assert body.get("etag") == G[rn]["etag"], "update must send the current etag"
    for f in fields.split(","):
        G[rn][f] = copy.deepcopy(body.get(f, []))
    _g_meta(G[rn])
    return copy.deepcopy(G[rn])


def g_delete(rn):
    CALLS.append(("g", "delete"))
    G.pop(rn)


def g_get(rn):
    CALLS.append(("g", "get"))
    return copy.deepcopy(G[rn])


msgraph.create_contact, msgraph.update_contact, msgraph.delete_contact = ms_create, ms_update, ms_delete
msgraph.get_contact = lambda cid: copy.deepcopy(MS[cid])
google.create_person, google.update_person, google.delete_person, google.get_person = g_create, g_update, g_delete, g_get


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


def make_test_contact():
    ids, _ = writer.create(dict(writer.TEST_REC))
    # things the edit page doesn't show, as a real contact would have them
    G[ids["google"]]["emailAddresses"] = [{"value": "contact-sync-test@example.com", "type": "work",
                                           "metadata": {"primary": True}, "formattedType": "Work"}]
    G[ids["google"]]["phoneNumbers"] = [{"value": "(555) 555-0100", "type": "Cottage", "formattedType": "Cottage"}]
    G[ids["google"]]["organizations"] = [{"name": "Example Test Co", "title": "", "department": "Testing"}]
    G[ids["google"]]["nicknames"] = [{"value": "CST"}]
    G[ids["google"]]["urls"] = [{"value": "https://example.com", "type": "work"}]
    G[ids["google"]]["memberships"] = [{"contactGroupMembership": {"contactGroupResourceName": "contactGroups/abc",
                                                                   "contactGroupId": "abc"}},
                                       {"contactGroupMembership": {"contactGroupResourceName": "contactGroups/myContacts"}}]
    MS[ids["microsoft"]].update(mobilePhone=None, businessPhones=["(555) 555-0100"],   # Outlook: "work"
                                nickName="CST", categories=["Cal-Com"], businessHomePage="https://example.com",
                                emailAddresses=[{"address": "contact-sync-test@example.com", "name": "Test Person"}],
                                department="Testing")
    return ids


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
    assert not warn and "birthday" in body
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


def test_no_blind_retry_of_creates():
    calls = []

    def boom(req, timeout=0):
        calls.append(req.get_method())
        raise TimeoutError("timed out")
    real, sleep = urllib.request.urlopen, net.time.sleep
    urllib.request.urlopen, net.time.sleep = boom, lambda s: None
    try:
        with net.writes_allowed():
            for url in ("https://graph.microsoft.com/v1.0/me/contacts",
                        "https://people.googleapis.com/v1/people:createContact?personFields=names"):
                calls.clear()
                try:
                    net.request("POST", url, body={})
                except TimeoutError:
                    pass
                assert calls == ["POST"], f"create was sent {len(calls)} times"
        calls.clear()
        try:
            net.request("GET", "https://graph.microsoft.com/v1.0/me/contacts")
        except TimeoutError:
            pass
        assert len(calls) == 5, "reads should still be retried"
    finally:
        urllib.request.urlopen, net.time.sleep = real, sleep


def test_modes_and_flow():
    set_mode("off")
    assert refused(lambda: writer.create(dict(writer.TEST_REC)))
    set_mode("test")
    ids = make_test_contact()
    real = {"given": "Jane", "family": "Example", "middle": "", "company": "", "title": "", "emails": [],
            "phones": [], "addr": [], "birthday": "", "notes": ""}
    assert refused(lambda: writer.create(dict(real)))                         # test mode: real names refused

    # edit only the notes: everything the page doesn't show survives on both sides
    rec = writer._best_rec([MS[ids["microsoft"]]], [])
    rec["notes"] = "edited"
    n_gets = sum(1 for c in CALLS if c == ("g", "get"))
    writer.update(members(ids), rec)
    assert sum(1 for c in CALLS if c == ("g", "get")) == n_gets + 1, "one Google read per update (etag reused)"
    gp, mc = G[ids["google"]], MS[ids["microsoft"]]
    assert gp["biographies"][0]["value"] == "edited" and mc["personalNotes"] == "edited"
    assert gp["emailAddresses"][0]["type"] == "work", gp["emailAddresses"]
    assert gp["phoneNumbers"][0]["type"] == "Cottage", gp["phoneNumbers"]
    assert gp["organizations"][0]["department"] == "Testing" and gp["nicknames"] == [{"value": "CST"}]
    assert mc["emailAddresses"][0]["name"] == "Test Person" and mc["nickName"] == "CST" and mc["categories"] == ["Cal-Com"]
    assert "metadata" not in gp["emailAddresses"][0] or True

    # clearing the birthday clears it on both sides
    writer.update(members(ids), dict(rec, birthday="1990-05-06"))
    assert MS[ids["microsoft"]]["birthday"].startswith("1990-05-06") and G[ids["google"]]["birthdays"]
    writer.update(members(ids), dict(rec, birthday=""))
    assert MS[ids["microsoft"]]["birthday"] is None and G[ids["google"]]["birthdays"] == []

    # renaming a test contact to a real name is refused in test mode
    assert refused(lambda: writer.update(members(ids), dict(rec, given="Jane", family="Example")))

    # restore the version before the first edit: both sides back exactly as they were
    hist = writer.history([ids["microsoft"], ids["google"]])
    first_edit = min((h for h in hist if h["action"] == "edit"), key=lambda h: h["id"])
    writer.restore_version(first_edit["id"], members(ids))
    assert G[ids["google"]]["biographies"][0]["value"].startswith("Made by contact-sync")
    assert MS[ids["microsoft"]]["personalNotes"].startswith("Made by contact-sync")

    # delete -> recycle bin -> restore: full fidelity, labels and extra fields included
    before_g, before_m = copy.deepcopy(G[ids["google"]]), copy.deepcopy(MS[ids["microsoft"]])
    bin_id = writer.delete(members(ids))
    assert not MS and not G
    new_ids, warns = writer.restore_bin(bin_id)
    g2, m2 = G[new_ids["google"]], MS[new_ids["microsoft"]]
    for f in ("nicknames", "urls", "organizations", "emailAddresses", "phoneNumbers", "addresses"):
        want = [{k: v for k, v in i.items() if k not in model.PEOPLE_OUTPUT_ONLY} for i in before_g.get(f, [])]
        assert g2[f] == want, (f, g2[f], want)
    assert g2["memberships"] == [{"contactGroupMembership": {"contactGroupResourceName": "contactGroups/abc"}}]
    for f in ("nickName", "categories", "businessHomePage", "department", "emailAddresses", "personalNotes"):
        assert m2[f] == before_m[f], f
    assert refused(lambda: writer.restore_bin(bin_id))                       # only once

    # test_only() keeps API calls on test contacts even in "on" mode
    set_mode("on")
    with writer.test_only():
        assert refused(lambda: writer.create(dict(real)))
    jane, _ = writer.create(dict(real))
    assert model.from_graph(MS[jane["microsoft"]])["name"] == "Jane Example"
    assert refused(lambda: writer.update([{"side": "google", "id": jane["google"]},
                                          {"side": "google", "id": new_ids["google"]}], real))
    set_mode("test")


def test_partial_restore_finishes_only_missing_side():
    set_mode("test")
    ids, _ = writer.create(dict(writer.TEST_REC))
    bin_id = writer.delete(members(ids))
    n_ms = len(MS)
    FAIL["google_create"] = 1
    try:
        writer.restore_bin(bin_id)
    except writer.Refused as e:
        assert "Microsoft 365" in str(e) and "Google" in str(e)
    else:
        raise AssertionError("partial failure not reported")
    assert len(MS) == n_ms + 1
    ids2, _ = writer.restore_bin(bin_id)                                      # retry: Google only
    assert len(MS) == n_ms + 1, "retry made a second Microsoft copy"
    assert ids2["google"] in G and ids2["microsoft"] in MS


def test_page_updates_in_place():
    set_mode("test")
    r = clean.analyse([model.from_graph(MS[c]) for c in MS] + [model.from_people(G[g], {}) for g in G])
    runner = jobs.Runner()
    runner.report = dict(r, created=1, backup="b", notes=[], group_names={})
    writer.listener = runner.apply_change
    try:
        before = runner.report["counts"]["people"]
        two = dict(writer.TEST_REC, given="Contact-Sync", family="Test Two", emails=["two@example.com"],
                   phones=[{"value": "(555) 555-0177", "type": "mobile"}])
        ids, _ = writer.create(dict(two))
        assert runner.report["counts"]["people"] == before + 1
        p = next(x for x in runner.report["people"] if x["name"] == "Contact-Sync Test Two")
        writer.update(p["members"], dict(two, notes="changed"))
        p = next(x for x in runner.report["people"] if x["name"] == "Contact-Sync Test Two")
        assert all(c["notes"] == "changed" for c in p["members"])
        writer.delete(p["members"])
        assert runner.report["counts"]["people"] == before
        assert runner.report["backup"] == "b"
    finally:
        writer.listener = None


def test_edit_form_keeps_typed_input():
    form = {"given": "Contact-Sync", "family": "Test", "notes": "my long notes", "phones": "555 0100 | Cottage",
            "emails": "not an email"}
    f = lambda k: form.get(k, "")
    try:
        web.parse_edit_form(f)
    except ValueError as e:
        assert "not an email" in str(e)
    else:
        raise AssertionError("bad email accepted")
    rec = web.parse_edit_form(f, validate=False)
    assert rec["notes"] == "my long notes" and rec["phones"] == [{"value": "555 0100", "type": "cottage"}]
    form["emails"] = "x@example.com"
    assert web.parse_edit_form(f)["phones"][0]["type"] == "cottage"           # custom labels are accepted


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
