"""Cleanup analysis on made-up contacts. Run: python tests/test_clean.py"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))
import clean  # noqa: E402


def c(side, cid, name="", emails=(), phones=(), notes="", company="", groups=()):
    return {"side": side, "id": cid, "name": name, "given": "", "family": "", "company": company, "title": "",
            "emails": list(emails), "phones": [{"value": p, "type": "mobile"} for p in phones],
            "addresses": [], "birthday": "", "notes": notes, "groups": list(groups), "updated": ""}


def test_phones():
    n = clean.norm_phone
    assert n("(555) 555-0100") == ("+15555550100", "")
    assert n("555.555.0100") == ("+15555550100", "")
    assert n("1-555-555-0100") == ("+15555550100", "")
    assert n("+1 555 555 0100") == ("+15555550100", "")
    assert n("555-555-0100 x23") == ("+15555550100", "23")
    assert n("555 555 0100 ext. 7") == ("+15555550100", "7")
    assert n("011 44 20 7946 0000") == ("+442079460000", "")
    assert n("+44 20 7946 0000") == ("+442079460000", "")
    assert n("555-0100")[0] is None                       # too short to be sure
    assert n("020 7946 0000", "GB") == ("+442079460000", "")
    assert clean.pretty_phone("+15555550100") == "+1 555 555 0100"
    assert clean.pretty_phone("+15555550100", "23") == "+1 555 555 0100 x23"


def test_names():
    assert clean.fix_name("JANE EXAMPLE") == "Jane Example"
    assert clean.fix_name("jane example-smith") == "Jane Example-Smith"
    assert clean.fix_name("MARY MCDONALD") == "Mary McDonald"
    assert clean.fix_name("DeAngelo Example") == "DeAngelo Example"   # mixed case left alone
    assert clean.fix_name("  Jane   Example ") == "Jane Example"
    assert clean.fix_name("ACME") == "ACME" and clean.fix_name("exampletv") == "exampletv"   # brands


def test_extension_separators():
    assert clean.norm_phone("(555) 555-0100;102") == ("+15555550100", "102")
    assert clean.norm_phone("(555) 555-0100,7") == ("+15555550100", "7")
    assert clean.norm_phone("passcode 6563")[0] is None


def test_import_block():
    notes = ("Gate code 12\n-----------------------------\nContact Imported:\n"
             "Home Street : 1 Example Ave\nBusiness Street : 2 Sample Rd")
    own, block = clean.split_import_block(notes)
    assert own == "Gate code 12" and block.startswith("-----") and "Business Street" in block
    assert clean.split_import_block("just notes") == ("just notes", "")
    assert clean.split_import_block("-----------------------------\nContact Imported:\nHome Street : x")[0] == ""


def test_household_line_not_merged():
    contacts = [
        c("microsoft", "m1", "Jane Example", [], ["555-555-0100"]),
        c("microsoft", "m2", "John Example", [], ["555-555-0100"]),
        c("google", "g1", "Jane Example", [], ["555-555-0100"]),
        c("google", "g2", "John Example", [], ["555-555-0100"]),
        c("microsoft", "m3", "Dr Sam Sample", ["sam@sample.test"]),
        c("google", "g3", "Sam Sample", ["sam@sample.test"]),
    ]
    r = clean.analyse(contacts)
    k = r["counts"]
    assert k["dup_groups"] == 0 and k["paired"] == 3, k
    assert k["shared_info"] == 1 and len(r["shared_info"][0]["contacts"]) == 4


def test_variants_and_renames():
    contacts = [
        c("microsoft", "m1", "Micheal Example", [], ["555-555-0130"]),
        c("google", "g1", "Michael Example", [], ["555-555-0130"]),        # spelling variant: same person
        c("microsoft", "m2", "Cindy Sample", [], ["555-555-0140"]),
        c("google", "g2", "Cynthia Placeholder", [], ["555-555-0140"]),    # renamed on one side
    ]
    r = clean.analyse(contacts)
    assert r["counts"]["paired"] == 1, r["counts"]
    assert any(p["name"] == "same phone, names differ" for p in r["possible"]), r["possible"]
    assert clean.fix_name("Barbara &amp; Chuck Example") == "Barbara & Chuck Example"


def test_name_order():
    x = c("google", "g1", "Greg Example", ["greg@example.com"])
    x["shown_as"] = "Example, Greg"
    r = clean.analyse([x])
    assert r["counts"]["name_order"] == 1 and r["name_order"][0]["new"] == "Greg Example"


def test_analyse():
    contacts = [
        # same person on both sides, matched by phone format differences
        c("microsoft", "m1", "Jane Example", ["Jane@Example.com"], ["(555) 555-0100"]),
        c("google", "g1", "Jane Example", ["jane@example.com"], ["+15555550100"]),
        # duplicate on the Google side (shares the email)
        c("google", "g2", "JANE EXAMPLE", ["jane@example.com"]),
        # only in Microsoft
        c("microsoft", "m2", "John Sample", ["john@sample.test"]),
        # same name as John, nothing shared, John is missing from Google -> possible match
        c("google", "g3", "John Sample", [], ["555-555-0199"]),
        # two different Mikes, each properly paired -> not a possible match
        c("microsoft", "m5", "Mike", [], ["555-555-0111"]),
        c("google", "g6", "Mike", [], ["555-555-0111"]),
        c("microsoft", "m6", "Mike", [], ["555-555-0122"]),
        c("google", "g7", "Mike", [], ["555-555-0122"]),
        # junk
        c("google", "g4", "", [], []),
        c("microsoft", "m3", "", ["no-reply@shop.test"]),
        # old sync tool notes
        c("microsoft", "m4", "Pat Placeholder", ["pat@example.org"],
          notes="Met at the conference\nSynced by SyncGene 2024-03-01 id 8f3a9b2c1d"),
        c("google", "g5", "Pat Placeholder", ["pat@example.org"],
          notes="Synced by SyncGene 2024-05-11 id 77aa88bb99"),
    ]
    r = clean.analyse(contacts)
    k = r["counts"]
    assert k["microsoft"] == 6 and k["google"] == 7, k
    assert k["dup_groups"] == 1 and k["dup_extra"] == 1, k
    jane = r["duplicates"][0]
    assert jane["ms"] == 1 and jane["google"] == 2 and jane["name"] == "Jane Example"
    assert k["possible"] == 1 and r["possible"][0]["name"] == "john sample"
    assert k["junk"] == 2, r["junk"]
    assert k["sync_tool_contacts"] == 2
    assert r["sync_tool"]["lines"][0] == ("Synced by SyncGene # id #", 2), r["sync_tool"]["lines"]
    assert k["name_fixes"] == 1 and k["email_fixes"] == 1
    assert k["paired"] == 3, k                      # Pat and the two Mikes
    assert any(f["new"] == "+1 555 555 0100" for f in r["phone_fixes"])


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
