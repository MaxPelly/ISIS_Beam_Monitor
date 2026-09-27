from __future__ import annotations

import asyncio
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Tuple


class SQLiteStateStore:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        # Lets an external reader (e.g. the sqlite3 CLI) holding a lock delay
        # our writes briefly instead of failing with "database is locked".
        self.conn.execute("PRAGMA busy_timeout = 5000")
        self._init_schema()
        # One connection is shared by every caller, and concurrent use of a
        # sqlite3 connection from several threads corrupts its state, so all
        # async callers go through run(), which uses this single thread.
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sqlite")

    async def run(self, fn: Callable[..., Any], *args: Any) -> Any:
        """Run fn(*args) off the event loop on the store's one worker thread."""
        return await asyncio.get_running_loop().run_in_executor(self._executor, fn, *args)

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
            """
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()
        self._executor.shutdown(wait=False)  # may be called from its own thread

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

    def commit(self) -> None:
        self.conn.commit()
