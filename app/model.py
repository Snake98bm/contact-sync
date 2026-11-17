"""Turn raw Graph contacts and People API persons into one common shape, and turn an edited
contact back into each side's format.

Contact = {
  "side": "microsoft" | "google", "id": str, "name": str, "shown_as": str,
  "given": str, "middle": str, "family": str, "company": str, "title": str,
  "emails": [str], "phones": [{"value", "type"}], "addresses": [str],
  "addr": [{"type": home|work|other, "street", "city", "region", "postal", "country"}],
  "birthday": "YYYY-MM-DD" | "--MM-DD" | "", "notes": str, "groups": [str], "updated": str,
}
The editable part (what the edit page and restore write) is EDIT_FIELDS.
"""
EDIT_FIELDS = ("given", "middle", "family", "company", "title", "emails", "phones", "addr", "birthday", "notes")
GRAPH_ADDR = {"home": "homeAddress", "work": "businessAddress", "other": "otherAddress"}
ADDR_KEYS = ("street", "city", "region", "postal", "country")


def _clean(s):
    return " ".join((s or "").split())


def pick_name(shown, given, family, company, middle=""):
    """The person's name as 'First Middle Last'. A shown name in 'Last, First' form (left by some
    sync tools) is rebuilt from the name fields. No person name: the company name."""
    shown, parts = _clean(shown), _clean(f"{given or ''} {middle or ''} {family or ''}")
    if shown and not (", " in shown and parts):
        return shown
    return parts or shown or _clean(company)


def addr_line(a):
    return ", ".join(_clean(a.get(k)) for k in ADDR_KEYS if _clean(a.get(k)))


def from_graph(c):
    phones = [{"value": p, "type": "work"} for p in c.get("businessPhones") or []]
    phones += [{"value": p, "type": "home"} for p in c.get("homePhones") or []]
    if c.get("mobilePhone"):
        phones.append({"value": c["mobilePhone"], "type": "mobile"})
    addr = []
    for typ, kind in GRAPH_ADDR.items():
        a = c.get(kind) or {}
        item = {"type": typ, "street": (a.get("street") or "").strip(), "city": _clean(a.get("city")),
                "region": _clean(a.get("state")), "postal": _clean(a.get("postalCode")),
                "country": _clean(a.get("countryOrRegion"))}
        if addr_line(item):
            addr.append(item)
    given, middle, family = _clean(c.get("givenName")), _clean(c.get("middleName")), _clean(c.get("surname"))
    groups = list(c.get("categories") or [])
    if c.get("_folder") and c["_folder"] not in ("Contacts", None):
        groups.append("Folder: " + c["_folder"])
    return {
        "side": "microsoft", "id": c["id"],
        "name": pick_name(c.get("displayName"), given, family, c.get("companyName"), middle),
        "shown_as": _clean(c.get("displayName")), "given": given, "middle": middle, "family": family,
        "company": _clean(c.get("companyName")), "title": _clean(c.get("jobTitle")),
        "emails": [e["address"].strip() for e in c.get("emailAddresses") or [] if (e.get("address") or "").strip()],
        "phones": [p for p in phones if _clean(p["value"])],
        "addresses": [addr_line(a) for a in addr], "addr": addr,
        "birthday": (c.get("birthday") or "")[:10],
        "notes": (c.get("personalNotes") or "").strip(), "groups": groups,
        "updated": c.get("lastModifiedDateTime") or "",
    }


def from_people(p, group_names):
    """group_names: resourceName -> name, from contactGroups."""
    n = (p.get("names") or [{}])[0]
    given, middle, family = _clean(n.get("givenName")), _clean(n.get("middleName")), _clean(n.get("familyName"))
    org = (p.get("organizations") or [{}])[0]
    bday = ""
    for b in p.get("birthdays") or []:
        d = b.get("date") or {}
        if d.get("month") and d.get("day"):
            bday = (f"{d['year']:04d}-" if d.get("year") else "--") + f"{d['month']:02d}-{d['day']:02d}"
            break
    groups = []
    for m in p.get("memberships") or []:
        rn = (m.get("contactGroupMembership") or {}).get("contactGroupResourceName")
        if rn and rn not in ("contactGroups/myContacts", "contactGroups/starred"):
            groups.append(group_names.get(rn, rn))
    updated = ""
    for s in (p.get("metadata") or {}).get("sources") or []:
        if s.get("type") == "CONTACT":
            updated = s.get("updateTime", "")
    addr = []
    for a in p.get("addresses") or []:
        typ = (a.get("type") or "other").lower()
        item = {"type": typ if typ in GRAPH_ADDR else "other", "street": (a.get("streetAddress") or "").strip(),
                "city": _clean(a.get("city")), "region": _clean(a.get("region")),
                "postal": _clean(a.get("postalCode")), "country": _clean(a.get("country"))}
        if not addr_line(item) and _clean(a.get("formattedValue")):
            item["street"] = (a.get("formattedValue") or "").strip()      # free-text address
        if addr_line(item):
            addr.append(item)
    return {
        "side": "google", "id": p["resourceName"],
        "name": pick_name(n.get("displayName"), given, family, org.get("name"), middle),
        "shown_as": _clean(n.get("displayName")), "given": given, "middle": middle, "family": family,
        "company": _clean(org.get("name")), "title": _clean(org.get("title")),
        "emails": [e["value"].strip() for e in p.get("emailAddresses") or [] if (e.get("value") or "").strip()],
        "phones": [{"value": ph["value"], "type": (ph.get("type") or "other").lower()}
                   for ph in p.get("phoneNumbers") or [] if _clean(ph.get("value"))],
        "addresses": [addr_line(x) for x in addr], "addr": addr, "birthday": bday,
        "notes": "\n".join((b.get("value") or "").strip() for b in p.get("biographies") or [] if (b.get("value") or "").strip()),
        "groups": groups, "updated": updated,
    }


