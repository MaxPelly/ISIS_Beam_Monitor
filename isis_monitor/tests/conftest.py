import pytest

from isis_monitor import messages


@pytest.fixture(autouse=True)
def default_timezone():
    """Card times in the default timezone, whatever a test (or a config it loads) set."""
    yield
    messages.set_timezone("Europe/London")
