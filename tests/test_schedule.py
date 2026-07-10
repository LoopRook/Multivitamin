from datetime import datetime

import bot_features


def _cfg(**kw):
    base = {"quote_weekdays": None, "quote_interval_days": 1, "last_quote_date": None}
    base.update(kw)
    return base


def test_interval_daily_always_due():
    assert bot_features._rename_due(_cfg(quote_interval_days=1), datetime(2026, 7, 9)) is True


def test_interval_every_n_days():
    now = datetime(2026, 7, 9)
    assert bot_features._rename_due(_cfg(quote_interval_days=3, last_quote_date="2026-07-07"), now) is False  # 2 days
    assert bot_features._rename_due(_cfg(quote_interval_days=3, last_quote_date="2026-07-06"), now) is True   # 3 days
    assert bot_features._rename_due(_cfg(quote_interval_days=3, last_quote_date=None), now) is True           # first run


def test_weekday_mode_matches_today_only():
    now = datetime(2026, 7, 9)
    today = now.weekday()
    other = (today + 1) % 7
    assert bot_features._rename_due(_cfg(quote_weekdays=str(today)), now) is True
    assert bot_features._rename_due(_cfg(quote_weekdays=str(other)), now) is False
    assert bot_features._rename_due(_cfg(quote_weekdays=f"{other},{today}"), now) is True  # multi-day incl. today


def test_weekday_overrides_interval():
    now = datetime(2026, 7, 9)
    today = now.weekday()
    # weekdays present -> weekday mode wins even with a long interval + a recent last run
    cfg = _cfg(quote_weekdays=str(today), quote_interval_days=30,
               last_quote_date=now.strftime("%Y-%m-%d"))
    assert bot_features._rename_due(cfg, now) is True


def test_feature_due_reuses_cadence():
    now = datetime(2026, 7, 9)
    today = now.weekday()
    assert bot_features._feature_due({"weekdays": None, "interval_days": 3,
                                      "last_run_date": "2026-07-07"}, now) is False
    assert bot_features._feature_due({"weekdays": None, "interval_days": 3,
                                      "last_run_date": "2026-07-06"}, now) is True
    assert bot_features._feature_due({"weekdays": str(today), "interval_days": 1,
                                      "last_run_date": now.strftime("%Y-%m-%d")}, now) is True


def test_cadence_desc():
    assert bot_features._cadence_desc({"quote_interval_days": 1}) == "Daily"
    assert bot_features._cadence_desc({"quote_interval_days": 3}) == "Every 3 days"
    assert bot_features._cadence_desc({"quote_weekdays": "6"}) == "Weekly on Sun"
    assert bot_features._cadence_desc({"quote_weekdays": "0,2,4"}) == "Weekly on Mon, Wed, Fri"


# ── _fire_action: the scheduled time is a threshold, not an exact minute ──────

FIRE = bot_features._fire_action


def test_fire_action_fires_at_the_scheduled_minute():
    assert FIRE("04:00", "04:00", "2026-07-08", "2026-07-09") == "fire"


def test_fire_action_catches_up_after_a_missed_minute():
    # The old `cur_time == scheduled` check skipped the day entirely here:
    # a restart or a slow tick straddling 04:00 meant no rename at all.
    assert FIRE("04:00", "04:01", "2026-07-08", "2026-07-09") == "fire"
    assert FIRE("04:00", "23:59", "2026-07-08", "2026-07-09") == "fire"


def test_fire_action_waits_until_the_time():
    assert FIRE("04:00", "03:59", "2026-07-08", "2026-07-09") == "skip"


def test_fire_action_never_double_fires():
    assert FIRE("04:00", "04:00", "2026-07-09", "2026-07-09") == "skip"
    assert FIRE("04:00", "18:00", "2026-07-09", "2026-07-09") == "skip"


def test_fire_action_first_run_stamps_instead_of_surprise_posting():
    # Guild configured at 23:00 with a 04:00 slot: record the day, start tomorrow.
    assert FIRE("04:00", "23:00", None, "2026-07-09") == "stamp"
    # ...but a first run before the slot just waits for it normally.
    assert FIRE("04:00", "01:00", None, "2026-07-09") == "skip"


def test_fire_action_first_run_fires_when_the_slot_arrives_on_time():
    # A weekly cadence must not swallow its very first Sunday: reaching the
    # slot on time (or within the grace hour) fires even with no history.
    assert FIRE("04:00", "04:00", None, "2026-07-09") == "fire"
    assert FIRE("04:00", "04:59", None, "2026-07-09") == "fire"
    assert FIRE("04:00", "05:01", None, "2026-07-09") == "stamp"


# ── pack_lines: unbounded lists must never 400 an ephemeral reply ─────────────

def test_pack_lines_short_list_untouched():
    assert bot_features.pack_lines(["a", "b"]) == "a\nb"


def test_pack_lines_drops_overflow_with_a_tail():
    lines = [f"line {i} " + "x" * 90 for i in range(40)]  # ~4k chars
    packed = bot_features.pack_lines(lines)
    assert len(packed) <= 2000
    assert "more*" in packed.splitlines()[-1]


# ── rename budget: refuse up front instead of hanging on Discord's 429 ────────

def test_rename_cooldown_free_then_limited_then_free_again():
    from datetime import timedelta
    import pytz
    gid = 777001
    bot_features._rename_times.pop(gid, None)
    assert bot_features.rename_cooldown_remaining(gid) == 0

    now = datetime.now(pytz.utc)
    bot_features._rename_times[gid] = [now - timedelta(minutes=1), now]   # 2 in-window
    wait = bot_features.rename_cooldown_remaining(gid)
    assert 1 <= wait <= 10                                                # blocked, real ETA

    bot_features._rename_times[gid] = [now - timedelta(minutes=11), now]  # oldest expired
    assert bot_features.rename_cooldown_remaining(gid) == 0


def test_record_guild_rename_trims_expired():
    from datetime import timedelta
    import pytz
    gid = 777002
    old = datetime.now(pytz.utc) - timedelta(minutes=30)
    bot_features._rename_times[gid] = [old, old]
    bot_features._record_guild_rename(gid)
    assert len(bot_features._rename_times[gid]) == 1   # stale entries dropped
