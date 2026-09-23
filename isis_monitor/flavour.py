"""Optional personality lines, shown only when `fun_mode` is enabled.

Line pools are keyed by (target, transition), where target is a beam
target's display name (e.g. "TS1") or "*" for target-agnostic events.
`pick()` falls back to the "*" pool when there's no target-specific one.
"""
import random
from typing import Dict, List, Tuple

Key = Tuple[str, str]

_LINES: Dict[Key, List[str]] = {
    ("*", "off"): [
        "Lights out for now.",
        "Nothing to see here — beam's taking five.",
        "Quiet on this line for the moment.",
    ],
    ("*", "low"): [
        "Trickle mode engaged.",
        "Slow and steady.",
        "Taking it easy.",
    ],
    ("*", "medium"): [
        "Cruising along nicely.",
        "Right in the middle of things.",
        "Steady progress.",
    ],
    ("*", "high"): [
        "Full steam ahead!",
        "Running hot and happy.",
        "All cylinders firing.",
    ],
    ("*", "restored"): [
        "Back from the dead — beam's alive again!",
        "The wait is over, beam is back.",
        "Order restored.",
    ],
    ("*", "new_run"): [
        "New run, who dis?",
        "Fresh run just dropped.",
        "And we're off again.",
    ],
    ("*", "finishing"): [
        "Nearly there — wrapping this run up.",
        "Home stretch for this run.",
        "Almost in the bag.",
    ],
    ("*", "mcr_news"): [
        "Fresh off the MCR press.",
        "Word from the control room.",
        "Hot off the wire.",
    ],
    ("*", "startup"): [
        "Reporting for duty.",
        "Monitor's awake and watching.",
        "On watch from here.",
    ],
}


def pick(key: Key, rng: random.Random) -> str:
    """Pick a random line for `key`, falling back to the "*" target pool."""
    target, transition = key
    lines = _LINES.get((target, transition)) or _LINES.get(("*", transition))
    if not lines:
        return ""
    return rng.choice(lines)
