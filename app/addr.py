"""Address repair for the cleanup.

Offline:
- a whole address typed into the street box ("1 Example St / Springfield / CA / 90000 / United
  States", or "1 Example St, Springfield, CA 90000") is split into street, city, state, ZIP, country;
- state names become 2-letter codes; a missing state comes from the ZIP's first 3 digits.
Online (only when the owner has turned it on): a missing ZIP (or state/city) is looked up with the
US Census Bureau's free geocoder. Only street, city and state are sent, never a name. Results are
cached in the database so rebuilding the plan doesn't ask again.
"""
import json
import logging
import re
import time
import urllib.parse

import net
import store

log = logging.getLogger("addr")
CENSUS = "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress"

STATE_NAMES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA", "colorado": "CO",
    "connecticut": "CT", "delaware": "DE", "district of columbia": "DC", "florida": "FL", "georgia": "GA",
    "hawaii": "HI", "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD", "massachusetts": "MA", "michigan": "MI",
    "minnesota": "MN", "mississippi": "MS", "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY", "north carolina": "NC",
    "north dakota": "ND", "ohio": "OH", "oklahoma": "OK", "oregon": "OR", "pennsylvania": "PA",
    "rhode island": "RI", "south carolina": "SC", "south dakota": "SD", "tennessee": "TN", "texas": "TX",
    "utah": "UT", "vermont": "VT", "virginia": "VA", "washington": "WA", "west virginia": "WV",
    "wisconsin": "WI", "wyoming": "WY", "puerto rico": "PR", "calif": "CA", "cal": "CA",
}
STATES = set(STATE_NAMES.values())
COUNTRY_WORDS = {"united states", "united states of america", "usa", "us", "u.s.", "u.s.a."}

# first 3 ZIP digits -> state (USPS ranges)
_ZIP3 = [((5, 5), "NY"), ((6, 9), "PR"), ((10, 27), "MA"), ((28, 29), "RI"), ((30, 38), "NH"), ((39, 49), "ME"),
         ((50, 54), "VT"), ((55, 55), "MA"), ((56, 59), "VT"), ((60, 69), "CT"), ((70, 89), "NJ"),
         ((100, 149), "NY"), ((150, 196), "PA"), ((197, 199), "DE"), ((200, 200), "DC"), ((201, 201), "VA"),
         ((202, 205), "DC"), ((206, 219), "MD"), ((220, 246), "VA"), ((247, 268), "WV"), ((270, 289), "NC"),
         ((290, 299), "SC"), ((300, 319), "GA"), ((320, 349), "FL"), ((350, 369), "AL"), ((370, 385), "TN"),
         ((386, 397), "MS"), ((398, 399), "GA"), ((400, 427), "KY"), ((430, 459), "OH"), ((460, 479), "IN"),
         ((480, 499), "MI"), ((500, 528), "IA"), ((530, 549), "WI"), ((550, 567), "MN"), ((569, 569), "DC"),
         ((570, 577), "SD"), ((580, 588), "ND"), ((590, 599), "MT"), ((600, 629), "IL"), ((630, 658), "MO"),
         ((660, 679), "KS"), ((680, 693), "NE"), ((700, 714), "LA"), ((716, 729), "AR"), ((730, 749), "OK"),
         ((750, 799), "TX"), ((800, 816), "CO"), ((820, 831), "WY"), ((832, 838), "ID"), ((840, 847), "UT"),
         ((850, 865), "AZ"), ((870, 884), "NM"), ((885, 885), "TX"), ((889, 898), "NV"), ((900, 961), "CA"),
         ((967, 968), "HI"), ((970, 979), "OR"), ((980, 994), "WA"), ((995, 999), "AK")]

SCHEMA = "CREATE TABLE IF NOT EXISTS addr_cache(q TEXT PRIMARY KEY, result TEXT, ts REAL);"
ZIP_RE = re.compile(r"^\d{5}(-\d{4})?$")


def init():
    with store.connect() as db:
        db.executescript(SCHEMA)


def state_code(s):
    s = (s or "").strip().rstrip(".")
    if s.upper() in STATES:
        return s.upper()
    return STATE_NAMES.get(s.lower(), s)


def zip_state(z):
    m = re.match(r"^(\d{3})\d{2}", (z or "").strip())
    if not m:
        return ""
    n = int(m.group(1))
    return next((st for (lo, hi), st in _ZIP3 if lo <= n <= hi), "")


def _split_tail(s):
    """'Springfield, CA 90000' / 'Springfield CA' / 'CA 90000' -> (city, state, zip) or None."""
    m = re.fullmatch(r"\s*(?:(.*?)[,\s]+)?([A-Za-z]{2}|[A-Za-z][A-Za-z .]+?)\.?\s*(\d{5}(?:-\d{4})?)?\s*", s)
    if not m:
        return None
    city, st, z = (m.group(1) or "").strip(" ,"), m.group(2), m.group(3) or ""
    if state_code(st) not in STATES:
        return None
    return city, state_code(st), z


