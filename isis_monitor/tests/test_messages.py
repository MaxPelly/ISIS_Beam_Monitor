import random
from datetime import datetime, timedelta, timezone

from isis_monitor.messages import (
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


def test_get_timezone_reflects_set_timezone():
    try:
        set_timezone("America/New_York")
        assert get_timezone().key == "America/New_York"
    finally:
        set_timezone("Europe/London")


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
# Notification.to_summary
# ---------------------------------------------------------------------------

def test_to_summary_is_one_line_with_details_but_no_flavour():
    dt = datetime(2026, 9, 23, 13, 5, tzinfo=timezone.utc)
    notification = Notification(
        title="TS1 Beam is now high",
        text="Current: 150.000 uA\nfacility-wide trip",
        emoji="🟢",
        facts=[("Previous", "medium"), ("Was medium for", "3h 12m")],
        flavour="Off to the races.",
        timestamp=dt,
    )

    summary = notification.to_summary()

    assert summary == (
        "🟢 TS1 Beam is now high | Current: 150.000 uA facility-wide trip"
        " | Previous: medium | Was medium for: 3h 12m | Wed 23 Sep 14:05"
    )


def test_to_summary_omits_empty_optional_parts():
    assert Notification(title="Title", text="").to_summary() == "Title"


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
        ("Was low for", "3h 12m"),
    ]
    assert n.timestamp == dt
    assert n.channel == ""


def test_beam_change_builder_sets_explicit_channel():
    dt = datetime(2026, 9, 23, 13, 5, tzinfo=timezone.utc)
    n = beam_change(
        "TS1", "low", "high", 150.0, 20.0, 140.0, timedelta(hours=1), dt, channel="TS1",
    )
    assert n.channel == "TS1"


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


def test_beam_change_builder_zero_high_threshold_does_not_crash():
    """A misconfigured (0.0) high boundary must degrade gracefully, not raise."""
    dt = datetime(2026, 9, 23, 13, 5, tzinfo=timezone.utc)
    n = beam_change("Muon", "low", "high", 3.0, 1.0, 0.0, timedelta(minutes=5), dt)
    assert ("% of high threshold", "n/a") in n.facts


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
    n = startup_status("TS1", "high", 150.0, dt, channel="TS1")
    assert n.title == "Monitor online: TS1 is high"
    assert n.emoji == "🛰️"
    assert "150.000 uA" in n.text
    assert n.flavour == ""
    assert n.channel == "TS1"


def test_startup_status_builder_picks_flavour_when_rng_given():
    dt = datetime(2026, 9, 23, 13, 5, tzinfo=timezone.utc)
    n = startup_status("TS1", "high", 150.0, dt, rng=random.Random(1))
    assert n.flavour != ""


def test_run_started_builder():
    dt = datetime(2026, 9, 23, 13, 5, tzinfo=timezone.utc)
    n = run_started("PEARL", "Run 12346", "Run 12345", timedelta(hours=2), 1000.0, dt)
    assert n.title == "PEARL: New run started"
    assert n.channel == ""  # filled with "Experiment Updates" by the channel on broadcast
    assert n.text == "Run 12346"
    assert n.emoji == "🚀"
    assert n.flavour == ""
    assert n.facts == [
        ("Previous run", "Run 12345"),
        ("Duration", "2h 0m"),
        ("Final total collected", "1000.0 µA·h"),
    ]


def test_run_finishing_builder():
    dt = datetime(2026, 9, 23, 13, 5, tzinfo=timezone.utc)
    n = run_finishing("PEARL", "Run 12345", 150.0, 130.0, 0.5, "high", dt)
    assert n.title == "PEARL: Run about to finish"
    assert n.channel == ""  # filled with "Experiment Updates" by the channel on broadcast
    assert n.text == "Run 12345"
    assert n.emoji == "🏁"
    assert n.flavour == ""
    assert n.facts == [
        ("Collected", "150.0 / 130 µA·h"),
        ("Rate", "1800.0 µA"),
        ("ETA", "0s"),
        ("Instrument beam", "high"),
    ]


