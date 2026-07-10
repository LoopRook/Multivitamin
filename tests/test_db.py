import pytest
import db_utils


def test_migrations_idempotent():
    db_utils.init_db()
    db_utils.init_db()  # running twice must not raise


def test_config_roundtrip(gid):
    for field, val in [
        ("bracket_source_channel", 123),
        ("pre_bracket_name", "Old Name"),
        ("bracket_pacing", "daily"),
        ("timezone", "UTC"),
    ]:
        db_utils.set_config(gid, field, val)
        assert db_utils.get_config(gid)[field] == val


def test_invalid_config_field_rejected(gid):
    with pytest.raises(ValueError):
        db_utils.set_config(gid, "not_a_field", 1)


def test_forward_nomination_dedup(gid):
    assert db_utils.record_forward_nomination(gid, 7, "Quote", 1) is True   # first
    assert db_utils.record_forward_nomination(gid, 7, "Quote", 2) is False  # duplicate
    assert db_utils.record_forward_nomination(gid, 7, "Other", 3) is True   # different quote
    assert db_utils.record_forward_nomination(gid, 8, "Quote", 4) is True   # different channel


def test_rename_post_lookup(gid):
    db_utils.store_rename_post(gid, 555, 10, "Hello world", "user", 1, None)
    row = db_utils.get_rename_post_by_message_id(gid, 555)
    assert row is not None and row["quote"] == "Hello world"
    assert db_utils.get_rename_post_by_message_id(gid, 111) is None


def test_custom_feature_schedule(gid):
    assert db_utils.add_custom_feature(gid, "Meme of the Day", None, "media", 1, 2, "12:00", command="meme")
    feat = db_utils.get_custom_feature_by_command(gid, "meme")
    assert feat["interval_days"] == 1 and not feat["weekdays"]  # daily by default

    db_utils.set_custom_feature_schedule(feat["id"], 1, "6")  # every Sunday
    assert db_utils.get_custom_feature_by_id(feat["id"])["weekdays"] == "6"

    db_utils.set_custom_feature_schedule(feat["id"], 3, None)  # every 3 days (clears weekdays)
    f3 = db_utils.get_custom_feature_by_id(feat["id"])
    assert f3["weekdays"] is None and f3["interval_days"] == 3


def test_reset_guild_wipes_everything(gid):
    db_utils.set_config(gid, "post_channel", 99)
    db_utils.add_custom_feature(gid, "Meme", None, "media", 1, 2, "12:00", command="meme")
    db_utils.store_rename_post(gid, 1, 2, "q", "u", 3, None)
    db_utils.record_forward_nomination(gid, 4, "q", 1)
    db_utils.add_season(gid, "S", "2026-10-01T00:00:00+00:00", "2026-10-31T23:59:59+00:00")
    bid = db_utils.create_bracket(gid, 2026, 4, 24, label="2026")
    eid = db_utils.create_bracket_entry(bid, 1, "q", "u", 5)
    db_utils.create_bracket_matchup(bid, 1, 0, eid, eid)
    db_utils.add_bot_admin(gid, 555, 1)

    db_utils.reset_guild(gid)

    assert db_utils.get_custom_features(gid) == []
    assert db_utils.get_rename_post_by_message_id(gid, 1) is None
    assert db_utils.get_seasons(gid) == []
    assert db_utils.get_active_bracket(gid) is None
    assert db_utils.get_bracket_history(gid) == []
    assert db_utils.get_bot_admins(gid) == []
    assert db_utils.record_forward_nomination(gid, 4, "q", 2) is True  # nominations cleared → treated as first again
    # config row is gone; next access recreates defaults
    assert db_utils.get_config(gid)["post_channel"] is None


def test_bracket_champion_history_excludes_tests(gid):
    bid = db_utils.create_bracket(gid, 2026, 8, 24, label="2026", pacing="round")
    db_utils.complete_bracket(bid)
    db_utils.set_bracket_champion(bid, "The Champion", "Sam")
    tid = db_utils.create_bracket(gid, 0, 4, 24, label="TEST")
    db_utils.complete_bracket(tid)
    db_utils.set_bracket_champion(tid, "Testy", "x")

    hist = db_utils.get_bracket_history(gid)
    assert len(hist) == 1
    assert hist[0]["champion_quote"] == "The Champion"
    assert hist[0]["champion_user"] == "Sam"