def split_street(a):
    """If the whole address sits in the street box (several lines, or 'street, city, ST ZIP'), split it
    into the other boxes. Only empty boxes are filled. Returns a new address dict."""
    a = dict(a)
    lines = [ln.strip() for ln in (a.get("street") or "").replace("\r", "").split("\n") if ln.strip()]
    if len(lines) == 1 and lines[0].count(",") >= 2:
        lines = [x.strip() for x in lines[0].split(",") if x.strip()]
    if len(lines) < 2:
        return a
    found = {}
    while len(lines) > 1:                        # read from the end; the first line always stays the street
        last = lines[-1]
        if last.lower().strip(".") in COUNTRY_WORDS and "country" not in found:
            found["country"] = "United States"
            lines.pop()
            continue
        if ZIP_RE.match(last) and "postal" not in found and "region" not in found:
            found["postal"] = last
            lines.pop()
            continue
        if state_code(last) in STATES and "region" not in found and len(last) <= 20:
            found["region"] = state_code(last)
            lines.pop()
            continue
        t = _split_tail(last)
        if t and "region" not in found and "city" not in found:
            city, st, z = t
            found["region"] = st
            if z and "postal" not in found:
                found["postal"] = z
            lines.pop()
            if city:
                found["city"] = city
                break
            continue                             # "CA 90000" alone: the city may be the line before
        if ("region" in found or "postal" in found) and "city" not in found and not re.match(r"^\d", last):
            found["city"] = last
            lines.pop()
        break
    if not found or not lines:
        return a
    norm = lambda k, v: (state_code(v) if k == "region" else " ".join((v or "").split())).lower()
    for k, v in found.items():
        if (a.get(k) or "").strip() and norm(k, a[k]) != norm(k, v) and k != "country":
            return dict(a, street=a.get("street"))  # the street box disagrees with a filled box: leave it all
    a["street"] = "\n".join(lines)
    for k, v in found.items():
        if not (a.get(k) or "").strip():
            a[k] = v
    return a


def offline_fix(a):
    """Split a crammed street, state as a 2-letter code, state from ZIP. Returns a new dict."""
    a = split_street(a)
    if a.get("region"):
        a["region"] = state_code(a["region"])
    if not a.get("region") and a.get("postal"):
        a["region"] = zip_state(a["postal"])
    if (a.get("country") or "").lower().strip(".") in COUNTRY_WORDS:
        a["country"] = "United States"
    return a


def needs_lookup(a):
    return bool((a.get("street") or "").strip()) and not (a.get("postal") and a.get("region") and a.get("city")) \
        and bool(a.get("city") or a.get("postal") or a.get("region"))


def census(a, timeout=20):
    """Ask the US Census geocoder. Returns {"city","region","postal","matched"} or None.
    Sends only the street (first line), city, state and ZIP."""
    street = (a.get("street") or "").split("\n")[0].strip()
    q = ", ".join(x for x in (street, a.get("city") or "", a.get("region") or "", a.get("postal") or "") if x.strip())
    row = (store.q("SELECT result FROM addr_cache WHERE q=?", (q.lower(),)) or [None])[0]
    if row:
        return json.loads(row["result"])
    url = f"{CENSUS}?address={urllib.parse.quote(q)}&benchmark=Public_AR_Current&format=json"
    r = net.request("GET", url, timeout=timeout)
    matches = ((r.get("result") or {}).get("addressMatches") or [])
    out = None
    if len(matches) == 1:                        # one clear match only; several = ambiguous, leave it
        c = matches[0].get("addressComponents") or {}
        out = {"city": (c.get("city") or "").title(), "region": c.get("state") or "", "postal": c.get("zip") or "",
               "matched": matches[0].get("matchedAddress") or ""}
    store.x("INSERT OR REPLACE INTO addr_cache(q, result, ts) VALUES(?,?,?)", (q.lower(), json.dumps(out), time.time()))
    time.sleep(0.3)
    return out


def fix(a, lookup=True):
    """(fixed address, how, confident) — how: list of what was done."""
    before = dict(a)
    a = offline_fix(a)
    how = []
    if (a.get("street") or "") != (before.get("street") or "") and any(
            (a.get(k) or "") != (before.get(k) or "") for k in ("city", "region", "postal")):
        how.append("split the address out of the street box")
    if (a.get("region") or "") != (before.get("region") or "") and "split the address out of the street box" not in how:
        how.append("state from ZIP" if not before.get("region") else "state as 2 letters")
    confident = True
    if lookup and needs_lookup(a):
        try:
            got = census(a)
        except Exception as e:
            log.warning("address lookup failed: %s", e)
            got = None
        if got:
            filled = [k for k in ("city", "region", "postal") if not (a.get(k) or "").strip() and got.get(k)]
            for k in filled:
                a[k] = got[k]
            if filled:
                how.append("looked up " + ", ".join({"city": "city", "region": "state", "postal": "ZIP"}[k] for k in filled)
                           + f" (Census: {got['matched']})")
        else:
            how.append("no match found: fill in by hand if you like")
            confident = False
    return a, how, confident
