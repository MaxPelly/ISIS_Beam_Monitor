import copy
from unittest.mock import AsyncMock

import pytest

from isis_monitor.config_editor import describe_changes, edit_settings, render_menu, run_config_editor

SETTINGS = {
    "notifications": {
        "timezone": "Europe/London", "debounce_seconds": "20",
        "stall_minutes": "15", "finish_warning_minutes": "15", "summary_time": "08:00",
    },
    "instruments": [
        {"name": "PEARL", "notify_counts": "130", "beam_target": "TS1", "channel": "experiment"},
        {"name": "WISH", "notify_counts": "50", "beam_target": "TS2", "channel": "experiment"},
    ],
}
TARGETS = ["TS1", "TS2", "Muons"]
MODES = ["experiment", "instrument"]


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
    result = await edit_settings(copy.deepcopy(SETTINGS), SETTINGS, TARGETS, term.read_line, term.write, MODES)
    return result, term


def test_render_menu_numbers_settings_then_instruments():
    menu = render_menu(SETTINGS)
    assert " 1) timezone" in menu and " 5) summary_time" in menu
    assert " 6) PEARL    notify at 130 µA·h on TS1 · Teams channel: Experiment Updates" in menu
    assert " 7) WISH" in menu


async def test_edit_notification_and_save():
    result, term = await edit("1", "UTC", "s", "y")
    assert result["notifications"]["timezone"] == "UTC"
    assert "timezone: Europe/London → UTC" in term.text
    assert "timezone [Europe/London]: " in term.prompts


async def test_enter_and_a_blank_name_change_nothing():
    """Enter keeps the current value; a blank name cancels adding an instrument."""
    result, term = await edit("4", "", "a", "", "s", "q")
    assert "No changes to save." in term.text
    assert "Adding an instrument (leave the name blank to cancel)." in term.text
    assert term.prompts.count("Name []: ") == 1
    assert result is None


async def test_edit_instrument_reprompts_for_invalid_beam_target():
    """Beam targets are case-insensitive and stored canonically."""
    result, term = await edit("7", "", "75", "Muon2", "muons", "", "s", "y")
    assert result["instruments"][1] == {
        "name": "WISH", "notify_counts": "75", "beam_target": "Muons", "channel": "experiment",
    }
    assert "Beam target must be one of TS1, TS2, Muons, auto." in term.text


async def test_auto_beam_target_is_stored_blank():
    """"auto" leaves beam_target unset, so it comes from CS:INSTLIST."""
    result, term = await edit("7", "", "", "AUTO", "", "s", "y")
    assert result["instruments"][1]["beam_target"] == ""
    assert "Beam target (TS1/TS2/Muons/auto) [TS2]: " in term.prompts
    assert " 7) WISH     notify at 50 µA·h on (auto)" in render_menu(result)


async def test_add_instrument_upper_cases_name():
    result, term = await edit("a", "emu", "10", "Muons", "", "s", "y")
    assert result["instruments"][-1] == {
        "name": "EMU", "notify_counts": "10", "beam_target": "Muons", "channel": "experiment",
    }
    assert "+ EMU (notify_counts=10, beam_target=Muons, channel=experiment)" in term.text


async def test_delete_instrument_needs_confirmation():
    result, term = await edit("d 6", "n", "d 6", "y", "s", "y")
    assert [i["name"] for i in result["instruments"]] == ["WISH"]
    assert "  - PEARL" in term.text


async def test_cannot_delete_last_instrument_or_a_missing_one():
    result, term = await edit("d 6", "y", "d 6", "d 10")
    assert "At least one instrument is required." in term.text
    assert "No instrument with that number." in term.text
    # Input then closes with the deletion unsaved.
    assert result is None
    assert "Input closed; your changes were discarded." in term.text


async def test_declining_save_or_discard_returns_to_menu():
    result, term = await edit("1", "UTC", "s", "n", "q", "n", "q", "y")
    assert result is None
    assert term.prompts.count("Discard your changes? [y/N]: ") == 2


