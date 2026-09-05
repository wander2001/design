"""Run-to-run state: what we already reported, and last-seen fund holdings.

Without this a daily job re-sends the same congressional filing every morning for
45 days, because disclosure feeds keep old rows around.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS seen (
    key        TEXT PRIMARY KEY,
    kind       TEXT NOT NULL,
    first_seen TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS seen_kind ON seen(kind);
CREATE TABLE IF NOT EXISTS snapshot (
    name       TEXT PRIMARY KEY,
    payload    TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


class State:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "State":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- dedup -----------------------------------------------------------
    def filter_new(self, keys: Iterable[str]) -> set[str]:
        """Return the subset of ``keys`` never recorded before (no write)."""
        keys = list(keys)
        if not keys:
            return set()
        found: set[str] = set()
        for chunk in (keys[i : i + 500] for i in range(0, len(keys), 500)):
            marks = ",".join("?" * len(chunk))
            with closing(self.conn.execute(f"SELECT key FROM seen WHERE key IN ({marks})", chunk)) as cur:
                found.update(row[0] for row in cur)
        return {k for k in keys if k not in found}

    def mark_seen(self, pairs: Iterable[tuple[str, str]]) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self.conn.executemany(
            "INSERT OR IGNORE INTO seen(key, kind, first_seen) VALUES (?,?,?)",
            [(key, kind, now) for key, kind in pairs],
        )
        self.conn.commit()

    def prune(self, keep_days: int = 400) -> int:
        cutoff = datetime.now(timezone.utc).timestamp() - keep_days * 86400
        cutoff_iso = datetime.fromtimestamp(cutoff, timezone.utc).isoformat()
        cur = self.conn.execute("DELETE FROM seen WHERE first_seen < ?", (cutoff_iso,))
        self.conn.commit()
        return cur.rowcount

    # -- snapshots -------------------------------------------------------
    def get_snapshot(self, name: str) -> Any:
        with closing(self.conn.execute("SELECT payload FROM snapshot WHERE name = ?", (name,))) as cur:
            row = cur.fetchone()
        return json.loads(row[0]) if row else None

    def put_snapshot(self, name: str, payload: Any) -> None:
        self.conn.execute(
            "INSERT INTO snapshot(name, payload, updated_at) VALUES (?,?,?) "
            "ON CONFLICT(name) DO UPDATE SET payload=excluded.payload, updated_at=excluded.updated_at",
            (name, json.dumps(payload, default=str), datetime.now(timezone.utc).isoformat()),
        )
        self.conn.commit()
