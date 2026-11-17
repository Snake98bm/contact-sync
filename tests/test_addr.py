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


def test_wrong_zip_and_jumbled_street():
    seen = []

    def fake(method, url, **kw):
        q = urllib.parse.unquote(url.split("address=")[1].split("&")[0])
        seen.append(q)
        if q.lower().startswith("47 s. example elm"):            # only the number-first order matches
            return {"result": {"addressMatches": [{"matchedAddress": "47 S EXAMPLE ELMS WAY, SPRINGFIELD, CA, 90002",
                                                   "addressComponents": {"city": "SPRINGFIELD", "state": "CA",
                                                                         "zip": "90002"}}]}}
        return {"result": {"addressMatches": []}}
    real, net.request = net.request, fake
    try:
        a, how, ok = addr.fix(A("Elm Suite K 47 S. Example", "", "CA", "90001"))
        assert a["street"] == "47 S Example Elms Way Suite K", a
        assert (a["city"], a["region"], a["postal"]) == ("Springfield", "CA", "90002"), a
        assert any("ZIP 90001 looks wrong" in h for h in how) and any("put in order" in h for h in how) and not ok, how
        assert all("Suite" not in q for q in seen), seen             # the suite is left out of the lookup
    finally:
        net.request = real


def _census_with(known):
    """Fake geocoder that only knows the given full addresses (street, city) -> zip."""
    def fake(method, url, **kw):
        q = urllib.parse.unquote(url.split("address=")[1].split("&")[0]).lower()
        hits = [(s, c, z) for (s, c), z in known.items() if q.startswith(f"{s.lower()}, {c.lower()}")]
        return {"result": {"addressMatches": [{"matchedAddress": f"{s.upper()}, {c.upper()}, CA, {z}",
                                               "addressComponents": {"city": c.upper(), "state": "CA", "zip": z}}
                                              for s, c, z in hits]}}
    return fake


def test_street_type_guess_and_city_from_zip():
    real = net.request
    try:
        net.request = _census_with({("1946 Example Garden Ave", "Springfield"): "90003",
                                    ("47 N Example Elms Dr", "Springfield"): "90004"})
        a, how, ok = addr.fix(A("1946 Example Garden", "Springfield", "CA", ""))
        assert (a["street"], a["postal"]) == ("1946 Example Garden Ave", "90003") and not ok, (a, how)
        assert any("street type guessed" in h for h in how)
        # no city, ZIP present: the city comes from other contacts with that ZIP
        a, how, ok = addr.fix(A("47 N Example Elms Dr Suite K", "", "CA", "90004"),
                              zip_city=addr.zip_cities([A("x", "Springfield", "CA", "90004")]))
        assert (a["street"], a["city"], a["postal"]) == ("47 N Example Elms Dr Suite K", "Springfield", "90004"), a
        # two different street types both match: too unsure, nothing guessed
        net.request = _census_with({("5 Example Ave", "Springfield"): "90005", ("5 Example St", "Springfield"): "90006"})
        a, how, ok = addr.fix(A("5 Example", "Springfield", "CA", ""))
        assert a["street"] == "5 Example" and not a["postal"] and not ok
    finally:
        net.request = real


def test_lookup_failure_is_reported_as_failure():
    def boom(method, url, **kw):
        raise TimeoutError("timed out")
    real, net.request = net.request, boom
    try:
        a, how, ok = addr.fix(A("7 Example Rd", "Springfield", "CA", ""))
        assert any("lookup failed" in h for h in how) and not any("no match" in h for h in how) and not ok, how
    finally:
        net.request = real


def test_app_start_creates_the_lookup_table():
    import inspect
    import main
    assert "addr.init()" in inspect.getsource(main.main)


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
    # the same place under two types and spelled differently: one kept, as Work
    other = A("1 Example Street", "Springfield", "CA", "90000", typ="other")
    work = A("1 Example St.", "Springfield", "CA", "", typ="work")
    rec, reasons, _ = cleanup.tidy(dict(c, addr=[other, work]))
    assert len(rec["addr"]) == 1 and rec["addr"][0]["type"] == "work" and rec["addr"][0]["postal"] == "90000", rec
    # same street in another town is a different place
    rec, _, _ = cleanup.tidy(dict(c, addr=[home, A("1 Example St", "Shelbyville", "CA", "", typ="work")]))
    assert len(rec["addr"]) == 2


