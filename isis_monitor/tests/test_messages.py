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
# fmt_time / fmt_duration
# ---------------------------------------------------------------------------

def test_fmt_time_converts_to_the_configured_timezone():
    # Summer, so UK is on BST (UTC+1): 13:05 UTC -> 14:05 local.
    assert fmt_time(DT) == "Wed 23 Sep 14:05"
    set_timezone("America/New_York")  # conftest restores the default
    assert fmt_time(DT) == "Wed 23 Sep 09:05"
    assert get_timezone().key == "America/New_York"


def test_fmt_duration():
    assert fmt_duration(timedelta(hours=3, minutes=12)) == "3h 12m"
    assert fmt_duration(timedelta(minutes=45)) == "45m"
    assert fmt_duration(timedelta(seconds=20)) == "20s"
    assert fmt_duration(timedelta(seconds=-5)) == "0s"  # clamps to zero


# ---------------------------------------------------------------------------
# Notification.to_plain_text / to_summary
# ---------------------------------------------------------------------------

def test_plain_text_and_summary_carry_every_part():
    """The summary is the whole message on one line: text-only Teams clients
    and flows show it instead of the card."""
    notification = Notification(
        title="TS1 Beam is now high",
        text="Current: 150.000 uA\nfacility-wide trip",
        emoji="🟢",
        facts=[("Previous", "medium"), ("Was medium for", "3h 12m")],
        timestamp=DT,
    )

    assert notification.to_plain_text() == (
        "🟢 TS1 Beam is now high\nCurrent: 150.000 uA\nfacility-wide trip"
        "\nPrevious: medium\nWas medium for: 3h 12m\nWed 23 Sep 14:05"
    )
    assert notification.to_summary() == (
        "🟢 TS1 Beam is now high | Current: 150.000 uA facility-wide trip"
        " | Previous: medium | Was medium for: 3h 12m | Wed 23 Sep 14:05"
    )
    # Empty optional parts are left out, not shown blank.
    assert Notification(title="Title", text="Text").to_plain_text() == "Title\nText"
    assert Notification(title="Title", text="").to_summary() == "Title"


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


# Every builder with its typical arguments, and what it should give:
# title, severity, emoji, topic, beam, and parts of its text.
ALL_BUILDERS = [
    pytest.param(lambda: beam_change("Muons", **CHANGE), "Muons ⬆️ low → high", Severity.GOOD, "🟢",
                 "Muons", "Muons", ["Muons beam moved from low to high."], id="beam_change"),
    pytest.param(lambda: startup_status("TS1", state="high", beam_val=150.0, time_now=DT),
                 "Monitor online: TS1 is high", Severity.INFO, "🛰️", "TS1", "TS1", ["150.000 uA"], id="startup"),
    # Run cards carry the instrument as their topic whatever their channel.
    pytest.param(lambda: run_started("PEARL", **STARTED), "PEARL: New run started", Severity.INFO, "🚀",
                 "PEARL", "", ["Run 2"], id="run_started"),
    pytest.param(lambda: run_finishing("PEARL", **FINISHING), "PEARL: Run about to finish", Severity.INFO, "🏁",
                 "PEARL", "", ["Run 1"], id="run_finishing"),
    pytest.param(lambda: collection_stalled("PEARL", **STALLED), "PEARL: Data collection stalled",
                 Severity.WARNING, "⚠️", "PEARL", "", ["17m", "TS1"], id="stalled"),
    pytest.param(lambda: mcr_news("Machine update.", time_now=DT), "MCR News", Severity.INFO, MCR_NEWS_EMOJI,
                 "MCR", "", ["Machine update."], id="mcr_news"),
    # display_name doubles as the channel label here
    pytest.param(lambda: daily_summary("TS1", **SUMMARY), "TS1 daily summary", Severity.GOOD, "📊",
                 "Summary", "TS1", ["95% uptime"], id="daily_summary"),
]


@pytest.mark.parametrize("build, title, severity, emoji, topic, beam, text_parts", ALL_BUILDERS)
def test_builder_basics(build, title, severity, emoji, topic, beam, text_parts):
    n = build()
    assert (n.title, n.severity, n.emoji, n.topic, n.beam, n.timestamp) == (title, severity, emoji, topic, beam, DT)
    for part in text_parts:
        assert part in n.text
    # `to_plain_text()` and the Teams card both prefix `emoji` onto `title`, so
    # a builder mustn't also embed it in the title, or it renders twice.
    assert emoji not in title


