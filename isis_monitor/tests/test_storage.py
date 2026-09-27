from datetime import datetime, timedelta, timezone

from isis_monitor.storage import SQLiteStateStore


def test_storage_write_load_and_prune(tmp_path):
    db = tmp_path / "state.db"
    store = SQLiteStateStore(db)

    now = datetime.now(timezone.utc)
    old = now - timedelta(days=8)

    store.write_samples([(old, "TS1", 1.0, "low"), (now, "TS1", 2.0, "medium")])
    store.commit()

    rows = store.load_recent_samples(now - timedelta(days=7))
    assert len(rows) == 1
    assert rows[0]["current"] == 2.0

    deleted = store.prune_older_than(now - timedelta(days=7))
    store.commit()
    assert deleted == 1

    rows2 = store.load_recent_samples(now - timedelta(days=30))
    assert len(rows2) == 1
    store.close()


def test_storage_snapshot_upsert_and_reopen(tmp_path):
    db = tmp_path / "state.db"
    store = SQLiteStateStore(db)
    assert store.load_snapshot("daemon_state") is None

    store.upsert_snapshot("daemon_state", '{"v":1}')
    store.upsert_snapshot("daemon_state", '{"v":2}')
    store.commit()
    store.close()

    reopened = SQLiteStateStore(db)
    assert reopened.load_snapshot("daemon_state") == '{"v":2}'
    reopened.close()


def test_storage_sets_busy_timeout(tmp_path):
    """An external reader holding a lock should delay writes, not fail them."""
    db = tmp_path / "state.db"
    store = SQLiteStateStore(db)
    (timeout_ms,) = store.conn.execute("PRAGMA busy_timeout").fetchone()
    assert timeout_ms > 0
    store.close()



async def test_run_serialises_database_work_on_one_thread(tmp_path):
    """The persistence and summary loops share one connection; concurrent use
    from two threads raised InterfaceError/OperationalError."""
    import asyncio
    import threading
    store = SQLiteStateStore(tmp_path / "s.db")
    threads = set()
    active = 0
    overlapped = False

    def work(i):
        nonlocal active, overlapped
        active += 1
        overlapped |= active > 1
        threads.add(threading.get_ident())
        store.write_samples([(datetime.now(timezone.utc), "TS1", float(i), "high")])
        store.upsert_snapshot("k", str(i))
        store.commit()
        active -= 1

    await asyncio.gather(*(store.run(work, i) for i in range(50)))
    await store.run(store.close)
    assert len(threads) == 1 and not overlapped