def test_run_finishing_builder_omits_eta_when_rate_not_positive():
    dt = datetime(2026, 9, 23, 13, 5, tzinfo=timezone.utc)
    n = run_finishing("PEARL", "Run 12345", 50.0, 130.0, 0.0, "high", dt)
    fact_keys = [key for key, _ in n.facts]
    assert "ETA" not in fact_keys


def test_mcr_news_builder():
    dt = datetime(2026, 9, 23, 13, 5, tzinfo=timezone.utc)
    n = mcr_news("Machine update: nothing to report.", dt)
    assert n.title == "MCR News"
    assert n.text == "Machine update: nothing to report."
    assert n.severity == Severity.INFO
    assert n.emoji == "📰"
    assert n.flavour == ""
    assert n.url is None


def test_mcr_news_builder_good_keyword():
    dt = datetime(2026, 9, 23, 13, 5, tzinfo=timezone.utc)
    n = mcr_news("Timing issues have been rectified. Beam back on @ 15:35", dt)
    assert n.severity == Severity.GOOD
    assert n.emoji == "🎉"


def test_mcr_news_builder_attention_keyword():
    dt = datetime(2026, 9, 23, 13, 5, tzinfo=timezone.utc)
    n = mcr_news("We are investigating a water flow fault on Target 2.", dt)
    assert n.severity == Severity.ATTENTION
    assert n.emoji == "🚨"


def test_mcr_news_builder_warning_keyword():
    dt = datetime(2026, 9, 23, 13, 5, tzinfo=timezone.utc)
    n = mcr_news("Scheduled maintenance will take place this evening.", dt)
    assert n.severity == Severity.WARNING
    assert n.emoji == "🔧"


def test_mcr_news_builder_good_checked_before_attention():
    """A resolution message naming the fault it just fixed must classify as GOOD."""
    dt = datetime(2026, 9, 23, 13, 5, tzinfo=timezone.utc)
    n = mcr_news("The faulty power supply in the Inner Synchrotron has been repaired.", dt)
    assert n.severity == Severity.GOOD
    assert n.emoji == "🎉"


def test_mcr_news_builder_includes_url_and_label():
    dt = datetime(2026, 9, 23, 13, 5, tzinfo=timezone.utc)
    n = mcr_news("Machine update.", dt, url="https://example.com/mcr")
    assert n.url == "https://example.com/mcr"
    assert n.url_label == "Open MCR news"


def test_run_and_mcr_builders_default_to_info_severity():
    dt = datetime(2026, 9, 23, 13, 5, tzinfo=timezone.utc)
    for n in (
        run_started("PEARL", "Run 2", "Run 1", timedelta(hours=1), 1000.0, dt),
        run_finishing("PEARL", "Run 1", 150.0, 130.0, 0.5, "high", dt),
        mcr_news("News", dt),
    ):
        assert n.severity == Severity.INFO


def test_run_and_mcr_builders_pick_flavour_when_rng_given():
    dt = datetime(2026, 9, 23, 13, 5, tzinfo=timezone.utc)
    rng = random.Random(1)
    for n in (
        run_started("PEARL", "Run 2", "Run 1", timedelta(hours=1), 1000.0, dt, rng=rng),
        run_finishing("PEARL", "Run 1", 150.0, 130.0, 0.5, "high", dt, rng=rng),
        mcr_news("News", dt, rng=rng),
    ):
        assert n.flavour != ""


def test_collection_stalled_builder():
    dt = datetime(2026, 9, 23, 13, 5, tzinfo=timezone.utc)
    n = collection_stalled("PEARL", "TS1", timedelta(minutes=17), dt)
    assert n.title == "PEARL: Data collection stalled"
    assert n.channel == ""  # filled with "Experiment Updates" by the channel on broadcast
    assert n.severity == Severity.WARNING
    assert n.emoji == "⚠️"
    assert "17m" in n.text
    assert "TS1" in n.text


