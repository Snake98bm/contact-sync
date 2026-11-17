"""Paths, settings.json, the token file and the run history (SQLite)."""
import json
import os
import sqlite3
import threading

DATA_DIR = os.environ.get("DATA_DIR", "/data")
DB_PATH = os.path.join(DATA_DIR, "contact-sync.db")
SETTINGS_PATH = os.path.join(DATA_DIR, "settings.json")
TOKENS_PATH = os.path.join(DATA_DIR, "tokens.json")
BACKUP_DIR = os.path.join(DATA_DIR, "backups")

DEFAULT_SETTINGS = {
    "backup_enabled": True,     # nightly backup + fresh report
    "backup_time": "02:30",     # local time (TZ)
    "keep_days": 30,            # backups older than this are deleted
    "default_region": "US",     # country code assumed for phone numbers without one
    "write_mode": "test",       # off / test (only "Contact-Sync Test..." contacts) / on
    "sync_enabled": False,      # set by "Start syncing" on the Sync page
    "sync_minutes": 60,         # hourly
    "sync_max_deletes": 10,     # more deletes than this in one run wait for your OK
}

_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs(
  id INTEGER PRIMARY KEY, started REAL, finished REAL, trigger TEXT,
  ms_count INTEGER, google_count INTEGER, folder TEXT, error TEXT);
"""


def connect():
    os.makedirs(DATA_DIR, exist_ok=True)
    db = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    db.row_factory = sqlite3.Row
    return db


def init():
    with _lock, connect() as db:
        db.executescript(SCHEMA)


def q(sql, args=()):
    with _lock, connect() as db:
        return [dict(r) for r in db.execute(sql, args).fetchall()]


def x(sql, args=()):
    with _lock, connect() as db:
        return db.execute(sql, args).lastrowid


def _write_private(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def load_settings():
    s = dict(DEFAULT_SETTINGS)
    try:
        with open(SETTINGS_PATH) as f:
            s.update({k: v for k, v in json.load(f).items() if k in DEFAULT_SETTINGS})
    except (OSError, ValueError):
        pass
    return s


def save_settings(s):
    _write_private(SETTINGS_PATH, s)


def load_tokens():
    try:
        with open(TOKENS_PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_token(provider, data):
    """provider = 'microsoft' or 'google'; data = token dict (None removes it)."""
    with _lock:
        t = load_tokens()
        if data is None:
            t.pop(provider, None)
        else:
            t[provider] = data
        _write_private(TOKENS_PATH, t)
