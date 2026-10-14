from datetime import datetime, timedelta, timezone

import pytest

from isis_monitor.messages import (
    MCR_NEWS_EMOJI,
    Notification,
    Severity,
    beam_change,
    collection_stalled,
    daily_summary,
    fmt_duration,
    fmt_time,
    get_timezone,
    mcr_news,
    run_finishing,
    run_started,
    set_timezone,
    startup_status,
)

DT = datetime(2026, 9, 23, 13, 5, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# fmt_time
# ---------------------------------------------------------------------------

def test_fmt_time_converts_utc_to_uk_local():
    # Summer, so UK is on BST (UTC+1): 13:05 UTC -> 14:05 local.
    assert fmt_time(DT) == "Wed 23 Sep 14:05"


def test_set_timezone_changes_fmt_time():
    try:
        set_timezone("America/New_York")
        assert fmt_time(DT) == "Wed 23 Sep 09:05"
        assert get_timezone().key == "America/New_York"
    finally:
        set_timezone("Europe/London")  # restore the default for other tests


# ---------------------------------------------------------------------------
# fmt_duration
# ---------------------------------------------------------------------------

def test_fmt_duration():
    assert fmt_duration(timedelta(hours=3, minutes=12)) == "3h 12m"
    assert fmt_duration(timedelta(minutes=45)) == "45m"
    assert fmt_duration(timedelta(seconds=20)) == "20s"
    assert fmt_duration(timedelta(seconds=-5)) == "0s"  # clamps to zero


# ---------------------------------------------------------------------------
# Notification.to_plain_text
# ---------------------------------------------------------------------------

def test_to_plain_text_includes_all_parts():
    notification = Notification(
        title="TS1 Beam is now high",
        text="Current: 150.000 uA",
        emoji="🟢",
        facts=[("Previous", "medium")],
        timestamp=DT,
    )

    plain = notification.to_plain_text()

    assert "🟢 TS1 Beam is now high" in plain
    assert "Current: 150.000 uA" in plain
    assert "Previous: medium" in plain
    assert "Wed 23 Sep 14:05" in plain


def test_to_plain_text_and_summary_omit_empty_optional_parts():
    assert Notification(title="Title", text="Text").to_plain_text() == "Title\nText"
    assert Notification(title="Title", text="").to_summary() == "Title"


# ---------------------------------------------------------------------------
# Notification.to_summary
# ---------------------------------------------------------------------------

def test_to_summary_is_one_line_with_details():
    notification = Notification(
        title="TS1 Beam is now high",
        text="Current: 150.000 uA\nfacility-wide trip",
        emoji="🟢",
        facts=[("Previous", "medium"), ("Was medium for", "3h 12m")],
        timestamp=DT,
    )

    summary = notification.to_summary()

    assert summary == (
        "🟢 TS1 Beam is now high | Current: 150.000 uA facility-wide trip"
        " | Previous: medium | Was medium for: 3h 12m | Wed 23 Sep 14:05"
    )


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------

# Typical arguments for each builder; tests override what they check.
CHANGE = dict(prev_state="low", new_state="high", beam_val=150.0, prev_val=20.0, high_threshold=140.0,
              time_in_prev_state=timedelta(hours=1), time_now=DT)
STARTED = dict(new_run_name="Run 2", prev_run_name="Run 1", prev_duration=timedelta(hours=1), prev_counts=1000.0,
               time_now=DT, target=130.0)
FINISHING = dict(run_name="Run 1", counts_collected=150.0, counts_target=130.0, rate_per_second=0.5,
                 instrument_state="high", time_now=DT)
STALLED = dict(beam_target="TS1", stalled_for=timedelta(minutes=17), time_now=DT)
SUMMARY = dict(uptime_pct=95.0, trips=0, longest_on_streak=timedelta(hours=10), sparkline="▇", instrument_runs={},
               time_now=DT)


def kw(base, **overrides):
    return {**base, **overrides}


def test_beam_change_builder_going_up_is_good():
    n = beam_change("TS1", **kw(CHANGE, time_in_prev_state=timedelta(hours=3, minutes=12)))
    assert n.title == "TS1 ⬆️ low → high"
    assert n.severity == Severity.GOOD
    assert n.emoji == "🟢"
    assert n.facts == [
        ("Current", "150.000 uA"),
        ("Previous", "20.000 uA"),
        ("% of high threshold", "107%"),
        ("Was low for", "3h 12m"),
    ]
    assert n.timestamp == DT
    assert n.beam == n.topic == "TS1"


@pytest.mark.parametrize("old, new, current, previous, severity, emoji", [
    ("medium", "low", 20.0, 80.0, Severity.WARNING, "🟠"),
    ("low", "off", 0.0, 20.0, Severity.ATTENTION, "🔴"),
])
def test_beam_change_builder_going_down(old, new, current, previous, severity, emoji):
    n = beam_change("TS1", **kw(CHANGE, prev_state=old, new_state=new, beam_val=current, prev_val=previous))
    assert n.title == f"TS1 ⬇️ {old} → {new}"
    assert (n.severity, n.emoji) == (severity, emoji)


def test_beam_change_builder_restored_emoji_only_after_an_hour_plus_outage():
    """A quick blip (< 1h off) is not a 'restored' event — no party emoji.
    Recovering from an hour-plus outage gets the celebratory emoji instead."""
    off = kw(CHANGE, prev_state="off", prev_val=0.0)
    assert beam_change("TS1", **kw(off, time_in_prev_state=timedelta(minutes=30))).emoji == "🟢"
    assert beam_change("TS1", **kw(off, time_in_prev_state=timedelta(hours=1, minutes=5))).emoji == "🎉"


def test_beam_change_builder_zero_high_threshold_does_not_crash():
    """A misconfigured (0.0) high boundary must degrade gracefully, not raise."""
    n = beam_change("Muons", **kw(CHANGE, high_threshold=0.0))
    assert ("% of high threshold", "n/a") in n.facts


def test_beam_change_builder_includes_trip_note():
    n = beam_change("TS1", **kw(CHANGE, new_state="off", beam_val=0.0,
                                trip_note="⚠️ TS2 and Muons also went off, likely a facility-wide trip"))
    assert "facility-wide trip" in n.text


def test_startup_status_builder():
    n = startup_status("TS1", state="high", beam_val=150.0, time_now=DT)
    assert n.title == "Monitor online: TS1 is high"
    assert n.emoji == "🛰️"
    assert "150.000 uA" in n.text
    assert n.beam == n.topic == "TS1"


def test_run_started_builder():
    n = run_started("PEARL", **kw(STARTED, new_run_name="Run 12346", prev_run_name="Run 12345",
                                  prev_duration=timedelta(hours=2), target=128.0, target_note="auto, from 5 runs"))
    assert n.title == "PEARL: New run started"
    assert n.text == "Run 12346"
    assert n.severity == Severity.INFO
    assert n.emoji == "🚀"
    assert n.facts == [
        ("Previous run", "Run 12345"),
        ("Duration", "2h 0m"),
        ("Final total collected", "1000.0 µA·h"),
        ("Target", "128 µA·h (auto, from 5 runs)"),
    ]
    # Negative counts mean no reading ever arrived for the previous run.
    n = run_started("PEARL", **kw(STARTED, prev_counts=-1.0))
    assert ("Final total collected", "unknown") in n.facts
    assert ("Target", "130 µA·h") in n.facts


def test_run_finishing_builder():
    n = run_finishing("PEARL", **kw(FINISHING, run_name="Run 12345"))
    assert n.title == "PEARL: Run about to finish"
    assert n.text == "Run 12345"
    assert n.severity == Severity.INFO
    assert n.emoji == "🏁"
    assert n.facts == [
        ("Collected", "150.0 / 130 µA·h"),
        ("Rate", "1800.0 µA"),
        ("ETA", "target reached"),
        ("Instrument beam", "high"),
    ]
    # Sent ahead of the target: a real ETA (80 µA·h to go at 1 µA·h a minute).
    n = run_finishing("PEARL", **kw(FINISHING, counts_collected=50.0, rate_per_second=1 / 60))
    assert dict(n.facts)["ETA"] == "1h 20m"
    # No ETA when the rate isn't positive.
    n = run_finishing("PEARL", **kw(FINISHING, counts_collected=50.0, rate_per_second=0.0))
    assert "ETA" not in [key for key, _ in n.facts]


def test_mcr_news_builder():
    n = mcr_news("Machine update: nothing to report.", time_now=DT)
    assert n.title == "MCR News"
    assert n.text == "Machine update: nothing to report."
    assert n.severity == Severity.INFO
    assert n.emoji == "📰"
    assert n.url is None
    n = mcr_news("Machine update.", time_now=DT, url="https://example.com/mcr")
    assert n.url == "https://example.com/mcr"
    assert n.url_label == "Open MCR news"


@pytest.mark.parametrize("news, severity, emoji", [
    ("Timing issues have been rectified. Beam back on @ 15:35", Severity.GOOD, "🎉"),
    ("We are investigating a water flow fault on Target 2.", Severity.ATTENTION, "🚨"),
    ("Scheduled maintenance will take place this evening.", Severity.WARNING, "🔧"),
    # A resolution naming the fault it just fixed is GOOD: good keywords are checked first.
    ("The faulty power supply in the Inner Synchrotron has been repaired.", Severity.GOOD, "🎉"),
    ("Beam tripped twice overnight.", Severity.ATTENTION, "🚨"),
    ("A faulted magnet is being replaced.", Severity.ATTENTION, "🚨"),
    ("Beam back online to TS1.", Severity.GOOD, "🎉"),
    # Keywords only count as whole words.
    ("Tissue samples arriving for the triple-axis study.", Severity.INFO, MCR_NEWS_EMOJI),
])
def test_mcr_news_builder_keywords(news, severity, emoji):
    n = mcr_news(news, time_now=DT)
    assert (n.severity, n.emoji) == (severity, emoji)


def test_collection_stalled_builder():
    n = collection_stalled("PEARL", **STALLED)
    assert n.title == "PEARL: Data collection stalled"
    assert n.severity == Severity.WARNING
    assert n.emoji == "⚠️"
    assert "17m" in n.text
    assert "TS1" in n.text


def test_daily_summary_builder():
    runs = {"PEARL": (6, 512.34), "WISH": (1, 60.0), "SXD": (0, 0.0)}
    n = daily_summary("TS1", **kw(SUMMARY, trips=2, longest_on_streak=timedelta(hours=5, minutes=30),
                                  sparkline="▁▂▃▄▅", instrument_runs=runs))
    assert n.title == "TS1 daily summary"
    assert n.severity == Severity.GOOD  # >= 90% uptime
    assert n.emoji == "📊"
    assert n.facts == [
        ("Uptime", "95%"),
        ("Trips", "2"),
        ("Longest continuous on", "5h 30m"),
        ("Sparkline", "▁▂▃▄▅"),
        ("Runs in last 24h", "7"),
        ("PEARL", "6 runs, 512.3 µA·h"),
        ("WISH", "1 run, 60.0 µA·h"),
        ("SXD", "no runs"),
    ]
    assert n.beam == "TS1"  # display_name doubles as the channel label here
    assert n.topic == "Summary"
    assert daily_summary("TS1", **kw(SUMMARY, uptime_pct=50.0)).severity == Severity.INFO  # below 90% uptime


def test_daily_summary_shows_data_coverage_only_when_samples_are_missing():
    assert "Data coverage" not in dict(daily_summary("TS1", **SUMMARY, coverage_pct=99.5).facts)
    facts = daily_summary("TS1", **SUMMARY, coverage_pct=62.4).facts
    assert facts[:2] == [("Uptime", "95%"), ("Data coverage", "62%")]


def test_no_builder_bakes_its_own_emoji_into_the_title():
    """`to_plain_text()` and the Teams card both prefix `emoji` onto `title` —
    a builder must never also embed that emoji inside the title text itself,
    or it renders twice everywhere the notification is shown."""
    notifications = [
        beam_change("TS1", **CHANGE),
        startup_status("TS1", state="high", beam_val=150.0, time_now=DT),
        run_started("PEARL", **STARTED),
        run_finishing("PEARL", **FINISHING),
        collection_stalled("PEARL", **STALLED),
        mcr_news("Machine update.", time_now=DT),
        daily_summary("TS1", **SUMMARY),
    ]
    for n in notifications:
        if n.emoji:
            assert n.emoji not in n.title, f"{n.emoji!r} duplicated in title of {n.title!r}"


def test_builders_set_topic():
    assert beam_change("Muons", **CHANGE).topic == "Muons"
    assert mcr_news("Machine update.", time_now=DT).topic == "MCR"
    # Run cards carry the instrument as their topic whatever their channel.
    for n in (
        run_started("PEARL", **STARTED),
        run_finishing("PEARL", **FINISHING),
        collection_stalled("PEARL", **STALLED),
    ):
        assert n.topic == "PEARL", n.title


def test_sparkline_chars():
    from isis_monitor.messages import _BLOCKS, sparkline_chars
    assert sparkline_chars([], 10) == " " * 10
    padded = sparkline_chars([1.0, 2.0, 3.0], 10)
    assert len(padded) == 10 and padded.startswith(" " * 7)  # left-padded to the width
    assert len(sparkline_chars(list(range(20)), 10)) == 10  # the newest that fit
    assert sparkline_chars([0.0] * 5, 5) == " " * 5  # all zero is a flat baseline
    assert sparkline_chars([0.0, 100.0], 2) == " " + _BLOCKS[-1]
    assert sparkline_chars([5.0, 5.0], 2) == _BLOCKS[len(_BLOCKS) // 2] * 2  # steady, not zero
