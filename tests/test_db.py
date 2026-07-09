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
