"""Graph/People -> common shape, vCard output, read-only guard, Google paste parsing, API key.
Run: python tests/test_model.py"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))
os.environ["DATA_DIR"] = tempfile.mkdtemp()
os.environ["API_KEY"] = "test-key-0123456789abcdef"
import auth  # noqa: E402
import backup  # noqa: E402
import google  # noqa: E402
import model  # noqa: E402
import net  # noqa: E402

GRAPH = {
    "id": "AAMk1", "displayName": "Jane Example", "givenName": "Jane", "surname": "Example",
    "companyName": "Example Co", "jobTitle": "Owner",
    "emailAddresses": [{"name": "Jane", "address": "jane@example.com "}, {"address": ""}],
    "businessPhones": ["555-555-0100"], "homePhones": [], "mobilePhone": "555-555-0101",
    "homeAddress": {"street": "1 Main St", "city": "Springfield", "state": "CA", "postalCode": "90000"},
    "birthday": "1980-02-03T11:59:00Z", "personalNotes": "Likes tea", "categories": ["Family"],
    "lastModifiedDateTime": "2026-01-01T00:00:00Z", "_folder": "Contacts/Work",
}
PERSON = {
    "resourceName": "people/c1",
    "names": [{"displayName": "John Sample", "givenName": "John", "familyName": "Sample"}],
    "emailAddresses": [{"value": "john@sample.test"}],
    "phoneNumbers": [{"value": "(555) 555-0199", "type": "mobile"}],
    "organizations": [{"name": "Sample LLC", "title": "Manager"}],
    "birthdays": [{"date": {"month": 4, "day": 5}}],
    "biographies": [{"value": "Line one\nLine two"}],
    "memberships": [{"contactGroupMembership": {"contactGroupResourceName": "contactGroups/myContacts"}},
                    {"contactGroupMembership": {"contactGroupResourceName": "contactGroups/abc"}}],
    "metadata": {"sources": [{"type": "CONTACT", "updateTime": "2026-02-02T00:00:00Z"}]},
}


def test_graph():
    c = model.from_graph(GRAPH)
    assert c["name"] == "Jane Example" and c["emails"] == ["jane@example.com"]
    assert [p["type"] for p in c["phones"]] == ["work", "mobile"]
    assert c["addresses"] == ["1 Main St, Springfield, CA, 90000"]
    assert c["birthday"] == "1980-02-03" and c["groups"] == ["Family", "Folder: Contacts/Work"]


def test_people():
    c = model.from_people(PERSON, {"contactGroups/abc": "Friends"})
    assert c["name"] == "John Sample" and c["company"] == "Sample LLC"
    assert c["birthday"] == "--04-05" and c["groups"] == ["Friends"]
    assert c["notes"] == "Line one\nLine two" and c["updated"].startswith("2026-02-02")


def test_pick_name():
    assert model.pick_name("Example, Greg", "Greg", "Example", "") == "Greg Example"
    assert model.pick_name("Jane Example", "Jane", "Example", "") == "Jane Example"
    assert model.pick_name("", "", "", "Example Plumbing") == "Example Plumbing"
    assert model.pick_name("Example, Inc", "", "", "") == "Example, Inc"      # no parts to rebuild from
    p = dict(PERSON, names=[{"displayName": "Sample, John", "givenName": "John", "familyName": "Sample"}])
    c = model.from_people(p, {})
    assert c["name"] == "John Sample" and c["shown_as"] == "Sample, John"


def test_vcard():
    v = backup.vcard(model.from_people(PERSON, {}))
    assert v.startswith("BEGIN:VCARD\r\n") and v.endswith("END:VCARD\r\n")
    assert "NOTE:Line one\\nLine two" in v and "TEL;TYPE=CELL:(555) 555-0199" in v


def test_read_only_guard():
    net.check_allowed("GET", "https://graph.microsoft.com/v1.0/me/contacts")
    net.check_allowed("POST", "https://oauth2.googleapis.com/token")
    net.check_allowed("POST", "https://login.microsoftonline.com/x/oauth2/v2.0/token")
    for method, url in (("POST", "https://graph.microsoft.com/v1.0/me/contacts"),
                        ("PATCH", "https://graph.microsoft.com/v1.0/me/contacts/1"),
                        ("DELETE", "https://people.googleapis.com/v1/people/c1:deleteContact"),
                        ("POST", "https://people.googleapis.com/v1/people:createContact")):
        try:
            net.check_allowed(method, url)
        except net.WriteBlocked:
            continue
        raise AssertionError(f"{method} {url} was allowed")


def test_google_paste():
    assert google.parse_pasted("http://localhost/?state=abc&code=4/xyz&scope=s") == ("4/xyz", "abc", "")
    assert google.parse_pasted("  ?state=abc&code=4/xyz ") == ("4/xyz", "abc", "")
    assert google.parse_pasted("http://localhost/?error=access_denied&state=abc")[2] == "access_denied"
    assert google.parse_pasted("nonsense")[0] == ""


def test_api_key():
    assert auth.api_key_ok("Bearer test-key-0123456789abcdef")
    assert auth.api_key_ok("bearer  test-key-0123456789abcdef ")
    assert not auth.api_key_ok("Bearer wrong")
    assert not auth.api_key_ok("Basic test-key-0123456789abcdef")
    assert not auth.api_key_ok(None)


def test_backup_paths():
    assert backup.path_of("../etc", "passwd") is None
    assert backup.path_of("2026-01-01_000000", "../../auth.json") is None


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
