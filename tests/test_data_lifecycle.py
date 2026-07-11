from datetime import datetime, timedelta, timezone

import db_utils


def _iso(days_ago):
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


def test_mark_and_clear_removed(gid):
    db_utils.get_config(gid)
    db_utils.mark_guild_removed(gid)
    assert db_utils.get_config(gid)["removed_at"]
    db_utils.clear_guild_removed(gid)
    assert db_utils.get_config(gid)["removed_at"] is None


def test_purge_only_past_grace(gid):
    db_utils.get_config(gid)
    db_utils.set_config(gid, "quote_time", "9:00")
    db_utils.mark_guild_removed(gid)

    # Cutoff 30 days ago: this guild was removed just now, so it is NOT purged.
    assert db_utils.purge_expired_guilds(_iso(30)) == []
    # Force-age the stamp to 40 days ago, then a 30-day cutoff catches it.
    db_utils.set_config(gid, "removed_at", _iso(40))
    assert gid in db_utils.purge_expired_guilds(_iso(30))


def test_purge_ignores_present_guilds(gid):
    db_utils.get_config(gid)
    db_utils.set_config(gid, "quote_time", "9:00")   # present, removed_at is NULL
    assert db_utils.purge_expired_guilds(_iso(0)) == []
    assert db_utils.get_config(gid)["quote_time"] == "9:00"   # untouched
