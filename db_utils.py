import logging
import os
import sqlite3
from datetime import datetime, timezone

log = logging.getLogger(__name__)

DB_FILE = os.getenv("DB_FILE", "/data/server_config.db")

_VALID_CONFIG_FIELDS = frozenset({
    "quote_channel",
    "icon_channel",
    "post_channel",
    "music_channel",
    "song_post_channel",
    "enable_daily_quote",
    "enable_daily_song",
    "enable_cooldown",
    "timezone",
    "quote_time",
    "song_time",
    "last_quote_date",
    "last_song_date",
})

_CREATE_CONFIG = """
CREATE TABLE IF NOT EXISTS server_config (
    guild_id           INTEGER PRIMARY KEY,
    quote_channel      INTEGER,
    icon_channel       INTEGER,
    post_channel       INTEGER,
    music_channel      INTEGER,
    song_post_channel  INTEGER,
    enable_daily_quote INTEGER DEFAULT 1,
    enable_daily_song  INTEGER DEFAULT 1,
    enable_cooldown    INTEGER DEFAULT 1,
    timezone           TEXT    DEFAULT 'US/Eastern',
    quote_time         TEXT    DEFAULT '4:00',
    song_time          TEXT    DEFAULT '10:00',
    last_quote_date    TEXT,
    last_song_date     TEXT
)
"""

_CREATE_HISTORY = """
CREATE TABLE IF NOT EXISTS picks_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    user_name   TEXT    NOT NULL,
    category    TEXT    NOT NULL,
    item        TEXT,
    picked_at   TEXT    NOT NULL
)
"""

_MIGRATIONS = [
    ("timezone",        "TEXT DEFAULT 'US/Eastern'"),
    ("quote_time",      "TEXT DEFAULT '4:00'"),
    ("song_time",       "TEXT DEFAULT '10:00'"),
    ("last_quote_date", "TEXT"),
    ("last_song_date",  "TEXT"),
    ("enable_cooldown", "INTEGER DEFAULT 1"),
]


def db_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with db_conn() as conn:
        conn.execute(_CREATE_CONFIG)
        conn.execute(_CREATE_HISTORY)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_history_guild_user
            ON picks_history(guild_id, user_id, category)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_history_guild_cat_time
            ON picks_history(guild_id, category, picked_at)
        """)
        for col, definition in _MIGRATIONS:
            try:
                conn.execute(f"ALTER TABLE server_config ADD COLUMN {col} {definition}")
            except sqlite3.OperationalError:
                pass
    log.info("Database ready at %s", DB_FILE)


def get_config(guild_id: int) -> sqlite3.Row:
    with db_conn() as conn:
        row = conn.execute(
            "SELECT * FROM server_config WHERE guild_id=?", (guild_id,)
        ).fetchone()
        if not row:
            conn.execute(
                "INSERT OR IGNORE INTO server_config (guild_id) VALUES (?)", (guild_id,)
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM server_config WHERE guild_id=?", (guild_id,)
            ).fetchone()
        return row


def set_config(guild_id: int, field: str, value) -> None:
    if field not in _VALID_CONFIG_FIELDS:
        raise ValueError(f"Invalid config field: {field!r}")
    with db_conn() as conn:
        conn.execute(
            f"UPDATE server_config SET {field}=? WHERE guild_id=?", (value, guild_id)
        )
        conn.commit()


def show_config(guild_id: int) -> str:
    c = get_config(guild_id)
    return "\n".join([
        f"Guild ID:          {c['guild_id']}",
        f"Quote Channel:     {c['quote_channel']     or 'Not Set'}",
        f"Icon Channel:      {c['icon_channel']      or 'Not Set'}",
        f"Post Channel:      {c['post_channel']      or 'Not Set'}",
        f"Music Channel:     {c['music_channel']     or 'Not Set'}",
        f"Song Post Channel: {c['song_post_channel'] or 'Not Set'}",
        f"Quote Feature:     {'Enabled' if c['enable_daily_quote'] else 'Disabled'}",
        f"Song Feature:      {'Enabled' if c['enable_daily_song']  else 'Disabled'}",
        f"Cooldown:          {'Enabled' if c['enable_cooldown']    else 'Disabled'}",
        f"Timezone:          {c['timezone']   or 'US/Eastern'}",
        f"Quote Time:        {c['quote_time'] or '4:00'}",
        f"Song Time:         {c['song_time']  or '10:00'}",
    ])


def log_pick(guild_id: int, user_id: int, user_name: str, category: str, item: str) -> None:
    picked_at = datetime.now(timezone.utc).isoformat()
    with db_conn() as conn:
        conn.execute(
            "INSERT INTO picks_history (guild_id, user_id, user_name, category, item, picked_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (guild_id, user_id, user_name, category, item, picked_at),
        )
        conn.commit()


def get_today_pick_counts(guild_id: int, category: str, since_utc: str) -> dict[int, int]:
    """
    Return {user_id: picks_today} for *category* since *since_utc* (ISO timestamp).
    since_utc should be midnight of today in the guild's timezone, converted to UTC.
    """
    with db_conn() as conn:
        rows = conn.execute("""
            SELECT user_id, COUNT(*) AS count
            FROM picks_history
            WHERE guild_id=? AND category=? AND picked_at >= ?
            GROUP BY user_id
        """, (guild_id, category, since_utc)).fetchall()
    return {row["user_id"]: row["count"] for row in rows}


def get_user_last_picks(guild_id: int, user_id: int) -> dict[str, str]:
    with db_conn() as conn:
        rows = conn.execute("""
            SELECT category, MAX(picked_at) AS last_picked
            FROM picks_history
            WHERE guild_id=? AND user_id=?
            GROUP BY category
        """, (guild_id, user_id)).fetchall()
    return {row["category"]: row["last_picked"] for row in rows}
