import copy
from unittest.mock import AsyncMock

import pytest

from isis_monitor.config_editor import describe_changes, edit_settings, render_menu, run_config_editor

SETTINGS = {
    "notifications": {
        "fun_mode": "false", "timezone": "Europe/London", "debounce_seconds": "20",
        "stall_minutes": "15", "summary_time": "08:00",
    },
    "instruments": [
        {"name": "PEARL", "counts_pv": "IN:PEARL:COUNTS", "notify_counts": "130", "beam_target": "TS1"},
        {"name": "WISH", "counts_pv": "IN:WISH:COUNTS", "notify_counts": "50", "beam_target": "TS2"},
    ],
}
TARGETS = ["TS1", "TS2", "Muon"]


class Terminal:
    """Feeds scripted lines to read_line and records everything written."""

    def __init__(self, *lines):
        self.lines = list(lines)
        self.prompts = []
        self.output = []

    async def read_line(self, prompt):
        self.prompts.append(prompt)
        return self.lines.pop(0) if self.lines else None  # None = input closed

    def write(self, text):
        self.output.append(text)

    @property
    def text(self):
        return "\n".join(self.output)


async def edit(*lines):
    term = Terminal(*lines)
    result = await edit_settings(copy.deepcopy(SETTINGS), SETTINGS, TARGETS, term.read_line, term.write)
    return result, term


def test_render_menu_numbers_settings_then_instruments():
    menu = render_menu(SETTINGS)
    assert " 1) fun_mode" in menu and " 5) summary_time" in menu
    assert " 6) PEARL    notify at 130 on TS1  PV IN:PEARL:COUNTS" in menu
    assert " 7) WISH" in menu


@pytest.mark.asyncio
async def test_edit_notification_and_save():
    result, term = await edit("1", "true", "s", "y")
    assert result["notifications"]["fun_mode"] == "true"
    assert "fun_mode: false → true" in term.text
    assert "fun_mode [false]: " in term.prompts


@pytest.mark.asyncio
async def test_enter_keeps_current_value():
    result, term = await edit("4", "", "s", "q")
    assert "No changes to save." in term.text
    assert result is None


@pytest.mark.asyncio
async def test_edit_instrument_reprompts_for_invalid_beam_target():
    result, term = await edit("7", "", "", "75", "Muons", "Muon", "s", "y")
    assert result["instruments"][1] == {
        "name": "WISH", "counts_pv": "IN:WISH:COUNTS", "notify_counts": "75", "beam_target": "Muon",
    }
    assert "Beam target must be one of TS1, TS2, Muon." in term.text


@pytest.mark.asyncio
async def test_add_instrument_upper_cases_name():
    result, term = await edit("a", "emu", "IN:EMU:COUNTS", "10", "Muon", "s", "y")
    assert result["instruments"][-1] == {
        "name": "EMU", "counts_pv": "IN:EMU:COUNTS", "notify_counts": "10", "beam_target": "Muon",
    }
    assert "+ EMU (counts_pv=IN:EMU:COUNTS, notify_counts=10, beam_target=Muon)" in term.text


@pytest.mark.asyncio
async def test_delete_instrument_needs_confirmation():
    result, term = await edit("d 6", "n", "d 6", "y", "s", "y")
    assert [i["name"] for i in result["instruments"]] == ["WISH"]
    assert "  - PEARL" in term.text


@pytest.mark.asyncio
async def test_cannot_delete_last_instrument_or_a_missing_one():
    result, term = await edit("d 6", "y", "d 6", "d 9", "q")
    assert "At least one instrument is required." in term.text
    assert "No instrument with that number." in term.text


@pytest.mark.asyncio
async def test_quit_with_changes_asks_before_discarding():
    result, term = await edit("1", "true", "q", "n", "q", "y")
    assert result is None
    assert term.prompts.count("Discard your changes? [y/N]: ") == 2