def editable(c):
    """The editable fields of a contact in the common shape."""
    return {k: c.get(k) if k not in ("emails", "phones", "addr") else list(c.get(k) or []) for k in EDIT_FIELDS}


def display_name(rec):
    return _clean(f"{rec.get('given', '')} {rec.get('middle', '')} {rec.get('family', '')}") or _clean(rec.get("company"))




# ---------- back to each side ----------
#
# Edits are merged into the contact's current raw data (`base`), so anything the edit page does not
# show (email/phone labels, extra address lines, department, nicknames, websites...) is kept.
# Restores use the saved raw copy itself (graph_writable / people_writable), so nothing is lost.

GRAPH_READ_ONLY = {"id", "changeKey", "createdDateTime", "lastModifiedDateTime", "parentFolderId", "_folder",
                   "photo", "extensions", "singleValueExtendedProperties", "multiValueExtendedProperties"}
PEOPLE_WRITABLE = ("addresses", "biographies", "birthdays", "calendarUrls", "clientData", "emailAddresses", "events",
                   "externalIds", "genders", "imClients", "interests", "locales", "locations", "memberships",
                   "miscKeywords", "names", "nicknames", "occupations", "organizations", "phoneNumbers", "relations",
                   "sipAddresses", "urls", "userDefined")
PEOPLE_OUTPUT_ONLY = {"metadata", "formattedType", "canonicalForm", "displayName", "displayNameLastFirst"}
GOOGLE_FIELDS = "names,emailAddresses,phoneNumbers,organizations,biographies,birthdays,addresses"
GOOGLE_ALL_FIELDS = ",".join(PEOPLE_WRITABLE)


def graph_writable(raw):
    """A saved Graph contact as a body for POST/PATCH (read-only properties removed)."""
    return {k: v for k, v in (raw or {}).items() if k not in GRAPH_READ_ONLY and not k.startswith("@")}


def _strip(item, keep_unstructured=True):
    out = {k: v for k, v in (item or {}).items() if k not in PEOPLE_OUTPUT_ONLY}
    if not keep_unstructured:
        out.pop("unstructuredName", None)
    return out


def people_writable(raw):
    """A saved People API person as a body for createContact/updateContact (output-only fields
    removed). Group memberships keep only the group reference; the built-in 'myContacts' is implied."""
    body = {}
    for f in PEOPLE_WRITABLE:
        items = [_strip(i) for i in (raw or {}).get(f) or []]
        if f == "memberships":
            items = [{"contactGroupMembership": {"contactGroupResourceName":
                                                 i["contactGroupMembership"]["contactGroupResourceName"]}}
                     for i in items if (i.get("contactGroupMembership") or {}).get("contactGroupResourceName")
                     not in (None, "contactGroups/myContacts")]
        if f == "names":
            items = items[:1]
        body[f] = items
    return body


def _bucket(phone_type):
    """Which Outlook phone slot a Google type lands in: mobile, home, or work (business)."""
    t = (phone_type or "").lower()
    return t if t in ("mobile", "home") else "work"


def _digits(s):
    return "".join(ch for ch in s or "" if ch.isdigit())[-10:]


