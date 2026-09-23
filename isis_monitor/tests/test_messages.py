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
    startup_status,
)


# ---------------------------------------------------------------------------
# fmt_time
# ---------------------------------------------------------------------------

def test_fmt_time_converts_utc_to_uk_local():
    # Summer, so UK is on BST (UTC+1): 13:05 UTC -> 14:05 local.
    dt = datetime(2026, 9, 23, 13, 5, tzinfo=timezone.utc)
    assert fmt_time(dt) == "Wed 23 Sep 14:05"


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

def test_beam_change_builder():
    dt = datetime(2026, 9, 23, 13, 5, tzinfo=timezone.utc)
    n = beam_change("TS1", "high", 150.0, dt)
    assert n.title == "TS1 Beam is now high"
    assert "150.000 uA" in n.text
    assert n.timestamp == dt


def test_startup_status_builder():
    dt = datetime(2026, 9, 23, 13, 5, tzinfo=timezone.utc)
    n = startup_status("TS1", "high", 150.0, dt)
    assert n.title == "Monitor online: TS1 is high"
    assert "150.000 uA" in n.text


def test_run_started_builder():
    dt = datetime(2026, 9, 23, 13, 5, tzinfo=timezone.utc)
    n = run_started("Run 12345", dt)
    assert n.title == "New run started"
    assert n.text == "Run 12345"


def test_run_finishing_builder():
    dt = datetime(2026, 9, 23, 13, 5, tzinfo=timezone.utc)
    n = run_finishing("Run 12345", dt)
    assert n.title == "Run about to finish"
    assert n.text == "Run 12345"


def test_mcr_news_builder():
    dt = datetime(2026, 9, 23, 13, 5, tzinfo=timezone.utc)
    n = mcr_news("Beam restored after fault.", dt)
    assert n.title == "MCR News"
    assert n.text == "Beam restored after fault."


def test_builders_default_to_info_severity_and_no_emoji():
    dt = datetime(2026, 9, 23, 13, 5, tzinfo=timezone.utc)
    for n in (
        beam_change("TS1", "high", 150.0, dt),
        startup_status("TS1", "high", 150.0, dt),
        run_started("Run 1", dt),
        run_finishing("Run 1", dt),
        mcr_news("News", dt),
    ):
        assert n.severity == Severity.INFO
        assert n.emoji == ""
