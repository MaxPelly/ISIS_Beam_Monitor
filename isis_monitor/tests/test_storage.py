from datetime import datetime, timedelta, timezone

from isis_monitor.storage import SQLiteStateStore


def test_storage_write_load_and_prune(tmp_path):
    db = tmp_path / "state.db"
    store = SQLiteStateStore(db)

    now = datetime.now(timezone.utc)
    old = now - timedelta(days=8)

    store.write_sample(old, "TS1", 1.0, "low")
    store.write_sample(now, "TS1", 2.0, "medium")
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


def test_storage_snapshot_and_health(tmp_path):
    db = tmp_path / "state.db"
    store = SQLiteStateStore(db)

    store.upsert_snapshot("daemon_state", '{"ok":true}')
    store.upsert_health("beam", "connected")
    store.commit()

    snap = store.load_snapshot("daemon_state")
    assert snap == '{"ok":true}'

    health = store.load_health()
    assert len(health) == 1
    assert health[0]["component"] == "beam"
    assert health[0]["status"] == "connected"
    store.close()
