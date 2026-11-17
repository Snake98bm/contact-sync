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
import threading
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
    elif len(lines) == 1 and lines[0].count(",") == 1:
        # '716 Example, Springfield Ca': the part after the comma is 'City ST' (or 'City ST ZIP')
        head, tail = (x.strip() for x in lines[0].split(","))
        t = _split_tail(tail)
        if head and t and t[0]:
            lines = [head, tail]
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


UNIT_RE = re.compile(r"[,\s]*\b(?:suite|ste\.?|apt\.?|apartment|unit|bldg\.?|building|space|spc\.?|#)\s*#?\s*[\w-]+",
                     re.I)


def split_unit(line):
    """'47 S Example Elms Way Suite K' -> ('47 S Example Elms Way', 'Suite K')."""
    m = UNIT_RE.search(line or "")
    if not m:
        return (line or "").strip(), ""
    return (line[:m.start()] + " " + line[m.end():]).strip(" ,"), m.group(0).strip(" ,")


def variants(core):
    """The street as typed, and, when it doesn't start with the house number, with the number first
    ('Elm 47 S. Example' -> '47 S. Example Elm')."""
    out = [core]
    if core and not core[0].isdigit():
        m = re.search(r"\b\d+[A-Za-z]?\b", core)
        if m:
            out.append(f"{core[m.start():]} {core[:m.start()]}".strip(" ,"))
    return out


def census(a, timeout=20, use_postal=True):
    """Ask the US Census geocoder. Returns {"city","region","postal","matched","street"} or None.
    Sends only the street (first line), city, state and (if use_postal) ZIP."""
    street = (a.get("street") or "").split("\n")[0].strip()
    q = ", ".join(x for x in (street, a.get("city") or "", a.get("region") or "",
                              (a.get("postal") or "") if use_postal else "") if x.strip())
    row = (store.q("SELECT result FROM addr_cache WHERE q=?", (q.lower(),)) or [None])[0]
    if row:
        return json.loads(row["result"])
    url = f"{CENSUS}?address={urllib.parse.quote(q)}&benchmark=Public_AR_Current&format=json"
    r = net.request("GET", url, timeout=timeout)
    matches = ((r.get("result") or {}).get("addressMatches") or [])
    out = None
    if len(matches) == 1:                        # one clear match only; several = ambiguous, leave it
        c = matches[0].get("addressComponents") or {}
        matched = matches[0].get("matchedAddress") or ""
        out = {"city": (c.get("city") or "").title(), "region": c.get("state") or "", "postal": c.get("zip") or "",
               "matched": matched, "street": matched.split(",")[0].strip().title()}
    store.x("INSERT OR REPLACE INTO addr_cache(q, result, ts) VALUES(?,?,?)", (q.lower(), json.dumps(out), time.time()))
    time.sleep(0.15)
    return out


OSM = "https://nominatim.openstreetmap.org/search"
_osm_lock = threading.Lock()
_osm_last = [0.0]


def osm(a, timeout=20):
    """Second try after the Census Bureau has no match: OpenStreetMap's address search (Brad OK 2026-11-17).
    Sends only the street (first line, without the suite), city and state. Its usage policy allows at most
    one request a second, so calls are spaced out; answers are cached like the Census ones.
    Returns {"city","region","postal","matched","street"} or None (only a match with the same house number)."""
    core, _unit = split_unit((a.get("street") or "").split("\n")[0].strip())
    num = re.match(r"\d+", core or "")
    if not num or not (a.get("city") or "").strip():
        return None
    params = {"street": core, "city": a["city"].strip(), "state": a.get("region") or "", "country": "us",
              "format": "jsonv2", "addressdetails": "1", "limit": "2"}
    key = "osm2|" + "|".join(params[k] for k in ("street", "city", "state")).lower()
    row = (store.q("SELECT result FROM addr_cache WHERE q=?", (key,)) or [None])[0]
    if row:
        return json.loads(row["result"])
    with _osm_lock:                                  # at most one request a second (their rule)
        wait = _osm_last[0] + 1.1 - time.time()
        if wait > 0:
            time.sleep(wait)
        try:
            r = net.request("GET", f"{OSM}?{urllib.parse.urlencode(params)}", timeout=timeout)
        finally:
            _osm_last[0] = time.time()
    out = None
    r = r if isinstance(r, list) else []
    hits = [h for h in r if (h.get("address") or {}).get("house_number") == num.group(0)
            and (h.get("address") or {}).get("postcode")]
    if not hits:
        # OpenStreetMap often knows the road but not every house number (rural): the road's ZIP will do,
        # when the street name matches (ignoring N/S/E/W and Dr/Drive) and there's one ZIP only
        name = _name_words(core[len(num.group(0)):])
        hits = [h for h in r if not (h.get("address") or {}).get("house_number") and (h.get("address") or {}).get("postcode")
                and name and _name_words((h.get("address") or {}).get("road") or "") == name]
    if len({h["address"]["postcode"][:5] for h in hits}) == 1:      # one clear ZIP only
        ad = hits[0]["address"]
        street = f"{num.group(0)} {ad.get('road', '')}".strip()
        town = ad.get("city") or ad.get("town") or ad.get("village") or ad.get("hamlet") or ""
        out = {"city": town, "region": state_code(ad.get("state") or ""), "postal": ad["postcode"][:5],
               "matched": hits[0].get("display_name") or street, "street": _osm_street(street)}
    store.x("INSERT OR REPLACE INTO addr_cache(q, result, ts) VALUES(?,?,?)", (key, json.dumps(out), time.time()))
    return out