def to_graph(rec, base=None):
    """Graph body for a create (base None) or an update merged onto the current contact.
    Returns (body, warnings): Outlook holds at most 3 emails, one mobile, 2 home and 2 business
    phones, and one address of each kind."""
    base = base or {}
    warn = []
    emails = rec.get("emails") or []
    if len(emails) > 3:
        warn.append(f"Outlook keeps 3 emails; {len(emails) - 3} left out")
    names = {(e.get("address") or "").lower(): e.get("name") for e in base.get("emailAddresses") or []}
    mobile, home, business = [], [], []
    for p in rec.get("phones") or []:
        t = (p.get("type") or "").lower()
        (mobile if t == "mobile" else home if t == "home" else business).append(p["value"])
    if len(mobile) > 1:
        business = mobile[1:] + business
        mobile = mobile[:1]
    if len(home) > 2 or len(business) > 2:
        warn.append("Outlook keeps 2 home and 2 business phones; extra numbers left out")
    body = {
        "givenName": rec.get("given") or None, "middleName": rec.get("middle") or None,
        "surname": rec.get("family") or None, "displayName": display_name(rec) or None,
        "companyName": rec.get("company") or None, "jobTitle": rec.get("title") or None,
        "emailAddresses": [{"address": e, "name": names.get(e.lower()) or e} for e in emails[:3]],
        "mobilePhone": mobile[0] if mobile else None, "homePhones": home[:2], "businessPhones": business[:2],
        "personalNotes": rec.get("notes") or "",
    }
    seen = set()
    for a in rec.get("addr") or []:
        kind = GRAPH_ADDR.get(a.get("type"), "otherAddress")
        if kind in seen:
            warn.append(f"Outlook keeps one {a.get('type')} address; an extra one left out")
            continue
        seen.add(kind)
        body[kind] = {"street": a.get("street", ""), "city": a.get("city", ""), "state": a.get("region", ""),
                      "postalCode": a.get("postal", ""), "countryOrRegion": a.get("country", "")}
    for kind in GRAPH_ADDR.values():         # blank fields clear an address that was removed
        body.setdefault(kind, {"street": "", "city": "", "state": "", "postalCode": "", "countryOrRegion": ""})
    b = rec.get("birthday") or ""
    if len(b) == 10 and b[:4].isdigit():
        body["birthday"] = f"{b}T11:59:00Z"
    elif not b:
        if base:
            body["birthday"] = None           # cleared on the edit page: clear it in Outlook too
    else:
        warn.append("Outlook needs a year for a birthday; it was left as it was")
    return body, warn


def to_people(rec, base=None):
    """People API body (GOOGLE_FIELDS) for a create, or an update merged onto `base` (the
    current person) so labels, types and sub-fields that the record does not carry are kept."""
    base = base or {}
    nm = _strip((base.get("names") or [{}])[0], keep_unstructured=False)   # rebuilt from the parts
    for k1, k2 in (("given", "givenName"), ("middle", "middleName"), ("family", "familyName")):
        if rec.get(k1):
            nm[k2] = rec[k1]
        else:
            nm.pop(k2, None)
    old_emails = {(e.get("value") or "").strip().lower(): _strip(e) for e in base.get("emailAddresses") or []}
    old_phones = {}
    for ph in base.get("phoneNumbers") or []:
        old_phones.setdefault(_digits(ph.get("value")), _strip(ph))
    phones = []
    for p in rec.get("phones") or []:
        item = dict(old_phones.get(_digits(p["value"]), {}))
        item["value"] = p["value"]
        old_t, new_t = (item.get("type") or "other").lower(), (p.get("type") or "other").lower()
        # Keep Google's own label (e.g. "Cottage") when the new type is just Outlook's coarse
        # kind for the same sort of number (mobile / home / work).
        coarse_same = new_t in ("mobile", "home", "work") and _bucket(old_t) == new_t
        if old_t != new_t and not (item.get("type") and coarse_same):
            item["type"] = new_t
        phones.append(item)
    org = _strip((base.get("organizations") or [{}])[0])
    org.update(name=rec.get("company") or "", title=rec.get("title") or "")
    old_addr = {}
    for a in base.get("addresses") or []:
        old_addr.setdefault((a.get("type") or "other").lower(), _strip(a))
    addresses = []
    for a in rec.get("addr") or []:
        item = dict(old_addr.get(a.get("type") or "other", {}))
        item.pop("formattedValue", None)          # Google rebuilds it from the parts
        item.update({"type": a.get("type") or "other", "streetAddress": a.get("street", ""), "city": a.get("city", ""),
                     "region": a.get("region", ""), "postalCode": a.get("postal", ""), "country": a.get("country", "")})
        addresses.append(item)
    body = {
        "names": [nm] if any(nm.get(k) for k in ("givenName", "middleName", "familyName", "honorificPrefix",
                                                 "honorificSuffix")) else [],
        "emailAddresses": [dict(old_emails.get(e.lower(), {}), value=e) for e in rec.get("emails") or []],
        "phoneNumbers": phones,
        "organizations": [org] if (org.get("name") or org.get("title") or org.get("department")) else [],
        "biographies": [{"value": rec["notes"], "contentType": "TEXT_PLAIN"}] if rec.get("notes") else [],
        "addresses": addresses,
        "birthdays": [],
    }
    b = rec.get("birthday") or ""
    old_b = from_people(dict(base, resourceName="people/x"), {})["birthday"] if base else ""
    if b and b == old_b:
        body["birthdays"] = [_strip(x) for x in base.get("birthdays") or []]      # unchanged: keep as stored
    elif len(b) == 10 and b[:4].isdigit():
        body["birthdays"] = [{"date": {"year": int(b[:4]), "month": int(b[5:7]), "day": int(b[8:10])}}]
    elif len(b) == 7 and b.startswith("--"):
        body["birthdays"] = [{"date": {"month": int(b[2:4]), "day": int(b[5:7])}}]
    return body
