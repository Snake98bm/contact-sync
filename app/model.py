"""Turn raw Graph contacts and People API persons into one common shape.

Contact = {
  "side": "microsoft" | "google", "id": str, "name": str, "given": str, "family": str,
  "company": str, "title": str, "emails": [str], "phones": [{"value", "type"}],
  "addresses": [str], "birthday": str, "notes": str, "groups": [str], "updated": str,
}
"""


def _clean(s):
    return " ".join((s or "").split())


def from_graph(c):
    phones = [{"value": p, "type": "work"} for p in c.get("businessPhones") or []]
    phones += [{"value": p, "type": "home"} for p in c.get("homePhones") or []]
    if c.get("mobilePhone"):
        phones.append({"value": c["mobilePhone"], "type": "mobile"})
    addrs = []
    for kind in ("homeAddress", "businessAddress", "otherAddress"):
        a = c.get(kind) or {}
        line = ", ".join(_clean(a.get(k)) for k in ("street", "city", "state", "postalCode", "countryOrRegion")
                         if _clean(a.get(k)))
        if line:
            addrs.append(line)
    given, family = _clean(c.get("givenName")), _clean(c.get("surname"))
    name = _clean(c.get("displayName")) or _clean(f"{given} {family}")
    groups = list(c.get("categories") or [])
    if c.get("_folder") and c["_folder"] != "Contacts":
        groups.append("Folder: " + c["_folder"])
    return {
        "side": "microsoft", "id": c["id"], "name": name, "given": given, "family": family,
        "company": _clean(c.get("companyName")), "title": _clean(c.get("jobTitle")),
        "emails": [e["address"].strip() for e in c.get("emailAddresses") or [] if (e.get("address") or "").strip()],
        "phones": [p for p in phones if _clean(p["value"])],
        "addresses": addrs, "birthday": (c.get("birthday") or "")[:10],
        "notes": (c.get("personalNotes") or "").strip(), "groups": groups,
        "updated": c.get("lastModifiedDateTime") or "",
    }


def from_people(p, group_names):
    """group_names: resourceName -> name, from contactGroups."""
    n = (p.get("names") or [{}])[0]
    given, family = _clean(n.get("givenName")), _clean(n.get("familyName"))
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
    return {
        "side": "google", "id": p["resourceName"], "name": _clean(n.get("displayName")) or _clean(f"{given} {family}"),
        "given": given, "family": family,
        "company": _clean(org.get("name")), "title": _clean(org.get("title")),
        "emails": [e["value"].strip() for e in p.get("emailAddresses") or [] if (e.get("value") or "").strip()],
        "phones": [{"value": ph["value"], "type": (ph.get("type") or "other").lower()}
                   for ph in p.get("phoneNumbers") or [] if _clean(ph.get("value"))],
        "addresses": [_clean(a.get("formattedValue")) for a in p.get("addresses") or [] if _clean(a.get("formattedValue"))],
        "birthday": bday,
        "notes": "\n".join((b.get("value") or "").strip() for b in p.get("biographies") or [] if (b.get("value") or "").strip()),
        "groups": groups, "updated": updated,
    }
