"""Backups: the raw API data (what a restore would use) plus a .vcf per side that any contacts app
can import. One folder per run under /data/backups/<YYYY-MM-DD_HHMMSS>/."""
import gzip
import json
import os
import shutil
import time

import store

NAME_FMT = "%Y-%m-%d_%H%M%S"


def _vesc(s):
    return (s or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def vcard(c):
    """vCard 3.0 for one contact in the common shape."""
    out = ["BEGIN:VCARD", "VERSION:3.0", f"FN:{_vesc(c['name'] or c['company'] or (c['emails'] or [''])[0])}",
           f"N:{_vesc(c['family'])};{_vesc(c['given'])};;;"]
    if c["company"] or c["title"]:
        out.append(f"ORG:{_vesc(c['company'])}")
        if c["title"]:
            out.append(f"TITLE:{_vesc(c['title'])}")
    out += [f"EMAIL;TYPE=INTERNET:{_vesc(e)}" for e in c["emails"]]
    tmap = {"mobile": "CELL", "work": "WORK", "home": "HOME"}
    out += [f"TEL;TYPE={tmap.get(p['type'], 'VOICE')}:{_vesc(p['value'])}" for p in c["phones"]]
    out += [f"ADR:;;{_vesc(a)};;;;" for a in c["addresses"]]
    if c["birthday"]:
        out.append(f"BDAY:{c['birthday']}")
    if c["notes"]:
        out.append(f"NOTE:{_vesc(c['notes'])}")
    if c["groups"]:
        out.append("CATEGORIES:" + ",".join(_vesc(g) for g in c["groups"]))
    out.append("END:VCARD")
    return "\r\n".join(out) + "\r\n"


def save(raw_ms, raw_google, contacts, extra=None):
    """Write one backup folder and return its name."""
    name = time.strftime(NAME_FMT)
    folder = os.path.join(store.BACKUP_DIR, name)
    os.makedirs(folder, exist_ok=True)
    os.chmod(folder, 0o700)
    for fname, data in (("microsoft.json.gz", raw_ms), ("google.json.gz", raw_google)):
        if data is not None:
            with gzip.open(os.path.join(folder, fname), "wt", encoding="utf-8") as f:
                json.dump(data, f)
    for side in ("microsoft", "google"):
        cs = [c for c in contacts if c["side"] == side]
        if cs:
            with open(os.path.join(folder, f"{side}.vcf"), "w", encoding="utf-8", newline="") as f:
                f.writelines(vcard(c) for c in cs)
    with open(os.path.join(folder, "info.json"), "w") as f:
        json.dump({"created": time.time(), **(extra or {})}, f)
    for fn in os.listdir(folder):
        os.chmod(os.path.join(folder, fn), 0o600)
    return name


def listing():
    out = []
    if not os.path.isdir(store.BACKUP_DIR):
        return out
    for name in sorted(os.listdir(store.BACKUP_DIR), reverse=True):
        folder = os.path.join(store.BACKUP_DIR, name)
        if not os.path.isdir(folder):
            continue
        files = sorted(os.listdir(folder))
        size = sum(os.path.getsize(os.path.join(folder, f)) for f in files)
        out.append({"name": name, "files": files, "size": size})
    return out


def path_of(name, fname):
    """Safe path to a backup file, or None."""
    if not name or not fname or "/" in name + fname or "\\" in name + fname or ".." in name + fname:
        return None
    p = os.path.join(store.BACKUP_DIR, name, fname)
    return p if os.path.isfile(p) else None


def prune(keep_days):
    cutoff = time.time() - keep_days * 86400
    for b in listing()[1:]:                     # never delete the newest one
        try:
            t = time.mktime(time.strptime(b["name"], NAME_FMT))
        except ValueError:
            continue
        if t < cutoff:
            shutil.rmtree(os.path.join(store.BACKUP_DIR, b["name"]), ignore_errors=True)
