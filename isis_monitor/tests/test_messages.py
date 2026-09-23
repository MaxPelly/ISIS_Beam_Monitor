import random
from datetime import datetime, timedelta, timezone

from isis_monitor.messages import (
    Notification,
    Severity,
    beam_change,
    fmt_duration,
    fmt_time,
    mcr_news,
    run_finishing,
    run_started,
    set_timezone,
    startup_status,
)


# ---------------------------------------------------------------------------
# fmt_time
# ---------------------------------------------------------------------------

def test_fmt_time_converts_utc_to_uk_local():
    # Summer, so UK is on BST (UTC+1): 13:05 UTC -> 14:05 local.
    dt = datetime(2026, 9, 23, 13, 5, tzinfo=timezone.utc)
    assert fmt_time(dt) == "Wed 23 Sep 14:05"


def test_set_timezone_changes_fmt_time():
    dt = datetime(2026, 9, 23, 13, 5, tzinfo=timezone.utc)
    try:
        set_timezone("America/New_York")
        assert fmt_time(dt) == "Wed 23 Sep 09:05"
    finally:
        set_timezone("Europe/London")  # restore the default for other tests


# ---------------------------------------------------------------------------
# fmt_duration
# ---------------------------------------------------------------------------

def test_fmt_duration_hours_and_minutes():
    assert fmt_duration(timedelta(hours=3, minutes=12)) == "3h 12m"


def test_fmt_duration_minutes_only():
    assert fmt_duration(timedelta(minutes=45)) == "45m"


def test_fmt_duration_seconds_only():
    assert fmt_duration(timedelta(seconds=20)) == "20s"


def test_fmt_duration_negative_clamps_to_zero():
    assert fmt_duration(timedelta(seconds=-5)) == "0s"


# ---------------------------------------------------------------------------
# Notification.to_plain_text
# ---------------------------------------------------------------------------

def test_to_plain_text_includes_all_parts():
    dt = datetime(2026, 9, 23, 13, 5, tzinfo=timezone.utc)
    notification = Notification(
        title="TS1 Beam is now high",
        text="Current: 150.000 uA",
        emoji="🟢",
        facts=[("Previous", "medium")],
        flavour="Off to the races.",
        timestamp=dt,
    )

    plain = notification.to_plain_text()

    assert "🟢 TS1 Beam is now high" in plain
    assert "Current: 150.000 uA" in plain
    assert "Previous: medium" in plain
    assert "Off to the races." in plain
    assert "Wed 23 Sep 14:05" in plain


def test_to_plain_text_omits_empty_optional_parts():
    notification = Notification(title="Title", text="Text")
    plain = notification.to_plain_text()
    assert plain == "Title\nText"


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------

def test_beam_change_builder_going_up_is_good():
    dt = datetime(2026, 9, 23, 13, 5, tzinfo=timezone.utc)
    n = beam_change("TS1", "low", "high", 150.0, 20.0, 140.0, timedelta(hours=3, minutes=12), dt)
    assert n.title == "TS1 ⬆️ low → high"
    assert n.severity == Severity.GOOD
    assert n.emoji == "🟢"
    assert n.facts == [
        ("Current", "150.000 uA"),
        ("Previous", "20.000 uA"),
        ("% of high threshold", "107%"),
        ("Was low", "for 3h 12m"),
    ]
    assert n.timestamp == dt


def test_beam_change_builder_dropping_to_low_is_warning():
    dt = datetime(2026, 9, 23, 13, 5, tzinfo=timezone.utc)
    n = beam_change("TS1", "medium", "low", 20.0, 80.0, 140.0, timedelta(minutes=45), dt)
    assert n.title == "TS1 ⬇️ medium → low"
    assert n.severity == Severity.WARNING
    assert n.emoji == "🟠"


def test_beam_change_builder_going_to_off_is_attention():
    dt = datetime(2026, 9, 23, 13, 5, tzinfo=timezone.utc)
    n = beam_change("TS1", "low", "off", 0.0, 20.0, 140.0, timedelta(minutes=10), dt)
    assert n.title == "TS1 ⬇️ low → off"
    assert n.severity == Severity.ATTENTION
    assert n.emoji == "🔴"


def test_beam_change_builder_state_emoji_for_medium():
    dt = datetime(2026, 9, 23, 13, 5, tzinfo=timezone.utc)
    n = beam_change("TS1", "low", "medium", 80.0, 20.0, 140.0, timedelta(minutes=10), dt)
    assert n.emoji == "🟡"


