"""Cleanup analysis. Reads contacts in the common shape (model.py) and works out what a cleanup
would do: formatting fixes, duplicates, possible matches, junk and leftovers from an older sync
tool. Nothing here changes any account."""
import collections
import difflib
import hashlib
import html
import re

NOREPLY = re.compile(r"^(no-?reply|do-?not-?reply|mailer-daemon|notifications?|bounce)[^@]*@", re.I)
SYNC_TOOL = re.compile(r"sync ?gene|syncgene|synced (by|with|from)|sync id|cira ?apps", re.I)
# Block an older sync/import tool appended to notes: a dashed line, "Contact Imported:", then
# "Field : value" lines for fields it couldn't map (mostly addresses that are also in the address fields).
IMPORT_BLOCK = re.compile(r"-{10,}[ \t]*\r?\n[ \t]*Contact Imported:?.*\Z", re.S | re.I)
EXT = re.compile(r"\s*(?:ext\.?|extension|x|#|;|,)\s*(\d{1,6})\s*$", re.I)
TITLES = {"dr", "mr", "mrs", "ms", "miss", "prof", "rev", "sir"}
CALLING_CODES = {"US": "1", "CA": "1", "GB": "44", "AU": "61", "NZ": "64", "IE": "353", "MX": "52"}


# ---------- formatting ----------

def norm_email(e):
    return (e or "").strip().strip("<>").lower()


def norm_phone(raw, region="US"):
    """Return (e164, ext) or (None, ext) when the number can't be read with confidence.
    e164 looks like +15555550123."""
    s = (raw or "").strip()
    ext = ""
    m = EXT.search(s)
    if m and len(re.sub(r"\D", "", s[:m.start()])) >= 7:
        ext, s = m.group(1), s[:m.start()]
    digits = re.sub(r"\D", "", s)
    cc = CALLING_CODES.get(region, "1")
    if s.lstrip().startswith("+"):
        return ("+" + digits if 8 <= len(digits) <= 15 else None), ext
    if digits.startswith("011") and len(digits) > 10:
        return "+" + digits[3:], ext
    if digits.startswith("00") and len(digits) > 10:
        return "+" + digits[2:], ext
    if cc == "1":
        if len(digits) == 10 and digits[0] not in "01":
            return "+1" + digits, ext
        if len(digits) == 11 and digits[0] == "1":
            return "+" + digits, ext
        return None, ext
    if digits.startswith("0") and 9 <= len(digits) <= 11:
        return "+" + cc + digits[1:], ext
    return None, ext


def pretty_phone(e164, ext=""):
    """+15555550123 -> (555) 555-0123; other countries keep their +code and digits."""
    if not e164:
        return ""
    if e164.startswith("+1") and len(e164) == 12:
        out = f"({e164[2:5]}) {e164[5:8]}-{e164[8:]}"
    else:
        out = e164
    return out + (f" x{ext}" if ext else "")


def fix_name(name):
    """Web-page codes (&amp;) back to characters; ALL CAPS or all lower case -> Title Case for
    names of two or more words. Mixed case is left alone (McDonald, DeAngelo), and so are single
    words, which are often brands (USAA, directv)."""
    n = " ".join(html.unescape(name or "").split())
    letters = [c for c in n if c.isalpha()]
    if len(letters) < 2 or len(n.split(" ")) < 2 or not (n.isupper() or n.islower()):
        return n
    words = []
    for w in n.lower().split(" "):
        w = "-".join(p[:1].upper() + p[1:] for p in w.split("-"))
        if w.startswith(("Mc", "O'")) and len(w) > 2:
            w = w[:2] + w[2].upper() + w[3:]
        words.append(w)
    return " ".join(words)


def name_key(c):
    n = re.sub(r"[^a-z0-9 ]", "", fix_name(c["name"]).lower())
    n = " ".join(n.split())
    return n if len(n) >= 3 else ""


def names_match(a, b):
    """Could two contacts be the same person? Yes if either has no name, one name contains the
    other, or the first names agree (titles like Dr ignored). A shared phone or email between
    'Jane Example' and 'John Example' (a household line) is not enough."""
    ka, kb = name_key(a), name_key(b)
    if not ka or not kb or ka == kb:
        return True
    fa = [t for t in ka.split() if t not in TITLES]
    fb = [t for t in kb.split() if t not in TITLES]
    # one name inside the other, as whole words ("Greg" / "Greg Smith", not "Ann" / "Joann Example")
    if fa and fb and (set(fa) <= set(fb) or set(fb) <= set(fa)):
        return True
    if not (fa and fb):
        return False
    if fa[0] == fb[0]:
        return True
    # spelling variants of the first name (Micheal / Michael) when the rest of the name agrees
    return fa[1:] == fb[1:] and len(fa) > 1 and difflib.SequenceMatcher(None, fa[0], fb[0]).ratio() >= 0.75


