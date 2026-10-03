import random
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
    run_milestone,
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
        flavour="Off to the races.",
        timestamp=DT,
    )

    plain = notification.to_plain_text()

    assert "🟢 TS1 Beam is now high" in plain
    assert "Current: 150.000 uA" in plain
    assert "Previous: medium" in plain
    assert "Off to the races." in plain
    assert "Wed 23 Sep 14:05" in plain


def test_to_plain_text_and_summary_omit_empty_optional_parts():
    assert Notification(title="Title", text="Text").to_plain_text() == "Title\nText"
    assert Notification(title="Title", text="").to_summary() == "Title"


# ---------------------------------------------------------------------------
# Notification.to_summary
# ---------------------------------------------------------------------------

def test_to_summary_is_one_line_with_details_but_no_flavour():
    notification = Notification(
        title="TS1 Beam is now high",
        text="Current: 150.000 uA\nfacility-wide trip",
        emoji="🟢",
        facts=[("Previous", "medium"), ("Was medium for", "3h 12m")],
        flavour="Off to the races.",
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

def test_beam_change_builder_going_up_is_good():
    n = beam_change(
        "TS1", "low", "high", 150.0, 20.0, 140.0, timedelta(hours=3, minutes=12), DT, channel="TS1",
    )
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
    assert n.channel == "TS1"
    assert n.flavour == ""  # no rng, no flavour


@pytest.mark.parametrize("old, new, current, previous, severity, emoji", [
    ("medium", "low", 20.0, 80.0, Severity.WARNING, "🟠"),
    ("low", "off", 0.0, 20.0, Severity.ATTENTION, "🔴"),
])
def test_beam_change_builder_going_down(old, new, current, previous, severity, emoji):
    n = beam_change("TS1", old, new, current, previous, 140.0, timedelta(minutes=45), DT)
    assert n.title == f"TS1 ⬇️ {old} → {new}"
    assert (n.severity, n.emoji) == (severity, emoji)


def test_beam_change_builder_restored_emoji_only_after_an_hour_plus_outage():
    """A quick blip (< 1h off) is not a 'restored' event — no party emoji.
    Recovering from an hour-plus outage gets the celebratory emoji instead."""
    n = beam_change("TS1", "off", "high", 150.0, 0.0, 140.0, timedelta(minutes=30), DT)
    assert n.emoji == "🟢"
    n = beam_change("TS1", "off", "high", 150.0, 0.0, 140.0, timedelta(hours=1, minutes=5), DT)
    assert n.emoji == "🎉"


def test_beam_change_builder_zero_high_threshold_does_not_crash():
    """A misconfigured (0.0) high boundary must degrade gracefully, not raise."""
    n = beam_change("Muon", "low", "high", 3.0, 1.0, 0.0, timedelta(minutes=5), DT)
    assert ("% of high threshold", "n/a") in n.facts


def test_beam_change_builder_with_rng_picks_deterministic_flavour():
    n = beam_change(
        "TS1", "low", "high", 150.0, 20.0, 140.0, timedelta(hours=1), DT, rng=random.Random(1),
    )
    assert n.flavour != ""
    # Same seed picks the same line every time.
    n2 = beam_change(
        "TS1", "low", "high", 150.0, 20.0, 140.0, timedelta(hours=1), DT, rng=random.Random(1),
    )
    assert n.flavour == n2.flavour


def test_beam_change_builder_includes_trip_note():
    n = beam_change(
        "TS1", "low", "off", 0.0, 20.0, 140.0, timedelta(minutes=10), DT,
        trip_note="⚠️ TS2 and Muons also went off, likely a facility-wide trip",
    )
    assert "facility-wide trip" in n.text


def test_startup_status_builder():
    n = startup_status("TS1", "high", 150.0, DT, channel="TS1")
    assert n.title == "Monitor online: TS1 is high"
    assert n.emoji == "🛰️"
    assert "150.000 uA" in n.text
    assert n.flavour == ""
    assert n.channel == n.topic == "TS1"


def test_run_started_builder():
    n = run_started("PEARL", "Run 12346", "Run 12345", timedelta(hours=2), 1000.0, DT)
    assert n.title == "PEARL: New run started"
    assert n.channel == ""  # filled with "Experiment Updates" by the channel on broadcast
    assert n.text == "Run 12346"
    assert n.severity == Severity.INFO
    assert n.emoji == "🚀"
    assert n.flavour == ""
    assert n.facts == [
        ("Previous run", "Run 12345"),
        ("Duration", "2h 0m"),
        ("Final total collected", "1000.0 µA·h"),
    ]
    # Negative counts mean no reading ever arrived for the previous run.
    n = run_started("PEARL", "Run 2", "Run 1", timedelta(hours=1), -1.0, DT)
    assert ("Final total collected", "unknown") in n.facts


def test_run_finishing_builder():
    n = run_finishing("PEARL", "Run 12345", 150.0, 130.0, 0.5, "high", DT)
    assert n.title == "PEARL: Run about to finish"
    assert n.channel == ""  # filled with "Experiment Updates" by the channel on broadcast
    assert n.text == "Run 12345"
    assert n.severity == Severity.INFO
    assert n.emoji == "🏁"
    assert n.flavour == ""
    assert n.facts == [
        ("Collected", "150.0 / 130 µA·h"),
        ("Rate", "1800.0 µA"),
        ("ETA", "target reached"),
        ("Instrument beam", "high"),
    ]
    # Sent ahead of the target: a real ETA (80 µA·h to go at 1 µA·h a minute).
    n = run_finishing("PEARL", "Run 12345", 50.0, 130.0, 1 / 60, "high", DT)
    assert dict(n.facts)["ETA"] == "1h 20m"
    # No ETA when the rate isn't positive.
    n = run_finishing("PEARL", "Run 12345", 50.0, 130.0, 0.0, "high", DT)
    assert "ETA" not in [key for key, _ in n.facts]


def test_mcr_news_builder():
    n = mcr_news("Machine update: nothing to report.", DT)
    assert n.title == "MCR News"
    assert n.text == "Machine update: nothing to report."
    assert n.severity == Severity.INFO
    assert n.emoji == "📰"
    assert n.flavour == ""
    assert n.url is None
    n = mcr_news("Machine update.", DT, url="https://example.com/mcr")
    assert n.url == "https://example.com/mcr"
    assert n.url_label == "Open MCR news"


@pytest.mark.parametrize("news, severity, emoji", [
    ("Timing issues have been rectified. Beam back on @ 15:35", Severity.GOOD, "🎉"),
    ("We are investigating a water flow fault on Target 2.", Severity.ATTENTION, "🚨"),
    ("Scheduled maintenance will take place this evening.", Severity.WARNING, "🔧"),
    # A resolution naming the fault it just fixed is GOOD: good keywords are checked first.
    ("The faulty power supply in the Inner Synchrotron has been repaired.", Severity.GOOD, "🎉"),
    ("Beam tripped twice overnight.", Severity.ATTENTION, "🚨"),
    # Keywords only count as whole words.
    ("Tissue samples arriving for the triple-axis study.", Severity.INFO, MCR_NEWS_EMOJI),
])
def test_mcr_news_builder_keywords(news, severity, emoji):
    n = mcr_news(news, DT)
    assert (n.severity, n.emoji) == (severity, emoji)


def test_builders_pick_flavour_when_rng_given():
    rng = random.Random(1)
    for n in (
        startup_status("TS1", "high", 150.0, DT, rng=rng),
        run_started("PEARL", "Run 2", "Run 1", timedelta(hours=1), 1000.0, DT, rng=rng),
        run_finishing("PEARL", "Run 1", 150.0, 130.0, 0.5, "high", DT, rng=rng),
        mcr_news("News", DT, rng=rng),
        run_milestone("PEARL", 25, DT, rng=rng),
    ):
        assert n.flavour != "", n.title


def test_collection_stalled_builder():
    n = collection_stalled("PEARL", "TS1", timedelta(minutes=17), DT)
    assert n.title == "PEARL: Data collection stalled"
    assert n.channel == ""  # filled with "Experiment Updates" by the channel on broadcast
    assert n.severity == Severity.WARNING
    assert n.emoji == "⚠️"
    assert "17m" in n.text
    assert "TS1" in n.text


def test_daily_summary_builder():
    n = daily_summary("TS1", 95.0, 2, timedelta(hours=5, minutes=30), "▁▂▃▄▅", 7, DT)
    assert n.title == "TS1 daily summary"
    assert n.severity == Severity.GOOD  # >= 90% uptime
    assert n.emoji == "📊"
    assert "New record" not in n.text
    assert n.facts == [
        ("Uptime", "95%"),
        ("Trips", "2"),
        ("Longest continuous on", "5h 30m"),
        ("Sparkline", "▁▂▃▄▅"),
        ("Runs in last 24h", "7"),
    ]
    assert n.channel == "TS1"  # display_name doubles as the channel label here
    assert n.topic == "Summary"
    n = daily_summary("TS1", 50.0, 5, timedelta(hours=1), "▁▂", 1, DT)
    assert n.severity == Severity.INFO  # below 90% uptime


def test_daily_summary_shows_data_coverage_only_when_samples_are_missing():
    args = ("TS1", 95.0, 0, timedelta(hours=10), "▇", 3, DT)
    assert "Data coverage" not in dict(daily_summary(*args, coverage_pct=99.5).facts)
    facts = daily_summary(*args, coverage_pct=62.4).facts
    assert facts[:2] == [("Uptime", "95%"), ("Data coverage", "62%")]


def test_daily_summary_builder_new_record_note_and_fact_of_the_day():
    n = daily_summary(
        "TS1", 95.0, 0, timedelta(hours=10), "▇", 3, DT,
        is_new_record=True, fact_of_the_day="Neutrons are neutral.",
    )
    assert "New record" in n.text
    assert n.flavour == "Neutrons are neutral."


def test_run_milestone_builder():
    n = run_milestone("PEARL", 25, DT)
    assert n.title == "PEARL: 25 runs completed"
    assert n.channel == ""  # filled with "Experiment Updates" by the channel on broadcast
    assert n.severity == Severity.GOOD
    assert n.emoji == "🏆"
    assert n.flavour == ""


def test_no_builder_bakes_its_own_emoji_into_the_title():
    """`to_plain_text()` and the Teams card both prefix `emoji` onto `title` —
    a builder must never also embed that emoji inside the title text itself,
    or it renders twice everywhere the notification is shown."""
    notifications = [
        beam_change("TS1", "low", "high", 150.0, 20.0, 140.0, timedelta(hours=1), DT),
        startup_status("TS1", "high", 150.0, DT),
        run_started("PEARL", "Run 2", "Run 1", timedelta(hours=1), 1000.0, DT),
        run_finishing("PEARL", "Run 1", 150.0, 130.0, 0.5, "high", DT),
        collection_stalled("PEARL", "TS1", timedelta(minutes=17), DT),
        mcr_news("Machine update.", DT),
        daily_summary("TS1", 95.0, 0, timedelta(hours=10), "▇", 3, DT, is_new_record=True),
        run_milestone("PEARL", 25, DT),
    ]
    for n in notifications:
        if n.emoji:
            assert n.emoji not in n.title, f"{n.emoji!r} duplicated in title of {n.title!r}"


def test_builders_set_topic():
    assert beam_change("Muon", "low", "high", 5.0, 1.0, 5.0, timedelta(hours=1), DT, channel="Muons").topic == "Muons"
    assert mcr_news("Machine update.", DT).topic == "MCR"
    # Run cards carry the instrument as their topic whatever their channel.
    for n in (
        run_started("PEARL", "Run 2", "Run 1", timedelta(hours=1), 1000.0, DT),
        run_finishing("PEARL", "Run 1", 150.0, 130.0, 0.5, "high", DT),
        collection_stalled("PEARL", "TS1", timedelta(minutes=17), DT),
        run_milestone("PEARL", 25, DT, channel="PEARL"),
    ):
        assert n.topic == "PEARL", n.title
