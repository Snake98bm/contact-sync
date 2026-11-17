"""Address repair (no real lookups: the Census call is faked). Run: python tests/test_addr.py"""
import os
import sys
import tempfile
import urllib.parse

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))
os.environ["DATA_DIR"] = tempfile.mkdtemp()
import addr  # noqa: E402
import net  # noqa: E402
import store  # noqa: E402

store.init()
addr.init()
addr.time.sleep = lambda s: None


def A(street="", city="", region="", postal="", country="", typ="home"):
    return {"type": typ, "street": street, "city": city, "region": region, "postal": postal, "country": country}


def test_split_street():
    s = addr.split_street
    a = s(A("1 Example St\nSpringfield\nCA\n90000\nUnited States"))
    assert (a["street"], a["city"], a["region"], a["postal"], a["country"]) == \
        ("1 Example St", "Springfield", "CA", "90000", "United States"), a
    a = s(A("1 Example St\nSpringfield, CA 90000"))
    assert (a["street"], a["city"], a["region"], a["postal"]) == ("1 Example St", "Springfield", "CA", "90000"), a
    a = s(A("1 Example St, Springfield, CA 90000"))
    assert (a["street"], a["city"], a["region"], a["postal"]) == ("1 Example St", "Springfield", "CA", "90000"), a
    a = s(A("1 Example St\nSuite 5\nSpringfield\nCalifornia\n90000"))
    assert (a["street"], a["city"], a["region"], a["postal"]) == ("1 Example St\nSuite 5", "Springfield", "CA", "90000"), a
    a = s(A("1 Example St\nSpringfield\nUnited States"))     # no state or ZIP: city is not guessed
    assert a["street"] == "1 Example St\nSpringfield" and a["country"] == "United States" and not a["city"], a
    a = s(A("1 Example St\nSpringfield, CA 90000", city="Shelbyville"))   # disagrees with a filled box:
    assert a == A("1 Example St\nSpringfield, CA 90000", city="Shelbyville")   # nothing is dropped
    a = s(A("1 Example St\nSpringfield, CA 90000", city="Springfield"))   # agrees: the duplicate lines go
    assert (a["street"], a["city"], a["region"], a["postal"]) == ("1 Example St", "Springfield", "CA", "90000")
    assert s(A("1 Example St")) == A("1 Example St")


def test_state_from_zip_and_names():
    assert addr.zip_state("93292") == "CA" and addr.zip_state("10001") == "NY" and addr.zip_state("") == ""
    a = addr.offline_fix(A("1 Example St", "Springfield", "", "93277"))
    assert a["region"] == "CA"
    assert addr.offline_fix(A("x", "y", "california", "93277"))["region"] == "CA"


def test_lookup_fills_only_missing_and_caches():
    calls = []

    def fake(method, url, **kw):
        calls.append(url)
        assert "Jane" not in url and method == "GET"
        return {"result": {"addressMatches": [{"matchedAddress": "1 EXAMPLE ST, SPRINGFIELD, CA, 90000",
                                               "addressComponents": {"city": "SPRINGFIELD", "state": "CA", "zip": "90000"}}]}}
    real, net.request = net.request, fake
    try:
        a, how, ok = addr.fix(A("1 Example St", "Springfield", "CA", ""))
        assert a["postal"] == "90000" and a["city"] == "Springfield" and ok and "ZIP" in how[-1], (a, how)
        addr.fix(A("1 Example St", "Springfield", "CA", ""))
        assert len(calls) == 1, "second lookup should come from the cache"
        a, how, ok = addr.fix(A("1 Example St", "Springfield", "CA", "90000"))      # complete: no lookup
        assert len(calls) == 1 and not how
    finally:
        net.request = real


def test_no_match_is_not_confident():
    real = net.request
    net.request = lambda method, url, **kw: {"result": {"addressMatches": []}}
    try:
        a, how, ok = addr.fix(A("99 Nowhere Rd", "Springfield", "CA", ""))
        assert not ok and not a["postal"] and "no match" in how[-1]
    finally:
        net.request = real


def test_town_line_becomes_city():
    def fake(method, url, **kw):
        ok = "Springfield" in urllib.parse.unquote(url)
        return {"result": {"addressMatches": [{"matchedAddress": "2932 EXAMPLE AVE, SPRINGFIELD, CA, 90000",
                                               "addressComponents": {"city": "SPRINGFIELD", "state": "CA", "zip": "90000"}}]
                           if ok else []}}
    real, net.request = net.request, fake
    try:
        a, how, ok = addr.fix(A("2932 Example Ave\nSpringfield\nUnited States"))
        assert (a["street"], a["city"], a["region"], a["postal"], a["country"]) == \
            ("2932 Example Ave", "Springfield", "CA", "90000", "United States"), a
        assert ok and "Census confirmed" in how[0], how
        a, how, ok = addr.fix(A("1 Nowhere Rd\nShelbyville"))           # can't be confirmed: suggested, unticked
        assert a["city"] == "Shelbyville" and a["street"] == "1 Nowhere Rd" and not ok and "check it" in how[0]
        a, _, _ = addr.fix(A("1 Example St\nSuite 5"))                   # a suite line is never a city
        assert a["street"] == "1 Example St\nSuite 5" and not a["city"]
    finally:
        net.request = real


def test_incomplete_is_not_ticked():
    a, how, ok = addr.fix(A("1 Example St", "Springfield", "", ""), lookup=False)
    assert not ok


def test_repeated_address_removed():
    import cleanup
    home = A("1 Example St", "Springfield", "CA", "")
    fuller = A("1 Example St", "Springfield", "CA", "90000")
    c = {"side": "google", "id": "people/c1", "name": "Jane Example", "shown_as": "", "given": "Jane", "middle": "",
         "family": "Example", "company": "", "title": "", "emails": [], "phones": [], "addr": [home, fuller],
         "addresses": [], "birthday": "", "notes": "", "groups": [], "updated": ""}
    rec, reasons, _ = cleanup.tidy(c)
    assert len(rec["addr"]) == 1 and rec["addr"][0]["postal"] == "90000" and "remove repeated address" in reasons


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
