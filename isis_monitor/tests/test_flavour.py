import random

from isis_monitor.flavour import _LINES, pick


def test_pick_unknown_key_returns_empty_string():
    assert pick("no_such_transition", random.Random(1)) == ""


def test_pick_only_returns_lines_from_the_matching_pool():
    rng = random.Random(0)
    for _ in range(20):
        assert pick("off", rng) in _LINES["off"]
