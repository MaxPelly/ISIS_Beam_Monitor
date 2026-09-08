from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional, Tuple


class SQLiteStateStore:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS beam_samples (
                timestamp TEXT NOT NULL,
                target TEXT NOT NULL,
                current REAL NOT NULL,
                power TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_beam_samples_time
            ON beam_samples(timestamp);

            CREATE TABLE IF NOT EXISTS snapshot (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS health (
                component TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def write_sample(self, timestamp: datetime, target: str, current: float, power: str) -> None:
        self.conn.execute(
            "INSERT INTO beam_samples(timestamp, target, current, power) VALUES (?, ?, ?, ?)",
            (timestamp.isoformat(), target, current, power),
        )

    def write_samples(self, rows: Iterable[Tuple[datetime, str, float, str]]) -> None:
        self.conn.executemany(
            "INSERT INTO beam_samples(timestamp, target, current, power) VALUES (?, ?, ?, ?)",
            [(ts.isoformat(), target, current, power) for ts, target, current, power in rows],
        )

    def prune_older_than(self, cutoff: datetime) -> int:
        cur = self.conn.execute(
            "DELETE FROM beam_samples WHERE timestamp < ?",
            (cutoff.isoformat(),),
        )
        return cur.rowcount

    def load_recent_samples(self, since: datetime) -> list[sqlite3.Row]:
        cur = self.conn.execute(
            """
            SELECT timestamp, target, current, power
            FROM beam_samples
            WHERE timestamp >= ?
            ORDER BY timestamp ASC
            """,
            (since.isoformat(),),
        )
        return list(cur.fetchall())

    def upsert_snapshot(self, key: str, value: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            """
            INSERT INTO snapshot(key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
            """,
            (key, value, now),
        )

    def load_snapshot(self, key: str) -> Optional[str]:
        cur = self.conn.execute("SELECT value FROM snapshot WHERE key = ?", (key,))
        row = cur.fetchone()
        return row[0] if row else None

    def upsert_health(self, component: str, status: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            """
            INSERT INTO health(component, status, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(component) DO UPDATE SET status=excluded.status, updated_at=excluded.updated_at
            """,
            (component, status, now),
        )

    def load_health(self) -> list[sqlite3.Row]:
        cur = self.conn.execute("SELECT component, status FROM health")
        return list(cur.fetchall())

    def commit(self) -> None:
        self.conn.commit()
