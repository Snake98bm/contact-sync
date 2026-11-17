"""Contacts and Recycle bin pages (search, filters, sorting, details) and photos in backups.
Run: python tests/test_pages.py"""
import base64
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))
os.environ["DATA_DIR"] = tempfile.mkdtemp()
import backup  # noqa: E402
import clean  # noqa: E402
import jobs  # noqa: E402
import store  # noqa: E402
import web  # noqa: E402
import writer  # noqa: E402

store.init()
writer.init()


def c(side, cid, given, family, emails=(), phones=(), notes="", groups=(), updated="2026-01-01T00:00:00Z", addr=(),
      company=""):
    return {"side": side, "id": cid, "name": f"{given} {family}".strip() or company, "shown_as": "", "given": given,
            "middle": "", "family": family, "company": company, "title": "", "emails": list(emails),
            "phones": [{"value": p, "type": "mobile"} for p in phones], "addr": list(addr),
            "addresses": [", ".join(v for v in a.values() if v) for a in addr], "birthday": "", "notes": notes,
            "groups": list(groups), "updated": updated}


def report():
    contacts = [
        c("microsoft", "m1", "Anna", "Zed", ["anna@example.com"], ["(555) 555-0101"], "likes tea", ["Cal-Com"],
          "2099-01-01T00:00:00Z", [{"type": "home", "street": "1 Example St", "city": "", "region": "CA", "postal": ""}]),
        c("google", "g1", "Anna", "Zed", ["anna@example.com"], ["(555) 555-0101"], "likes coffee", ["Cal-Com"]),
        c("microsoft", "m2", "Bob", "Young", ["bob@example.com"], [], company="Acme"),
        c("google", "g2", "Bob", "Young", ["bob@example.com"], [], company="Acme"),
        c("google", "g3", "Cara", "Xavier", [], ["(555) 555-0103"]),
    ]
    r = clean.analyse(contacts)
    r.update(created=1, groups=r["groups"])
    return r


def names_in(html):
    import re
    return re.findall(r"href='/contacts/[0-9a-f]{12}'>([^<]+)</a>", html)


def test_contacts_filters_and_sorts():
    r = report()
    assert names_in(web.contacts_page(r)) == ["Anna Zed", "Bob Young", "Cara Xavier"]
    assert names_in(web.contacts_page(r, sort="name_desc")) == ["Cara Xavier", "Bob Young", "Anna Zed"]
    assert names_in(web.contacts_page(r, sort="last")) == ["Cara Xavier", "Bob Young", "Anna Zed"]
    assert names_in(web.contacts_page(r, sort="company"))[0] == "Bob Young"
    assert names_in(web.contacts_page(r, sort="recent"))[0] == "Anna Zed"
    assert names_in(web.contacts_page(r, show="one")) == ["Cara Xavier"]
    assert names_in(web.contacts_page(r, show="both")) == ["Anna Zed", "Bob Young"]
    assert names_in(web.contacts_page(r, show="no_phone")) == ["Bob Young"]
    assert names_in(web.contacts_page(r, show="no_email")) == ["Cara Xavier"]
    assert names_in(web.contacts_page(r, show="no_addr")) == ["Bob Young", "Cara Xavier"]
    assert names_in(web.contacts_page(r, show="addr_gaps")) == ["Anna Zed"]
    assert names_in(web.contacts_page(r, show="notes")) == ["Anna Zed"]
    assert names_in(web.contacts_page(r, show="differ")) == ["Anna Zed"]          # notes differ
    assert names_in(web.contacts_page(r, show="recent")) == ["Anna Zed"]
    assert names_in(web.contacts_page(r, group="Cal-Com")) == ["Anna Zed"]
    assert names_in(web.contacts_page(r, q="acme")) == ["Bob Young"]
    assert "Any label or category" in web.contacts_page(r)


