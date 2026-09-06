import logging
import pytest
from unittest.mock import MagicMock
import os
import signal
import asyncio
import fcntl
from pathlib import Path
from isis_monitor.daemon_state import DaemonState
from isis_monitor.tui import RichTUI
from main import StateLogHandler, SingleInstanceLock, _apply_snapshot_to_tui


class TestStateLogHandler:
    def test_emit_calls_update_log(self):
        """StateLogHandler.emit should forward the formatted message."""
        mock_state = MagicMock()
        handler = StateLogHandler(mock_state)
        handler.setFormatter(logging.Formatter("%(levelname)s - %(message)s"))

        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="", lineno=0,
            msg="hello world", args=(), exc_info=None,
        )
        handler.emit(record)

        mock_state.update_log.assert_called_once_with("INFO - hello world")

    def test_emit_handles_exception_gracefully(self, caplog):
        """If state.update_log raises, handleError should be called and not propagate."""
        mock_state = MagicMock()
        mock_state.update_log.side_effect = RuntimeError("State broken")
        handler = StateLogHandler(mock_state)

        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="", lineno=0,
            msg="test", args=(), exc_info=None,
        )
        # Should not raise
        handler.emit(record)

    def test_emit_with_warning_level(self):
        """Formatter applied correctly for WARNING level messages."""
        mock_state = MagicMock()
        handler = StateLogHandler(mock_state)
        handler.setFormatter(logging.Formatter("%(levelname)s - %(message)s"))

        record = logging.LogRecord(
            name="test", level=logging.WARNING, pathname="", lineno=0,
            msg="something went wrong", args=(), exc_info=None,
        )
        handler.emit(record)
        mock_state.update_log.assert_called_once_with("WARNING - something went wrong")

def test_single_instance_lock_success(tmp_path):
    import os
    from main import SingleInstanceLock
    lock_file = tmp_path / "test.lock"
    with SingleInstanceLock(lock_file) as lock:
        assert lock_file.exists()
        assert lock_file.read_text().strip() == str(os.getpid())
    assert not lock_file.exists()

def test_single_instance_lock_failure(tmp_path):
    from main import SingleInstanceLock
    lock_file = tmp_path / "test.lock"
    lock_file.write_text(str(os.getpid()))
    blocker = lock_file.open("a+")
    fcntl.flock(blocker.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        with pytest.raises(RuntimeError, match="Lock file already held|Lock held by"):
            with SingleInstanceLock(lock_file):
                pass
    finally:
        fcntl.flock(blocker.fileno(), fcntl.LOCK_UN)
        blocker.close()

def test_apply_snapshot_to_tui():
    from main import _apply_snapshot_to_tui
    from isis_monitor.tui import RichTUI
    tui = RichTUI(60, 60, 4, 50)
    snap = {
        "beam_states": {
            "TS1": {"current": 42.0, "power": "high"}
        },
        "mcr_news": "Test news"
    }
    _apply_snapshot_to_tui(tui, snap)
    assert tui.mcr_news == "Test news"
    assert "TS1" in tui.beam_states
    assert tui.beam_states["TS1"]["current"] == 42.0
