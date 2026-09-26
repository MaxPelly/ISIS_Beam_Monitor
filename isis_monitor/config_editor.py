"""Line-based config editor the TUI drops into when `c` is pressed.

Works on the settings dict from the daemon's get_config (see
config.editable_settings) and sends the result back with update_config.
Terminal I/O is injected — `read_line(prompt)` returns the line typed, or
None if input was closed — so the editing logic can be tested without one.
"""
import copy
from typing import Awaitable, Callable, List, Optional

ReadLine = Callable[[str], Awaitable[Optional[str]]]
Write = Callable[[str], None]
Request = Callable[[dict], Awaitable[dict]]

_INSTRUMENT_FIELDS = ("name", "counts_pv", "notify_counts", "beam_target")


class _InputClosed(Exception):
    pass


async def _ask(read_line: ReadLine, prompt: str) -> str:
    line = await read_line(prompt)
    if line is None:
        raise _InputClosed
    return line.strip()


async def _ask_default(read_line: ReadLine, label: str, current: str) -> str:
    """Prompt for a value, keeping `current` if Enter is pressed."""
    answer = await _ask(read_line, f"{label} [{current}]: ")
    return answer or current


async def _confirm(read_line: ReadLine, question: str) -> bool:
    return (await _ask(read_line, f"{question} [y/N]: ")).lower() in ("y", "yes")


def render_menu(settings: dict) -> str:
    notifications = settings["notifications"]
    lines = ["", "=== Configuration ===", "Notifications:"]
    width = max(len(key) for key in notifications)
    for i, (key, value) in enumerate(notifications.items(), start=1):
        lines.append(f"  {i:>2}) {key:<{width}}  {value}")
    lines.append("Instruments:")
    offset = len(notifications) + 1
    for i, inst in enumerate(settings["instruments"], start=offset):
        lines.append(
            f"  {i:>2}) {inst.get('name', ''):<8} notify at {inst.get('notify_counts', '')}"
            f" on {inst.get('beam_target', '') or '(default)'}  PV {inst.get('counts_pv', '')}"
        )
    lines.append(
        "Commands: <number> edit · a add instrument · d <number> delete instrument"
        " · s save and restart daemon · q quit without saving"
    )
    return "\n".join(lines)


def describe_changes(original: dict, edited: dict) -> List[str]:
    """Human-readable differences between two settings dicts."""
    changes = []
    for key, value in edited["notifications"].items():
        before = original["notifications"].get(key)
        if value != before:
            changes.append(f"  {key}: {before} → {value}")

    before_by_name = {i.get("name", "").upper(): i for i in original["instruments"]}
    after_by_name = {i.get("name", "").upper(): i for i in edited["instruments"]}
    for name, inst in after_by_name.items():
        old = before_by_name.get(name)
        if old is None:
            fields = ", ".join(f"{f}={inst.get(f, '')}" for f in _INSTRUMENT_FIELDS[1:])
            changes.append(f"  + {name} ({fields})")
            continue
        for f in _INSTRUMENT_FIELDS[1:]:
            if inst.get(f, "") != old.get(f, ""):
                changes.append(f"  {name} {f}: {old.get(f, '')} → {inst.get(f, '')}")
    changes.extend(f"  - {name}" for name in before_by_name if name not in after_by_name)
    if not changes and [i.get("name", "").upper() for i in original["instruments"]] != list(after_by_name):
        changes.append("  instrument order changed")
    return changes


async def _edit_instrument(inst: dict, beam_targets: List[str], read_line: ReadLine, write: Write) -> dict:
    edited = dict(inst)
    edited["name"] = (await _ask_default(read_line, "Name", inst.get("name", ""))).upper()
    edited["counts_pv"] = await _ask_default(read_line, "Counts PV", inst.get("counts_pv", ""))
    edited["notify_counts"] = await _ask_default(read_line, "Notify at counts", inst.get("notify_counts", ""))
    while True:
        target = await _ask_default(
            read_line, f"Beam target ({'/'.join(beam_targets)})", inst.get("beam_target", "")
        )
        if not beam_targets or target in beam_targets:
            edited["beam_target"] = target
            return edited
        write(f"Beam target must be one of {', '.join(beam_targets)}.")


async def edit_settings(
    settings: dict,
    original: dict,
    beam_targets: List[str],
    read_line: ReadLine,
    write: Write,
) -> Optional[dict]:
    """Let the user edit `settings`. Returns the edited settings once they
    choose to save (and something differs from `original`, the file's
    current settings), or None if they quit."""
    current = copy.deepcopy(settings)
    keys = list(current["notifications"])
    try:
        while True:
            write(render_menu(current))
            command = await _ask(read_line, "> ")
            instruments = current["instruments"]
            if command.isdigit() and 1 <= int(command) <= len(keys):
                key = keys[int(command) - 1]
                current["notifications"][key] = await _ask_default(read_line, key, current["notifications"][key])
            elif command.isdigit() and 0 <= int(command) - len(keys) - 1 < len(instruments):
                index = int(command) - len(keys) - 1
                instruments[index] = await _edit_instrument(instruments[index], beam_targets, read_line, write)
            elif command.lower() == "a":
                blank = {"name": "", "counts_pv": "", "notify_counts": "", "beam_target": ""}
                instruments.append(await _edit_instrument(blank, beam_targets, read_line, write))
            elif command.lower().startswith("d") and command[1:].strip().isdigit():
                index = int(command[1:].strip()) - len(keys) - 1
                if not 0 <= index < len(instruments):
                    write("No instrument with that number.")
                elif len(instruments) == 1:
                    write("At least one instrument is required.")
                elif await _confirm(read_line, f"Delete {instruments[index].get('name', '')}?"):
                    del instruments[index]
            elif command.lower() == "s":
                changes = describe_changes(original, current)
                if not changes:
                    write("No changes to save.")
                    continue
                write("Changes:\n" + "\n".join(changes))
                if await _confirm(read_line, "Save and restart the daemon?"):
                    return current
            elif command.lower() == "q":
                if not describe_changes(original, current) or await _confirm(read_line, "Discard your changes?"):
                    return None
            elif command:
                write(f"Unknown command: {command}")
    except _InputClosed:
        return None


async def run_config_editor(request: Request, read_line: ReadLine, write: Write) -> None:
    """Fetch the config from the daemon, let the user edit it, and save it."""

    async def pause() -> None:
        await read_line("Press Enter to return to the monitor...")

    try:
        reply = await request({"method": "get_config"})
    except (OSError, ValueError, RuntimeError) as exc:
        write(f"Could not reach the daemon: {exc}")
        await pause()
        return
    if not reply.get("ok") or "config" not in reply:
        write(f"Could not load the config: {reply.get('detail') or reply.get('error', 'unknown error')}")
        await pause()
        return

    original = reply["config"]
    settings = original
    while True:
        edited = await edit_settings(settings, original, reply.get("beam_targets", []), read_line, write)
        if edited is None:
            return
        try:
            result = await request({"method": "update_config", "revision": reply.get("revision"), "settings": edited})
        except (OSError, ValueError, RuntimeError) as exc:
            write(f"Save failed: {exc}")
            await pause()
            return
        if result.get("ok"):
            write("Saved. The daemon is restarting; the monitor will reconnect.")
            await pause()
            return
        write(f"Save failed ({result.get('error')}): {result.get('detail', '')}")
        if result.get("error") != "invalid_config":
            await pause()
            return
        settings = edited  # let the user fix the rejected value and try again
