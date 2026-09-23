import random

from isis_monitor.flavour import pick


def test_pick_is_deterministic_for_a_given_seed():
    assert pick(("*", "high"), random.Random(1)) == pick(("*", "high"), random.Random(1))


def test_pick_falls_back_to_wildcard_target():
    # "TS1" has no target-specific pool for "high" — falls back to ("*", "high").
    target_specific = pick(("TS1", "high"), random.Random(1))
    wildcard = pick(("*", "high"), random.Random(1))
    assert target_specific == wildcard


def test_pick_unknown_key_returns_empty_string():
    assert pick(("*", "no_such_transition"), random.Random(1)) == ""


def test_pick_only_returns_lines_from_the_matching_pool():
    from isis_monitor.flavour import _LINES

    rng = random.Random(0)
    for _ in range(20):
        line = pick(("*", "off"), rng)
        assert line in _LINES[("*", "off")]