def test_po_box_with_zip_in_street_box():
    def never(method, url, **kw):
        raise AssertionError("P.O. boxes are not looked up")
    real, net.request = net.request, never
    try:
        a, how, ok = addr.fix(A("Billing P.O. Box 77 zip 90008"),
                              zip_city=addr.zip_cities([A("x", "Springfield", "CA", "90008")]))
    finally:
        net.request = real
    assert (a["street"], a["city"], a["region"], a["postal"]) == ("P.O. Box 77", "Springfield", "CA", "90008"), a
    assert not ok and any("Billing" in h for h in how) and any("ZIP 90008" in h for h in how), how
    a, how, ok = addr.fix(A("PO Box 5", "Springfield", "CA", "90008"), lookup=False)
    assert a["street"] == "P.O. Box 5"


def test_house_number_on_its_own_line():
    seen = []

    def fake(method, url, **kw):
        seen.append(urllib.parse.unquote(url.split("address=")[1].split("&")[0]))
        return {"result": {"addressMatches": []}}
    real, net.request = net.request, fake
    try:
        a, how, ok = addr.fix(A("924\nN Example\nSpringfield, CA\nUnited States", "Springfield", "CA",
                                country="United States"))
    finally:
        net.request = real
    assert a["street"] == "924 N Example" and any("house number" in h for h in how), (a, how)
    assert seen and seen[0].startswith("924 N Example"), seen


def test_suite_anywhere_is_the_same_place():
    import cleanup
    k = lambda s: cleanup._street_key({"street": s})
    assert k("88 E. Example\nSte Q") == k("STE Q 88 E. Example") == k("88 E Example Suite Q")
    assert k("88 E Example Ste Q") != k("88 E Example Ste R") and k("88 E Example") != k("88 E Example Ste Q")
    assert k("1 Example St\nSpringfield") == k("1 Example St")


def test_zip_from_other_contacts_in_a_one_zip_town():
    real = net.request
    net.request = lambda method, url, **kw: {"result": {"addressMatches": []}}     # Census doesn't know it
    try:
        known = [A("1 X", "Smallville", "CA", "90009"), A("2 Y", "Smallville", "CA", "90009"),
                 A("3 Z", "Bigtown", "CA", "90010"), A("4 W", "Bigtown", "CA", "90011")]
        cz = addr.city_zips(known)
        a, how, ok = addr.fix(A("777 Example Lane", "Smallville", "CA", ""), city_zip=cz)
        assert a["postal"] == "90009" and not ok and any("other contacts in Smallville" in h for h in how), (a, how)
        a, how, ok = addr.fix(A("778 Example Lane", "Bigtown", "CA", ""), city_zip=cz)       # two ZIPs: no guess
        assert not a["postal"]
    finally:
        net.request = real


def test_openstreetmap_after_census_miss():
    seen = []

    def fake(method, url, **kw):
        seen.append(url)
        if "census.gov" in url:
            return {"result": {"addressMatches": []}}
        q = urllib.parse.parse_qs(url.split("?", 1)[1])
        assert set(q) >= {"street", "city", "state"} and "Suite" not in q["street"][0] and "Jane" not in url
        return [{"display_name": "62, Example Drive, Smallton, CA, 90012, United States",
                 "address": {"house_number": "62", "road": "Example Drive", "town": "Smallton",
                             "state": "California", "postcode": "90012"}},
                {"address": {"house_number": "99", "road": "Example Drive", "postcode": "90013"}}]   # other number
    real, net.request = net.request, fake
    try:
        a, how, ok = addr.fix(A("62 Example Drive Suite 4", "Smallton", "CA", ""), osm_lookup=True)
        assert a["postal"] == "90012" and not ok and any("OpenStreetMap has" in h for h in how), (a, how)
        assert any("openstreetmap.org" in u for u in seen)
        n = len(seen)
        addr.fix(A("62 Example Drive Suite 4", "Smallton", "CA", ""), osm_lookup=True)
        assert len(seen) == n, "second time comes from the cache"
        a, how, ok = addr.fix(A("63 Example Drive", "Smallton", "CA", ""))           # setting off: no OSM call
        assert not any("openstreetmap.org" in u for u in seen[n:]) and not a["postal"]
    finally:
        net.request = real


def test_street_differs():
    assert addr.street_differs("Suite K 47 S. Example Elm", "47 N Example Elms Dr")
    assert addr.street_differs("47 S Example Elms Dr", "47 N Example Elms Dr")
    assert not addr.street_differs("920 W. Example Ave", "920 W Example Avenue")
    assert not addr.street_differs("5 north example drive", "5 N EXAMPLE DR")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