@pytest.mark.parametrize("old, new, current, previous, arrow, severity, emoji", [
    ("low", "high", 150.0, 20.0, "⬆️", Severity.GOOD, "🟢"),
    ("medium", "low", 20.0, 80.0, "⬇️", Severity.WARNING, "🟠"),
    ("low", "off", 0.0, 20.0, "⬇️", Severity.ATTENTION, "🔴"),
])
def test_beam_change_builder_direction(old, new, current, previous, arrow, severity, emoji):
    n = beam_change("TS1", **kw(CHANGE, prev_state=old, new_state=new, beam_val=current, prev_val=previous))
    assert n.title == f"TS1 {arrow} {old} → {new}"
    assert (n.severity, n.emoji) == (severity, emoji)


def test_beam_change_builder_facts_and_notes():
    n = beam_change("TS1", **kw(CHANGE, time_in_prev_state=timedelta(hours=3, minutes=12)))
    assert n.facts == [
        ("Current", "150.000 uA"),
        ("Previous", "20.000 uA"),
        ("% of high threshold", "107%"),
        ("Was low for", "3h 12m"),
    ]
    # A misconfigured (0.0) high boundary must degrade gracefully, not raise.
    assert ("% of high threshold", "n/a") in beam_change("Muons", **kw(CHANGE, high_threshold=0.0)).facts
    n = beam_change("TS1", **kw(CHANGE, new_state="off", beam_val=0.0,
                                trip_note="⚠️ TS2 and Muons also went off, likely a facility-wide trip"))
    assert "facility-wide trip" in n.text
    # A quick blip (< 1h off) is not a 'restored' event — no party emoji.
    # Recovering from an hour-plus outage gets the celebratory emoji instead.
    off = kw(CHANGE, prev_state="off", prev_val=0.0)
    assert beam_change("TS1", **kw(off, time_in_prev_state=timedelta(minutes=30))).emoji == "🟢"
    assert beam_change("TS1", **kw(off, time_in_prev_state=timedelta(hours=1, minutes=5))).emoji == "🎉"


def test_run_started_builder_facts():
    n = run_started("PEARL", **kw(STARTED, new_run_name="Run 12346", prev_run_name="Run 12345",
                                  prev_duration=timedelta(hours=2), target=128.0, target_note="auto, from 5 runs"))
    assert n.text == "Run 12346"
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


def test_run_finishing_builder_facts():
    n = run_finishing("PEARL", **kw(FINISHING, run_name="Run 12345"))
    assert n.text == "Run 12345"
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


def test_mcr_news_builder_url():
    assert mcr_news("Machine update.", time_now=DT).url is None
    n = mcr_news("Machine update.", time_now=DT, url="https://example.com/mcr")
    assert n.url == "https://example.com/mcr"
    assert n.url_label == "Open MCR news"


def test_daily_summary_builder_facts():
    runs = {"PEARL": (6, 512.34), "WISH": (1, 60.0), "SXD": (0, 0.0)}
    n = daily_summary("TS1", **kw(SUMMARY, trips=2, longest_on_streak=timedelta(hours=5, minutes=30),
                                  sparkline="▁▂▃▄▅", instrument_runs=runs))
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
    assert daily_summary("TS1", **kw(SUMMARY, uptime_pct=50.0)).severity == Severity.INFO  # below 90% uptime
    # Data coverage is shown only when samples are missing.
    assert "Data coverage" not in dict(daily_summary("TS1", **SUMMARY, coverage_pct=99.5).facts)
    facts = daily_summary("TS1", **SUMMARY, coverage_pct=62.4).facts
    assert facts[:2] == [("Uptime", "95%"), ("Data coverage", "62%")]


def test_sparkline_chars():
    from isis_monitor.messages import _BLOCKS, sparkline_chars
    assert sparkline_chars([], 10) == " " * 10
    padded = sparkline_chars([1.0, 2.0, 3.0], 10)
    assert len(padded) == 10 and padded.startswith(" " * 7)  # left-padded to the width
    assert len(sparkline_chars(list(range(20)), 10)) == 10  # the newest that fit
    assert sparkline_chars([0.0] * 5, 5) == " " * 5  # all zero is a flat baseline
    assert sparkline_chars([0.0, 100.0], 2) == " " + _BLOCKS[-1]
    assert sparkline_chars([5.0, 5.0], 2) == _BLOCKS[len(_BLOCKS) // 2] * 2  # steady, not zero