def _name_words(s):
    """The street's name alone: 'East Sample Grove Drive' and 'Sample Grove Dr' both -> ['sample', 'grove']."""
    drop = {"n", "s", "e", "w", "ne", "nw", "se", "sw"} | {_ABBR.get(t, t) for t in STREET_TYPES}
    return [w for w in _street_words(s) if w not in drop]


def _osm_street(s):
    """'31 South Example Drive' -> '31 S Example Dr' (the way the rest of the app writes streets)."""
    short = {"north": "N", "south": "S", "east": "E", "west": "W", "avenue": "Ave", "street": "St", "drive": "Dr",
             "road": "Rd", "court": "Ct", "lane": "Ln", "boulevard": "Blvd", "place": "Pl", "circle": "Cir",
             "highway": "Hwy", "parkway": "Pkwy"}
    return " ".join(short.get(w.lower(), w) for w in s.split())


STREET_TYPES = {"ave", "avenue", "st", "street", "dr", "drive", "rd", "road", "ct", "court", "ln", "lane", "way",
                "blvd", "boulevard", "pl", "place", "cir", "circle", "hwy", "highway", "pkwy", "parkway", "ter",
                "terrace", "trl", "trail", "loop", "sq", "square"}
TRY_TYPES = ("Ave", "St", "Dr", "Rd", "Ct", "Ln", "Way")


_ABBR = {"avenue": "ave", "av": "ave", "street": "st", "drive": "dr", "road": "rd", "court": "ct", "lane": "ln",
         "boulevard": "blvd", "place": "pl", "circle": "cir", "highway": "hwy", "parkway": "pkwy", "terrace": "ter",
         "trail": "trl", "square": "sq", "north": "n", "south": "s", "east": "e", "west": "w",
         "northeast": "ne", "northwest": "nw", "southeast": "se", "southwest": "sw",
         "suite": "ste", "apartment": "apt", "building": "bldg", "space": "spc", "number": "#"}


def _street_words(s):
    words = re.sub(r"[^a-z0-9 ]", " ", (s or "").lower()).split()
    return [_ABBR.get(w, w) for w in words]


def street_differs(typed, census_street):
    """True when the Census street is really different (direction, name, missing street type), not just
    written differently ('W.' vs 'W', 'Avenue' vs 'Ave', capitals)."""
    return _street_words(typed) != _street_words(census_street)


def city_zips(addresses):
    """(city, state) -> ZIP, only for towns where all your contacts with a ZIP have the same one
    (a small town; a city with several ZIPs gets no guess)."""
    import collections
    seen = collections.defaultdict(set)
    for a in addresses:
        z, c = (a.get("postal") or "").strip()[:5], (a.get("city") or "").strip().lower()
        if re.fullmatch(r"\d{5}", z) and c:
            seen[(c, state_code(a.get("region") or "") or zip_state(z))].add(z)
    return {k: next(iter(v)) for k, v in seen.items() if len(v) == 1}


