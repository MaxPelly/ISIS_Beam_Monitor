import logging
import pytest
from unittest.mock import MagicMock
from main import TUILogHandler


class TestTUILogHandler:
    def test_emit_calls_update_log(self):
        """TUILogHandler.emit should forward the formatted message to tui.update_log."""
        mock_tui = MagicMock()
        handler = TUILogHandler(mock_tui)
        handler.setFormatter(logging.Formatter("%(levelname)s - %(message)s"))

        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="", lineno=0,
            msg="hello world", args=(), exc_info=None,
        )
        handler.emit(record)

        mock_tui.update_log.assert_called_once_with("INFO - hello world")

    def test_emit_handles_exception_gracefully(self, caplog):
        """If tui.update_log raises, handleError should be called and not propagate."""
        mock_tui = MagicMock()
        mock_tui.update_log.side_effect = RuntimeError("TUI broken")
        handler = TUILogHandler(mock_tui)

        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="", lineno=0,
            msg="test", args=(), exc_info=None,
        )
        # Should not raise
        handler.emit(record)

    def test_emit_with_warning_level(self):
        """Formatter applied correctly for WARNING level messages."""
        mock_tui = MagicMock()
        handler = TUILogHandler(mock_tui)
        handler.setFormatter(logging.Formatter("%(levelname)s - %(message)s"))

        record = logging.LogRecord(
            name="test", level=logging.WARNING, pathname="", lineno=0,
            msg="something went wrong", args=(), exc_info=None,
        )
        handler.emit(record)
        mock_tui.update_log.assert_called_once_with("WARNING - something went wrong")
