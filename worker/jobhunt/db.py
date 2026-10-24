import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

# Outside the project folder: it syncs to Dropbox, and a sync client copying a live SQLite file corrupts it.
# Not under AppData either: Microsoft Store Python silently redirects writes there into its own package folder,
# which is wiped if Python is reset. Backups and the app's log sit next to it.
DB_PATH = os.environ.get("JOBHUNT_DB") or str(Path.home() / ".jobhunt" / "jobhunt.db")
SCHEMA = (Path(__file__).parent / "schema.sql").read_text()
_ready = set()


def connect(path=DB_PATH):
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)  # waits out the other process's write lock
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")  # API reads while the worker writes
    conn.execute("PRAGMA foreign_keys=ON")
    if path not in _ready:
        conn.executescript(SCHEMA)
        if path != ":memory:":
            _ready.add(path)
    return conn


def now(dt=None):
    """UTC in SQLite's datetime('now') format, so Python and SQL timestamps compare as strings."""
    return (dt or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