def test_daily_summary_builder():
    dt = datetime(2026, 9, 23, 13, 5, tzinfo=timezone.utc)
    n = daily_summary("TS1", 95.0, 2, timedelta(hours=5, minutes=30), "▁▂▃▄▅", 7, dt)
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


def test_daily_summary_builder_low_uptime_is_info():
    dt = datetime(2026, 9, 23, 13, 5, tzinfo=timezone.utc)
    n = daily_summary("TS1", 50.0, 5, timedelta(hours=1), "▁▂", 1, dt)
    assert n.severity == Severity.INFO


def test_daily_summary_builder_new_record_note():
    dt = datetime(2026, 9, 23, 13, 5, tzinfo=timezone.utc)
    n = daily_summary("TS1", 95.0, 0, timedelta(hours=10), "▇", 3, dt, is_new_record=True)
    assert "New record" in n.text


def test_daily_summary_builder_carries_fact_of_the_day_as_flavour():
    dt = datetime(2026, 9, 23, 13, 5, tzinfo=timezone.utc)
    n = daily_summary("TS1", 95.0, 0, timedelta(hours=10), "▇", 3, dt, fact_of_the_day="Neutrons are neutral.")
    assert n.flavour == "Neutrons are neutral."


def test_run_milestone_builder():
    dt = datetime(2026, 9, 23, 13, 5, tzinfo=timezone.utc)
    n = run_milestone("PEARL", 25, dt)
    assert n.title == "PEARL: 25 runs completed"
    assert n.channel == ""  # filled with "Experiment Updates" by the channel on broadcast
    assert n.severity == Severity.GOOD
    assert n.emoji == "🏆"
    assert n.flavour == ""
    # title must not also bake in the emoji, or to_plain_text()/the Teams
    # card header (which both prefix `emoji` onto `title`) would show it twice.
    assert "🏆" not in n.title
    assert n.to_plain_text().count("🏆") == 1


def test_run_milestone_builder_picks_flavour_when_rng_given():
    dt = datetime(2026, 9, 23, 13, 5, tzinfo=timezone.utc)
    n = run_milestone("PEARL", 25, dt, rng=random.Random(1))
    assert n.flavour != ""


def test_no_builder_bakes_its_own_emoji_into_the_title():
    """`to_plain_text()` and the Teams card both prefix `emoji` onto `title` —
    a builder must never also embed that emoji inside the title text itself,
    or it renders twice everywhere the notification is shown."""
    dt = datetime(2026, 9, 23, 13, 5, tzinfo=timezone.utc)
    notifications = [
        beam_change("TS1", "low", "high", 150.0, 20.0, 140.0, timedelta(hours=1), dt),
        startup_status("TS1", "high", 150.0, dt),
        run_started("PEARL", "Run 2", "Run 1", timedelta(hours=1), 1000.0, dt),
        run_finishing("PEARL", "Run 1", 150.0, 130.0, 0.5, "high", dt),
        collection_stalled("PEARL", "TS1", timedelta(minutes=17), dt),
        mcr_news("Machine update.", dt),
        daily_summary("TS1", 95.0, 0, timedelta(hours=10), "▇", 3, dt, is_new_record=True),
        run_milestone("PEARL", 25, dt),
    ]
    for n in notifications:
        if n.emoji:
            assert n.emoji not in n.title, f"{n.emoji!r} duplicated in title of {n.title!r}"


def test_run_started_with_no_reading_shows_unknown_total():
    dt = datetime(2026, 9, 23, 13, 5, tzinfo=timezone.utc)
    n = run_started("PEARL", "Run 2", "Run 1", timedelta(hours=1), -1.0, dt)
    assert ("Final total collected", "unknown") in n.facts
