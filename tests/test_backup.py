import os

import db_utils


def test_backup_creates_snapshot_and_prunes(gid):
    db_utils.set_config(gid, "quote_time", "9:00")   # some data to snapshot

    path = db_utils.backup_database(keep=3)
    assert path and os.path.exists(path)
    backup_dir = os.path.dirname(path)

    # A backup is a valid, readable SQLite copy carrying the data.
    import sqlite3
    con = sqlite3.connect(path)
    row = con.execute("SELECT quote_time FROM server_config WHERE guild_id=?", (gid,)).fetchone()
    con.close()
    assert row and row[0] == "9:00"

    # Retention: seed 5 fake older backups, then prune to keep=3.
    for d in ("2000-01-01", "2000-01-02", "2000-01-03", "2000-01-04", "2000-01-05"):
        open(os.path.join(backup_dir, f"backup-{d}.db"), "w").close()
    db_utils.backup_database(keep=3)
    remaining = sorted(f for f in os.listdir(backup_dir) if f.startswith("backup-"))
    assert len(remaining) == 3
    # oldest are dropped; today's (newest by name) survives
    assert "backup-2000-01-01.db" not in remaining


def test_backup_same_day_overwrites(gid):
    p1 = db_utils.backup_database()
    p2 = db_utils.backup_database()   # must not fail on an existing same-day file
    assert p1 == p2 and os.path.exists(p2)
