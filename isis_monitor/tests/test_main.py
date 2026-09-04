import logging
import pytest
from unittest.mock import MagicMock
from main import StateLogHandler


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