def split_import_block(notes):
    """(your own text, the tool's block or '')."""
    m = IMPORT_BLOCK.search(notes or "")
    if not m:
        return (notes or "").strip(), ""
    return notes[:m.start()].strip(), m.group(0)


def keys(c, region="US"):
    out = set()
    for e in c["emails"]:
        e = norm_email(e)
        if "@" in e:
            out.add("email:" + e)
    for p in c["phones"]:
        e164, _ = norm_phone(p["value"], region)
        if e164:
            out.add("phone:" + e164)
    return out


# ---------- analysis ----------

class _UF:
    def __init__(self):
        self.p = {}

    def find(self, a):
        self.p.setdefault(a, a)
        while self.p[a] != a:
            self.p[a] = self.p[self.p[a]]
            a = self.p[a]
        return a

    def union(self, a, b):
        self.p[self.find(a)] = self.find(b)


def is_junk(c):
    """A reason string, or ''."""
    has_name = bool(name_key(c)) or bool(c["company"])
    if not has_name and not c["emails"] and not c["phones"]:
        return "No name, email or phone"
    if c["emails"] and all(NOREPLY.match(norm_email(e)) for e in c["emails"]) and not c["phones"]:
        return "Only a no-reply address"
    if not has_name and not c["phones"] and len(c["emails"]) == 1 and not c["notes"]:
        return "Just an email address, no name"
    return ""


