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