async def test_unknown_command_and_closed_input():
    result, term = await edit("zz", "²", "d ²")  # superscript digits aren't numbers here
    assert "Unknown command: zz" in term.text
    assert "Unknown command: ²" in term.text and "Unknown command: d ²" in term.text
    assert result is None  # input closed afterwards
    assert "discarded" not in term.text  # nothing had changed


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
    replies = [{"ok": True, "config": copy.deepcopy(SETTINGS), "revision": "rev1", "beam_targets": TARGETS,
                "channel_modes": MODES},
               *update_replies]
    return AsyncMock(side_effect=replies)


@pytest.mark.parametrize("reply, message", [
    ({"ok": True, "restarting": True}, "daemon is restarting"),
    # A config changed elsewhere ends the session rather than overwriting it.
    ({"ok": False, "error": "config_changed", "detail": "reload and try again"},
     "Save failed (config_changed): reload and try again"),
])
async def test_run_config_editor_saves_with_revision(reply, message):
    request = daemon(reply)
    term = Terminal("1", "UTC", "s", "y", "")
    await run_config_editor(request, term.read_line, term.write)
    update = request.await_args_list[1].args[0]
    assert (update["method"], update["revision"]) == ("update_config", "rev1")
    assert update["settings"]["notifications"]["timezone"] == "UTC"
    assert message in term.text
    assert request.await_count == 2


async def test_run_config_editor_lets_user_fix_invalid_config():
    request = daemon(
        {"ok": False, "error": "invalid_config", "detail": "[INSTRUMENT:WISH] notify_counts must be a positive number"},
        {"ok": True, "restarting": True},
    )
    term = Terminal("7", "", "0", "", "", "s", "y",   # rejected by the daemon
                    "7", "", "5", "", "", "s", "y",   # fixed; earlier edit still in place
                    "")
    await run_config_editor(request, term.read_line, term.write)
    assert "Save failed (invalid_config): [INSTRUMENT:WISH] notify_counts" in term.text
    assert request.await_args_list[2].args[0]["settings"]["instruments"][1]["notify_counts"] == "5"


async def test_run_config_editor_quit_sends_nothing():
    request = daemon()
    await run_config_editor(request, Terminal("q").read_line, lambda _text: None)
    assert request.await_count == 1


@pytest.mark.parametrize("request_mock, message", [
    (AsyncMock(side_effect=ConnectionError("gone")), "Could not reach the daemon: gone"),
    (AsyncMock(return_value={"ok": False, "error": "invalid_config", "detail": "bad file"}),
     "Could not load the config: bad file"),
])
async def test_run_config_editor_reports_load_failures(request_mock, message):
    term = Terminal()
    assert await run_config_editor(request_mock, term.read_line, term.write) is False
    assert message in term.text
    assert term.prompts == []


async def test_run_config_editor_keeps_edits_when_connection_is_lost_on_save():
    request = AsyncMock(side_effect=[
        {"ok": True, "config": copy.deepcopy(SETTINGS), "revision": "r", "beam_targets": TARGETS,
         "channel_modes": MODES},
        ConnectionError("daemon went away"),
        {"ok": True, "restarting": True},
    ])
    term = Terminal("1", "UTC", "s", "y",  # fails: connection lost
                    "s", "y")               # saved again once the daemon is back
    await run_config_editor(request, term.read_line, term.write)
    assert "Save failed: daemon went away. Your changes are kept" in term.text
    retry = request.await_args_list[2].args[0]
    assert (retry["revision"], retry["settings"]["notifications"]["timezone"]) == ("r", "UTC")


async def test_teams_channel_prompt_validates_and_shows_in_menu():
    result, term = await edit("6", "", "", "", "teams", "Instrument", "s", "y")
    assert result["instruments"][0]["channel"] == "instrument"
    assert "Teams channel must be one of experiment, instrument." in term.text
    assert "Teams channel (experiment/instrument) [experiment]: " in term.prompts
    assert "PEARL channel: experiment → instrument" in term.text
    assert "Teams channel: PEARL" in render_menu(result)