@pytest.mark.asyncio
async def test_declining_save_returns_to_menu():
    result, term = await edit("1", "true", "s", "n", "q", "y")
    assert result is None


@pytest.mark.asyncio
async def test_unknown_command_and_closed_input():
    result, term = await edit("zz")
    assert "Unknown command: zz" in term.text
    assert result is None  # input closed after "zz"


def test_describe_changes_covers_renames_and_order():
    edited = copy.deepcopy(SETTINGS)
    edited["instruments"].reverse()
    assert describe_changes(SETTINGS, edited) == ["  instrument order changed"]
    edited["instruments"][0]["name"] = "MERLIN"
    changes = describe_changes(SETTINGS, edited)
    assert any(c.startswith("  + MERLIN") for c in changes) and "  - WISH" in changes


# ---------------------------------------------------------------------------
# run_config_editor
# ---------------------------------------------------------------------------

def daemon(*update_replies):
    replies = [{"ok": True, "config": copy.deepcopy(SETTINGS), "revision": "rev1", "beam_targets": TARGETS},
               *update_replies]
    return AsyncMock(side_effect=replies)


@pytest.mark.asyncio
async def test_run_config_editor_saves_with_revision():
    request = daemon({"ok": True, "restarting": True})
    term = Terminal("1", "true", "s", "y", "")
    await run_config_editor(request, term.read_line, term.write)
    update = request.await_args_list[1].args[0]
    assert (update["method"], update["revision"]) == ("update_config", "rev1")
    assert update["settings"]["notifications"]["fun_mode"] == "true"
    assert "daemon is restarting" in term.text


@pytest.mark.asyncio
async def test_run_config_editor_lets_user_fix_invalid_config():
    request = daemon(
        {"ok": False, "error": "invalid_config", "detail": "[INSTRUMENT:WISH] notify_counts must be a positive number"},
        {"ok": True, "restarting": True},
    )
    term = Terminal("7", "", "", "0", "", "s", "y",   # rejected by the daemon
                    "7", "", "", "5", "", "s", "y",   # fixed; earlier edit still in place
                    "")
    await run_config_editor(request, term.read_line, term.write)
    assert "Save failed (invalid_config): [INSTRUMENT:WISH] notify_counts" in term.text
    assert request.await_args_list[2].args[0]["settings"]["instruments"][1]["notify_counts"] == "5"


@pytest.mark.asyncio
async def test_run_config_editor_stops_on_config_changed():
    request = daemon({"ok": False, "error": "config_changed", "detail": "reload and try again"})
    term = Terminal("1", "true", "s", "y", "")
    await run_config_editor(request, term.read_line, term.write)
    assert "Save failed (config_changed): reload and try again" in term.text
    assert request.await_count == 2


@pytest.mark.asyncio
async def test_run_config_editor_quit_sends_nothing():
    request = daemon()
    await run_config_editor(request, Terminal("q").read_line, lambda _text: None)
    assert request.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("request_mock, message", [
    (AsyncMock(side_effect=ConnectionError("gone")), "Could not reach the daemon: gone"),
    (AsyncMock(return_value={"ok": False, "error": "invalid_config", "detail": "bad file"}),
     "Could not load the config: bad file"),
])
async def test_run_config_editor_reports_load_failures(request_mock, message):
    term = Terminal("")
    await run_config_editor(request_mock, term.read_line, term.write)
    assert message in term.text
    assert term.prompts == ["Press Enter to return to the monitor..."]


@pytest.mark.asyncio
async def test_run_config_editor_reports_connection_lost_on_save():
    request = AsyncMock(side_effect=[
        {"ok": True, "config": copy.deepcopy(SETTINGS), "revision": "r", "beam_targets": TARGETS},
        ConnectionError("daemon went away"),
    ])
    term = Terminal("1", "true", "s", "y", "")
    await run_config_editor(request, term.read_line, term.write)
    assert "Save failed: daemon went away" in term.text