def analyse(contacts, region="US"):
    """contacts: both sides in the common shape. Returns the report dict."""
    by_id = {(c["side"], c["id"]): c for c in contacts}
    uf = _UF()
    holders = collections.defaultdict(list)       # email/phone -> contacts that have it
    shared = {}                                   # different people sharing an email/phone
    for c in contacts:
        node = (c["side"], c["id"])
        uf.find(node)
        for k in keys(c, region):
            for other in holders[k]:
                if names_match(c, by_id[other]):
                    uf.union(node, other)
                else:
                    shared.setdefault(k, set()).update({node, other})
            holders[k].append(node)

    groups = collections.defaultdict(list)
    for node in by_id:
        groups[uf.find(node)].append(node)

    people = []
    for members in groups.values():
        cs = [by_id[n] for n in members]
        ms = [c for c in cs if c["side"] == "microsoft"]
        gg = [c for c in cs if c["side"] == "google"]
        key = hashlib.sha1("|".join(sorted(f"{c['side']}:{c['id']}" for c in cs)).encode()).hexdigest()[:12]
        people.append({"key": key, "members": cs, "ms": len(ms), "google": len(gg),
                       "name": max((fix_name(c["name"]) for c in cs), key=len, default="")})

    # possible matches: someone missing on one side who has the same name as someone else
    # (often the same person with an unreadable or changed phone)
    by_name = collections.defaultdict(list)
    for i, p in enumerate(people):
        for nk in {name_key(c) for c in p["members"]} - {""}:
            by_name[nk].append(i)
    possible = []
    for nk, idx in by_name.items():
        idx = sorted(set(idx))
        if len(idx) > 1 and any(people[i]["ms"] == 0 or people[i]["google"] == 0 for i in idx):
            possible.append({"name": nk, "people": [people[i] for i in idx]})

    # household/business lines: one phone or email on contacts that are clearly different people
    person_of = {(c["side"], c["id"]): i for i, p in enumerate(people) for c in p["members"]}
    shared_info, seen_pairs = [], set()
    for k, nodes in shared.items():
        kind, _, value = k.partition(":")
        shared_info.append({"kind": kind, "value": value, "contacts": [by_id[n] for n in sorted(nodes)]})
        # ...but a one-sided contact in Microsoft and a one-sided one in Google with the same number is
        # probably one contact renamed on one side
        idx = {person_of[n] for n in nodes}
        only_m = [i for i in idx if people[i]["google"] == 0]
        only_g = [i for i in idx if people[i]["ms"] == 0]
        for a in only_m:
            for b in only_g:
                if (a, b) not in seen_pairs:
                    seen_pairs.add((a, b))
                    possible.append({"name": f"same {kind}, names differ", "people": [people[a], people[b]]})

    # notes blocks left by an older import/sync tool, and names stored as "Last, First"
    import_blocks, name_order = [], []
    for c in contacts:
        own, block = split_import_block(c["notes"])
        if block:
            import_blocks.append({"contact": c, "keeps_text": bool(own), "no_address": not c["addresses"],
                                  "block": block[:400]})
        if ", " in c.get("shown_as", "") and c["name"] != c.get("shown_as"):
            name_order.append({"contact": c, "old": c["shown_as"], "new": c["name"]})

    # formatting
    phone_fixes, phone_unknown, name_fixes, email_fixes = [], [], [], []
    for c in contacts:
        for p in c["phones"]:
            e164, ext = norm_phone(p["value"], region)
            if not e164:
                phone_unknown.append({"contact": c, "value": p["value"]})
            elif p["value"].strip() != pretty_phone(e164, ext):
                phone_fixes.append({"contact": c, "old": p["value"], "new": pretty_phone(e164, ext)})
        if c["name"] and fix_name(c["name"]) != c["name"]:
            name_fixes.append({"contact": c, "old": c["name"], "new": fix_name(c["name"])})
        for e in c["emails"]:
            if norm_email(e) != e:
                email_fixes.append({"contact": c, "old": e, "new": norm_email(e)})

    # leftovers from older sync tools, and note lines that repeat across many contacts
    tool_lines = collections.Counter()
    tool_contacts = []
    line_count = collections.Counter()
    for c in contacts:
        own_text = split_import_block(c["notes"])[0]   # the import block is reported on its own
        lines = {ln.strip() for ln in own_text.splitlines() if ln.strip()}
        for ln in lines:
            line_count[_shape(ln)] += 1
        hits = [ln for ln in lines if SYNC_TOOL.search(ln)]
        if hits:
            tool_contacts.append({"contact": c, "lines": hits})
            for ln in hits:
                tool_lines[_shape(ln)] += 1
    group_count = collections.Counter(g for c in contacts for g in c["groups"])

    junk = [{"contact": c, "reason": r} for c in contacts if (r := is_junk(c))]

    return {
        "counts": {
            "microsoft": sum(1 for c in contacts if c["side"] == "microsoft"),
            "google": sum(1 for c in contacts if c["side"] == "google"),
            "people": len(people),
            "paired": sum(1 for p in people if p["ms"] == 1 and p["google"] == 1),
            "only_ms": sum(1 for p in people if p["google"] == 0),
            "only_google": sum(1 for p in people if p["ms"] == 0),
            "dup_groups": sum(1 for p in people if p["ms"] > 1 or p["google"] > 1),
            "dup_extra": sum(max(p["ms"] - 1, 0) + max(p["google"] - 1, 0) for p in people),
            "possible": len(possible), "junk": len(junk),
            "phone_fixes": len(phone_fixes), "phone_unknown": len(phone_unknown),
            "name_fixes": len(name_fixes), "email_fixes": len(email_fixes),
            "sync_tool_contacts": len(tool_contacts),
            "import_blocks": len(import_blocks), "name_order": len(name_order),
            "shared_info": len(shared_info),
        },
        "import_blocks": import_blocks, "name_order": name_order, "shared_info": shared_info,
        "people": sorted(people, key=lambda p: (p["name"] or "~").lower()),
        "duplicates": sorted((p for p in people if p["ms"] > 1 or p["google"] > 1),
                             key=lambda p: -(p["ms"] + p["google"])),
        "only_ms": [p for p in people if p["google"] == 0],
        "only_google": [p for p in people if p["ms"] == 0],
        "possible": possible, "junk": junk,
        "phone_fixes": phone_fixes, "phone_unknown": phone_unknown,
        "name_fixes": name_fixes, "email_fixes": email_fixes,
        "sync_tool": {"contacts": tool_contacts, "lines": tool_lines.most_common(30)},
        "repeated_note_lines": [(ln, n) for ln, n in line_count.most_common(30) if n >= 5],
        "groups": group_count.most_common(),
    }


def _shape(line):
    """Collapse ids, dates and numbers so the same kind of line counts together:
    'Synced 2024-03-01 id 8f3a…' -> 'Synced # id #'."""
    line = re.sub(r"[0-9a-f]{8,}(-[0-9a-f]{4,})*", "#", line, flags=re.I)
    line = re.sub(r"\d+([:/.\-]\d+)*", "#", line)
    return line[:160]