def zip_cities(addresses):
    """ZIP -> the city most of your contacts with that ZIP have (to fill a missing city for a lookup)."""
    import collections
    seen = collections.defaultdict(collections.Counter)
    for a in addresses:
        z, c = (a.get("postal") or "")[:5], (a.get("city") or "").strip()
        if re.fullmatch(r"\d{5}", z) and c:
            seen[z][c.title()] += 1
    return {z: cnt.most_common(1)[0][0] for z, cnt in seen.items()}


PO_RE = re.compile(r"\b(?:p\.?\s*o\.?\s*box|post\s+office\s+box)\s*#?\s*([\w-]+)", re.I)
ZIP_WORD_RE = re.compile(r"[,\s]*\bzip(?:\s*code)?\s*[:#]?\s*(\d{5}(?:-\d{4})?)\b", re.I)


def _street_extras(a):
    """'Note P.O. Box 12 zip 90000' in the street box: ZIP to the ZIP box, street as 'P.O. Box 12'."""
    a, how, sure = dict(a), [], True
    street = (a.get("street") or "").replace("\r", "")
    m = ZIP_WORD_RE.search(street)
    if m and not (a.get("postal") or "").strip():
        a["postal"] = m.group(1)
        street = (street[:m.start()] + street[m.end():]).strip()
        how.append(f"took ZIP {m.group(1)} out of the street box")
    lines = street.split("\n")
    m = PO_RE.search(lines[0])
    if m:
        extra = (lines[0][:m.start()] + " " + lines[0][m.end():]).strip(" ,-")
        lines[0] = f"P.O. Box {m.group(1)}"
        if extra:
            how.append(f"street as {lines[0]} (left out “{extra}”: check it)")
            sure = False
        street = "\n".join(lines)
    a["street"] = street
    return a, how, sure, bool(m)