def _bin(name, ts, ms=None, g=None, restored=None, photo=None):
    return {"id": int(ts), "ts": ts, "name": name, "restored": restored, "note": None,
            "ms_raw": json.dumps([ms] if ms else []), "g_raw": json.dumps([g] if g else []),
            "has_photo": bool(photo), "ms_photo": None, "g_photo": photo, "reason": ""}


def bin_names(html):
    import re
    return re.findall(r"href='/bin/\d+'>([^<]+)</a>", html)


def test_bin_search_filter_sort_and_details():
    ms = {"id": "AAM1", "givenName": "Anna", "surname": "Zed", "displayName": "Anna Zed",
          "emailAddresses": [{"address": "anna@example.com"}], "mobilePhone": "(555) 555-0101"}
    g = {"resourceName": "people/c1", "names": [{"givenName": "Anna", "familyName": "Zed"}],
         "emailAddresses": [{"value": "anna@example.com"}], "biographies": [{"value": "a note"}]}
    jpeg = base64.b64encode(b"\xff\xd8\xff\xe0fakejpeg").decode()
    items = [_bin("Anna Zed", 300, ms, g, photo=jpeg), _bin("Bob Young", 200, ms={"id": "AAM2", "displayName": "Bob Young"}),
             _bin("Cara Xavier", 100, g={"resourceName": "people/c3", "names": [{"givenName": "Cara"}]}, restored=150)]
    items[1]["reason"] = "Sync: deleted in Google"
    items[0]["reason"] = "Cleanup: merged into another copy"
    page = lambda **kw: bin_names(web.bin_page(items, write_mode="on", **kw))
    assert page() == ["Anna Zed", "Bob Young", "Cara Xavier"]
    assert page(sort="old") == ["Cara Xavier", "Bob Young", "Anna Zed"]
    assert page(sort="name_desc") == ["Cara Xavier", "Bob Young", "Anna Zed"]
    assert page(show="waiting") == ["Anna Zed", "Bob Young"] and page(show="restored") == ["Cara Xavier"]
    assert page(show="both") == ["Anna Zed"] and page(show="photo") == ["Anna Zed"]
    assert page(show="sync") == ["Bob Young"] and page(show="cleanup") == ["Anna Zed"]
    assert page(q="5550101") == ["Anna Zed"] and page(q="deleted in google") == ["Bob Young"]
    d = web.bin_detail_page(items[0], "on")
    assert "data:image/jpeg;base64," in d and "a note" in d and "Restore to both" in d and "anna@example.com" in d
    assert "Restored" in web.bin_detail_page(items[2], "on")


def test_bin_reasons_from_history():
    writer._version("google", "people/c9", "Dee Example", "delete", before={}, note="sync: deleted in Microsoft 365 recycle bin #7")
    writer._version("microsoft", "AAM9", "Eve Example", "delete", before={}, note="recycle bin #8")
    reasons = writer.bin_reasons()
    assert reasons[7] == "Sync: deleted in Microsoft 365" and reasons[8] == "Deleted from the Contacts page"


def test_photos_in_backups():
    import google
    import msgraph
    msgraph.get_photo = lambda cid: b"\xff\xd8ms" if cid == "AAM1" else None
    google.get_photo = lambda raw: b"\xff\xd8g"
    raw_ms = {"contacts": [{"id": "AAM1"}, {"id": "AAM2"}], "folders": []}
    raw_g = {"people": [{"resourceName": "people/c1", "photos": [{"url": "https://lh3.googleusercontent.com/x=s100",
                                                                    "default": False}]},
                        {"resourceName": "people/c2", "photos": [{"url": "https://lh3.googleusercontent.com/y", "default": True}]}],
             "groups": []}
    photos = jobs.Runner()._photos(raw_ms, raw_g)
    assert set(photos["microsoft"]) == {"AAM1"} and set(photos["google"]) == {"people/c1"}
    backup.save(raw_ms, raw_g, [], {}, photos)
    assert base64.b64decode(backup.latest_photo("google", "people/c1")) == b"\xff\xd8g"
    assert backup.latest_photo("microsoft", "AAM2") is None


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
