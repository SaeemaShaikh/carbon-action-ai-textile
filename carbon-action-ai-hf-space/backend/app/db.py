"""SQLite store so readings, alerts, decisions and the plant state survive a restart.

Standard library only. Callers hold Engine.lock, so one shared connection is safe.
The simulator state is a pickle in our own local data folder; a real meter feed
would not need it.
"""
from __future__ import annotations

import json
import pickle
import sqlite3
from datetime import datetime
from pathlib import Path

SCHEMA_VERSION = 1


def _default(o):
    if hasattr(o, "isoformat"):
        return o.isoformat()
    if hasattr(o, "item"):
        return o.item()
    return str(o)


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS readings(
                factory TEXT, machine TEXT, ts TEXT, data TEXT, PRIMARY KEY(factory, machine, ts));
            CREATE TABLE IF NOT EXISTS records(
                kind TEXT, factory TEXT, id TEXT, seq INTEGER, data TEXT, PRIMARY KEY(kind, factory, id));
            CREATE TABLE IF NOT EXISTS state(factory TEXT PRIMARY KEY, version INTEGER, saved_at TEXT, blob BLOB);
        """)
        self.db.commit()

    # readings ---------------------------------------------------------
    def save_rows(self, fid: str, rows: list[dict]):
        self.db.executemany(
            "INSERT OR REPLACE INTO readings VALUES (?,?,?,?)",
            [(fid, r["machine_id"], r["timestamp"].isoformat() if hasattr(r["timestamp"], "isoformat")
              else str(r["timestamp"]), json.dumps(r, default=_default)) for r in rows])

    def load_rows(self, fid: str, since: datetime | None = None) -> list[dict]:
        q, args = "SELECT data FROM readings WHERE factory=?", [fid]
        if since is not None:
            q += " AND ts>=?"
            args.append(since.isoformat())
        return [json.loads(d) for (d,) in self.db.execute(q + " ORDER BY ts, machine", args)]

    def prune(self, fid: str, before: datetime):
        self.db.execute("DELETE FROM readings WHERE factory=? AND ts<?", (fid, before.isoformat()))

    # alerts & actions -------------------------------------------------
    def save_records(self, kind: str, fid: str, items: list[dict]):
        # seq = position in the list, so history reloads in its original order
        rows = [(kind, fid, it["id"], it.get("_seq", i), json.dumps(it, default=_default)) for i, it in enumerate(items)]
        self.db.executemany("INSERT OR REPLACE INTO records VALUES (?,?,?,?,?)", rows)

    def load_records(self, kind: str, fid: str) -> list[dict]:
        return [json.loads(d) for (d,) in self.db.execute(
            "SELECT data FROM records WHERE kind=? AND factory=? ORDER BY seq", (kind, fid))]

    # simulator state --------------------------------------------------
    def save_state(self, fid: str, state: dict):
        self.db.execute("INSERT OR REPLACE INTO state VALUES (?,?,?,?)",
                        (fid, SCHEMA_VERSION, datetime.now().isoformat(timespec="seconds"), pickle.dumps(state)))

    def load_state(self, fid: str) -> dict | None:
        row = self.db.execute("SELECT version, blob FROM state WHERE factory=?", (fid,)).fetchone()
        if not row or row[0] != SCHEMA_VERSION:
            return None
        try:
            return pickle.loads(row[1])
        except Exception as e:  # saved by an older code version: start fresh rather than crash
            print("Could not restore saved plant state:", repr(e))
            return None

    def clear_factory(self, fid: str):
        for table, col in (("readings", "factory"), ("records", "factory"), ("state", "factory")):
            self.db.execute(f"DELETE FROM {table} WHERE {col}=?", (fid,))
        self.db.commit()

    def commit(self):
        self.db.commit()

    def close(self):
        self.db.close()