def fix(a, lookup=True, zip_city=None, city_zip=None, osm_lookup=False):
    """(fixed address, how, confident) — how: list of what was done."""
    a, how, confident, po_box = _street_extras(a)
    before = dict(a)
    a = offline_fix(a)
    lines = [ln.strip() for ln in (a.get("street") or "").replace("\r", "").split("\n") if ln.strip()]
    if len(lines) >= 2 and re.fullmatch(r"\d+[A-Za-z]?(?:-\d+)?", lines[0]) and re.match(r"[A-Za-z]", lines[1]):
        a["street"] = "\n".join([f"{lines[0]} {lines[1]}"] + lines[2:])     # "924" / "N Example" on two lines
        how.append("joined the house number to its street")
    if (a.get("street") or "") != (before.get("street") or "") and any(
            (a.get(k) or "") != (before.get(k) or "") for k in ("city", "region", "postal")):
        how.append("split the address out of the street box")
    if (a.get("region") or "") != (before.get("region") or "") and "split the address out of the street box" not in how:
        how.append("state from ZIP" if not before.get("region") else "state as 2 letters")
    if po_box:
        # the Census Bureau can't look up P.O. boxes: the city comes from your other contacts with that ZIP
        z = (a.get("postal") or "")[:5]
        if not (a.get("city") or "").strip() and (zip_city or {}).get(z):
            a["city"] = zip_city[z]
            how.append(f"city from your other contacts with ZIP {z} (P.O. boxes can't be looked up: check it)")
            confident = False
        lookup = False
    # "2932 Example Ave / Springfield" with no state or ZIP: the last plain-words line is likely the city
    lines = [ln.strip() for ln in (a.get("street") or "").split("\n") if ln.strip()]
    if not (a.get("city") or "").strip() and len(lines) >= 2 and re.fullmatch(r"[A-Za-z][A-Za-z .'-]{1,29}", lines[-1]) \
            and not re.search(r"\b(suite|ste|apt|unit|box|floor|fl|bldg|building|room|rm|dept)\b", lines[-1], re.I):
        town = lines[-1]
        trial = dict(a, street="\n".join(lines[:-1]), city=town)
        got = None
        if lookup:
            try:
                got = census(trial)
            except Exception as e:
                log.warning("address lookup failed: %s", e)
        if got and (got.get("city") or "").lower() == town.lower():
            a = trial
            how.append(f"took the city “{town}” out of the street box (Census confirmed it)")
        else:
            a = trial
            how.append(f"took “{town}” out of the street box as the city (not confirmed: check it)")
            confident = False
    lines = [ln.strip() for ln in (a.get("street") or "").split("\n") if ln.strip()]
    if lookup and lines and (a.get("city") or a.get("postal") or a.get("region")):
        core, unit = split_unit(lines[0])
        # no city but a ZIP: borrow the city your other contacts with that ZIP have (the lookup needs one)
        city = (a.get("city") or "").strip() or (zip_city or {}).get((a.get("postal") or "")[:5], "")
        got = used = None
        guessed = False
        failed = ""
        try:
            for v in variants(core):
                q = dict(a, street=v, city=city)
                got = census(q, use_postal=False) or (census(q) if a.get("postal") else None)
                if got:
                    used = v
                    break
            if not got and city:
                # "1946 Example Garden": no street type. Try the common ones; accept only one clear answer.
                found = {}
                for v in variants(core):
                    if v.split()[-1].lower().rstrip(".") in STREET_TYPES:
                        continue
                    for t in TRY_TYPES:
                        g = census(dict(a, street=f"{v} {t}", city=city), use_postal=False)
                        if g:
                            found.setdefault(g["matched"], (g, v))
                        if len(found) > 1:
                            break
                    if len(found) > 1:
                        break
                if len(found) == 1:
                    got, used = next(iter(found.values()))
                    guessed = True
        except Exception as e:
            log.warning("address lookup failed: %s", e)
            got = None
            failed = str(e)[:120]
        if got:
            filled =[k for k in ("city", "region", "postal") if not (a.get(k) or "").strip() and got.get(k)]
            for k in filled:
                a[k] = got[k]
            if filled:
                how.append("looked up " + ", ".join({"city": "city", "region": "state", "postal": "ZIP"}[k] for k in filled)
                           + f" (Census: {got['matched']})")
            old_zip = (a.get("postal") or "").strip()
            if old_zip and got.get("postal") and "postal" not in filled and old_zip[:5] != got["postal"]:
                a["postal"] = got["postal"]
                how.append(f"ZIP {old_zip} looks wrong: the Census Bureau has {got['postal']} for {got['matched']}")
                confident = False                        # changes what's there: the owner decides
            if got.get("street") and (used != core or guessed or street_differs(core, got["street"])):
                a["street"] = "\n".join([got["street"] + (f" {unit}" if unit else "")] + lines[1:])
                how.append(("street type guessed: the Census Bureau knows it as " if guessed else
                            "street put in order as the Census Bureau knows it: " if used != core else
                            "street differs from what the Census Bureau has: ") + got["street"])
                confident = False
        elif failed:
            how.append(f"the address lookup failed ({failed}): rebuild the plan later to try again")
            confident = False
        elif needs_lookup(a):
            o = None
            if osm_lookup:                            # second try: OpenStreetMap often has rural/newer addresses
                try:
                    o = osm(dict(a, street=core, city=city))
                except Exception as e:
                    log.warning("OpenStreetMap lookup failed: %s", e)
            if o:
                filled = [k for k in ("city", "region", "postal") if not (a.get(k) or "").strip() and o.get(k)]
                for k in filled:
                    a[k] = o[k]
                how.append("the Census Bureau had no match; OpenStreetMap has " + o["street"] + ", "
                           + ", ".join(x for x in (o["city"], o["region"], o["postal"]) if x)
                           + (": filled in " + ", ".join({"city": "city", "region": "state", "postal": "ZIP"}[k]
                                                         for k in filled) if filled else "") + " (check it)")
                if o.get("street") and street_differs(core, o["street"]):
                    a["street"] = "\n".join([o["street"] + (f" {unit}" if unit else "")] + lines[1:])
                confident = False
            else:
                how.append("no match found (Census Bureau" + (" or OpenStreetMap" if osm_lookup else "")
                           + "): fill in by hand if you like")
                confident = False
    # still no ZIP: if every other contact in this town has the same one, suggest it (to check)
    town = ((a.get("city") or "").strip().lower(), state_code(a.get("region") or ""))
    if (a.get("street") or "").strip() and not (a.get("postal") or "").strip() and (city_zip or {}).get(town):
        a["postal"] = city_zip[town]
        how.append(f"ZIP {a['postal']} from your other contacts in {a['city'].strip()} (the only ZIP they have "
                   "there): check it")
        confident = False
    if (a.get("street") or "").strip() and not all((a.get(k) or "").strip() for k in ("city", "region", "postal")):
        confident = False                            # still incomplete: let the owner look first
    return a, how, confident