def test_beam_change_builder_short_outage_keeps_state_emoji():
    """A quick blip (< 1h off) is not a 'restored' event — no party emoji."""
    dt = datetime(2026, 9, 23, 13, 5, tzinfo=timezone.utc)
    n = beam_change("TS1", "off", "high", 150.0, 0.0, 140.0, timedelta(minutes=30), dt)
    assert n.emoji == "🟢"


def test_beam_change_builder_long_outage_is_restored():
    """Recovering from an hour-plus outage gets the celebratory emoji instead."""
    dt = datetime(2026, 9, 23, 13, 5, tzinfo=timezone.utc)
    n = beam_change("TS1", "off", "high", 150.0, 0.0, 140.0, timedelta(hours=1, minutes=5), dt)
    assert n.emoji == "🎉"


def test_beam_change_builder_no_rng_means_no_flavour():
    dt = datetime(2026, 9, 23, 13, 5, tzinfo=timezone.utc)
    n = beam_change("TS1", "low", "high", 150.0, 20.0, 140.0, timedelta(hours=1), dt)
    assert n.flavour == ""


def test_beam_change_builder_with_rng_picks_deterministic_flavour():
    dt = datetime(2026, 9, 23, 13, 5, tzinfo=timezone.utc)
    n = beam_change(
        "TS1", "low", "high", 150.0, 20.0, 140.0, timedelta(hours=1), dt, rng=random.Random(1),
    )
    assert n.flavour != ""
    # Same seed picks the same line every time.
    n2 = beam_change(
        "TS1", "low", "high", 150.0, 20.0, 140.0, timedelta(hours=1), dt, rng=random.Random(1),
    )
    assert n.flavour == n2.flavour


def test_beam_change_builder_includes_trip_note():
    dt = datetime(2026, 9, 23, 13, 5, tzinfo=timezone.utc)
    n = beam_change(
        "TS1", "low", "off", 0.0, 20.0, 140.0, timedelta(minutes=10), dt,
        trip_note="⚠️ TS2 and Muons also went off, likely a facility-wide trip",
    )
    assert "facility-wide trip" in n.text


def test_startup_status_builder():
    dt = datetime(2026, 9, 23, 13, 5, tzinfo=timezone.utc)
    n = startup_status("TS1", "high", 150.0, dt)
    assert n.title == "Monitor online: TS1 is high"
    assert n.emoji == "🛰️"
    assert "150.000 uA" in n.text
    assert n.flavour == ""


def test_startup_status_builder_picks_flavour_when_rng_given():
    dt = datetime(2026, 9, 23, 13, 5, tzinfo=timezone.utc)
    n = startup_status("TS1", "high", 150.0, dt, rng=random.Random(1))
    assert n.flavour != ""


def test_run_started_builder():
    dt = datetime(2026, 9, 23, 13, 5, tzinfo=timezone.utc)
    n = run_started("Run 12345", dt)
    assert n.title == "New run started"
    assert n.text == "Run 12345"
    assert n.emoji == "🚀"
    assert n.flavour == ""


def test_run_finishing_builder():
    dt = datetime(2026, 9, 23, 13, 5, tzinfo=timezone.utc)
    n = run_finishing("Run 12345", dt)
    assert n.title == "Run about to finish"
    assert n.text == "Run 12345"
    assert n.emoji == "🏁"
    assert n.flavour == ""


def test_mcr_news_builder():
    dt = datetime(2026, 9, 23, 13, 5, tzinfo=timezone.utc)
    n = mcr_news("Beam restored after fault.", dt)
    assert n.title == "MCR News"
    assert n.text == "Beam restored after fault."
    assert n.emoji == "📰"
    assert n.flavour == ""


def test_run_and_mcr_builders_default_to_info_severity():
    dt = datetime(2026, 9, 23, 13, 5, tzinfo=timezone.utc)
    for n in (
        run_started("Run 1", dt),
        run_finishing("Run 1", dt),
        mcr_news("News", dt),
    ):
        assert n.severity == Severity.INFO


def test_run_and_mcr_builders_pick_flavour_when_rng_given():
    dt = datetime(2026, 9, 23, 13, 5, tzinfo=timezone.utc)
    rng = random.Random(1)
    for n in (
        run_started("Run 1", dt, rng=rng),
        run_finishing("Run 1", dt, rng=rng),
        mcr_news("News", dt, rng=rng),
    ):
        assert n.flavour != ""
