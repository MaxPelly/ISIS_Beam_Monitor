"""Optional personality lines, shown only when `fun_mode` is enabled.

Line pools are keyed by transition or event, e.g. "off" or "new_run".
"""
import random
from typing import Dict, List

_LINES: Dict[str, List[str]] = {
    "off": [
        "Lights out for now.",
        "Nothing to see here — beam's taking five.",
        "Quiet on this line for the moment.",
    ],
    "low": [
        "Trickle mode engaged.",
        "Slow and steady.",
        "Taking it easy.",
    ],
    "medium": [
        "Cruising along nicely.",
        "Right in the middle of things.",
        "Steady progress.",
    ],
    "high": [
        "Full steam ahead!",
        "Running hot and happy.",
        "All cylinders firing.",
    ],
    "restored": [
        "Back from the dead — beam's alive again!",
        "The wait is over, beam is back.",
        "Order restored.",
    ],
    "new_run": [
        "New run, who dis?",
        "Fresh run just dropped.",
        "And we're off again.",
    ],
    "finishing": [
        "Nearly there — wrapping this run up.",
        "Home stretch for this run.",
        "Almost in the bag.",
    ],
    "mcr_news": [
        "Fresh off the MCR press.",
        "Word from the control room.",
        "Hot off the wire.",
    ],
    "startup": [
        "Reporting for duty.",
        "Monitor's awake and watching.",
        "On watch from here.",
    ],
    "milestone": [
        "Another one for the books.",
        "Racking them up nicely.",
        "The counter keeps climbing.",
    ],
}


def pick(key: str, rng: random.Random) -> str:
    """Pick a random line for `key`, or "" if there's no pool for it."""
    lines = _LINES.get(key)
    return rng.choice(lines) if lines else ""


# Shown once per day on the daily summary card, when fun_mode is enabled.
FACTS_OF_THE_DAY = [
    "Neutrons have no electric charge, which lets them pass through materials that block X-rays.",
    "A neutron still has a magnetic moment despite carrying no charge, which is what makes it useful for probing magnetism.",
    "Spallation neutron sources like ISIS fire high-energy protons at a heavy metal target to knock loose bursts of neutrons.",
    "ISIS is named after the stretch of the River Thames that runs through Oxford, itself named for the Egyptian goddess.",
    "Unlike a nuclear reactor, a spallation source only produces neutrons in short pulses, not a continuous stream.",
]


def fact_of_the_day(rng: random.Random) -> str:
    """Pick a random fact for the daily summary card."""
    return rng.choice(FACTS_OF_THE_DAY)
