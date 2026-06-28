import logging
import math
import os
import sqlite3
from datetime import datetime, timezone

log = logging.getLogger(__name__)

DB_FILE = os.getenv("DB_FILE", "/data/server_config.db")

_VALID_CONFIG_FIELDS = frozenset({
    "quote_channel", "icon_channel", "post_channel", "music_channel",
    "song_post_channel", "enable_daily_quote", "enable_daily_song",
    "enable_cooldown", "enable_voting",
    "timezone", "quote_time", "song_time", "last_quote_date", "last_song_date",
    "vote_emoji", "bracket_emoji_a", "bracket_emoji_b",
    "bracket_channel", "bracket_size", "bracket_voting_hours", "voting_enabled_at",
})

_CREATE_CONFIG = """
CREATE TABLE IF NOT EXISTS server_config (
    guild_id              INTEGER PRIMARY KEY,
    quote_channel         INTEGER,
    icon_channel          INTEGER,
    post_channel          INTEGER,
    music_channel         INTEGER,
    song_post_channel     INTEGER,
    enable_daily_quote    INTEGER DEFAULT 1,
    enable_daily_song     INTEGER DEFAULT 1,
    enable_cooldown       INTEGER DEFAULT 1,
    enable_voting         INTEGER DEFAULT 0,
    timezone              TEXT    DEFAULT 'US/Eastern',
    quote_time            TEXT    DEFAULT '4:00',
    song_time             TEXT    DEFAULT '10:00',
    last_quote_date       TEXT,
    last_song_date        TEXT,
    vote_emoji            TEXT    DEFAULT '👍',
    bracket_emoji_a       TEXT    DEFAULT '1️⃣',
    bracket_emoji_b       TEXT    DEFAULT '2️⃣',
    bracket_channel       INTEGER,
    bracket_size          INTEGER DEFAULT 8,
    bracket_voting_hours  INTEGER DEFAULT 24,
    voting_enabled_at     TEXT
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

_CREATE_RENAME_POSTS = """
CREATE TABLE IF NOT EXISTS rename_posts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    message_id  INTEGER NOT NULL,
    channel_id  INTEGER NOT NULL,
    quote       TEXT    NOT NULL,
    quote_user  TEXT,
    quote_uid   INTEGER,
    posted_at   TEXT    NOT NULL
)
"""

_CREATE_BRACKETS = """
CREATE TABLE IF NOT EXISTS brackets (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id      INTEGER NOT NULL,
    year          INTEGER NOT NULL,
    size          INTEGER NOT NULL,
    status        TEXT    DEFAULT 'active',
    current_round INTEGER DEFAULT 1,
    voting_hours  INTEGER DEFAULT 24,
    created_at    TEXT    NOT NULL
)
"""

_CREATE_BRACKET_ENTRIES = """
CREATE TABLE IF NOT EXISTS bracket_entries (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    bracket_id       INTEGER NOT NULL,
    seed             INTEGER NOT NULL,
    quote            TEXT    NOT NULL,
    quote_user       TEXT,
    season_reactions INTEGER DEFAULT 0,
    FOREIGN KEY (bracket_id) REFERENCES brackets(id)
)
"""

_CREATE_BRACKET_MATCHUPS = """
CREATE TABLE IF NOT EXISTS bracket_matchups (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    bracket_id      INTEGER NOT NULL,
    round           INTEGER NOT NULL,
    match_num       INTEGER NOT NULL,
    entry_a_id      INTEGER NOT NULL,
    entry_b_id      INTEGER NOT NULL,
    message_id      INTEGER,
    channel_id      INTEGER,
    winner_entry_id INTEGER,
    ends_at         TEXT,
    status          TEXT DEFAULT 'active',
    FOREIGN KEY (bracket_id)  REFERENCES brackets(id),
    FOREIGN KEY (entry_a_id)  REFERENCES bracket_entries(id),
    FOREIGN KEY (entry_b_id)  REFERENCES bracket_entries(id)
)
"""

_MIGRATIONS = [
    ("timezone",             "TEXT DEFAULT 'US/Eastern'"),
    ("quote_time",           "TEXT DEFAULT '4:00'"),
    ("song_time",            "TEXT DEFAULT '10:00'"),
    ("last_quote_date",      "TEXT"),
    ("last_song_date",       "TEXT"),
    ("enable_cooldown",      "INTEGER DEFAULT 1"),
    ("enable_voting",        "INTEGER DEFAULT 0"),
    ("vote_emoji",           "TEXT DEFAULT '👍'"),
    ("bracket_emoji_a",      "TEXT DEFAULT '1️⃣'"),
    ("bracket_emoji_b",      "TEXT DEFAULT '2️⃣'"),
    ("bracket_channel",      "INTEGER"),
    ("bracket_size",         "INTEGER DEFAULT 8"),
    ("bracket_voting_hours", "INTEGER DEFAULT 24"),
    ("voting_enabled_at",    "TEXT"),
]


def db_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with db_conn() as conn:
        conn.execute(_CREATE_CONFIG)
        conn.execute(_CREATE_HISTORY)
        conn.execute(_CREATE_RENAME_POSTS)
        conn.execute(_CREATE_BRACKETS)
        conn.execute(_CREATE_BRACKET_ENTRIES)
        conn.execute(_CREATE_BRACKET_MATCHUPS)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_history_guild_user ON picks_history(guild_id, user_id, category)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_history_guild_cat_time ON picks_history(guild_id, category, picked_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_rename_posts_guild ON rename_posts(guild_id, posted_at)")
        for col, definition in _MIGRATIONS:
            try:
                conn.execute(f"ALTER TABLE server_config ADD COLUMN {col} {definition}")
            except sqlite3.OperationalError:
                pass
    log.info("Database ready at %s", DB_FILE)


def get_config(guild_id: int) -> sqlite3.Row:
    with db_conn() as conn:
        row = conn.execute("SELECT * FROM server_config WHERE guild_id=?", (guild_id,)).fetchone()
        if not row:
            conn.execute("INSERT OR IGNORE INTO server_config (guild_id) VALUES (?)", (guild_id,))
            conn.commit()
            row = conn.execute("SELECT * FROM server_config WHERE guild_id=?", (guild_id,)).fetchone()
        return row


def set_config(guild_id: int, field: str, value) -> None:
    if field not in _VALID_CONFIG_FIELDS:
        raise ValueError(f"Invalid config field: {field!r}")
    with db_conn() as conn:
        conn.execute(f"UPDATE server_config SET {field}=? WHERE guild_id=?", (value, guild_id))
        conn.commit()


def show_config(guild_id: int) -> str:
    c = get_config(guild_id)
    return "\n".join([
        f"Guild ID:            {c['guild_id']}",
        f"Quote Channel:       {c['quote_channel']        or 'Not Set'}",
        f"Icon Channel:        {c['icon_channel']         or 'Not Set'}",
        f"Post Channel:        {c['post_channel']         or 'Not Set'}",
        f"Music Channel:       {c['music_channel']        or 'Not Set'}",
        f"Song Post Channel:   {c['song_post_channel']    or 'Not Set'}",
        f"Bracket Channel:     {c['bracket_channel']      or 'Not Set'}",
        f"Quote Feature:       {'Enabled' if c['enable_daily_quote'] else 'Disabled'}",
        f"Song Feature:        {'Enabled' if c['enable_daily_song']  else 'Disabled'}",
        f"Cooldown:            {'Enabled' if c['enable_cooldown']    else 'Disabled'}",
        f"Voting:              {'Enabled' if c['enable_voting']      else 'Disabled'}",
        f"Vote Emoji:          {c['vote_emoji']           or '👍'}",
        f"Bracket Emoji A:     {c['bracket_emoji_a']     or '1️⃣'}",
        f"Bracket Emoji B:     {c['bracket_emoji_b']     or '2️⃣'}",
        f"Bracket Size:        {c['bracket_size']         or 8}",
        f"Bracket Vote Hours:  {c['bracket_voting_hours'] or 24}",
        f"Timezone:            {c['timezone']             or 'US/Eastern'}",
        f"Quote Time:          {c['quote_time']           or '4:00'}",
        f"Song Time:           {c['song_time']            or '10:00'}",
    ])


# ── picks_history ────────────────────────────────────────────────────────────

def log_pick(guild_id: int, user_id: int, user_name: str, category: str, item: str) -> None:
    with db_conn() as conn:
        conn.execute(
            "INSERT INTO picks_history (guild_id, user_id, user_name, category, item, picked_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (guild_id, user_id, user_name, category, item, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()


def get_today_pick_counts(guild_id: int, category: str, since_utc: str) -> dict[int, int]:
    with db_conn() as conn:
        rows = conn.execute(
            "SELECT user_id, COUNT(*) AS count FROM picks_history "
            "WHERE guild_id=? AND category=? AND picked_at >= ? GROUP BY user_id",
            (guild_id, category, since_utc),
        ).fetchall()
    return {row["user_id"]: row["count"] for row in rows}


def get_user_last_picks(guild_id: int, user_id: int) -> dict[str, str]:
    with db_conn() as conn:
        rows = conn.execute(
            "SELECT category, MAX(picked_at) AS last_picked FROM picks_history "
            "WHERE guild_id=? AND user_id=? GROUP BY category",
            (guild_id, user_id),
        ).fetchall()
    return {row["category"]: row["last_picked"] for row in rows}


# ── rename_posts ─────────────────────────────────────────────────────────────

def store_rename_post(
    guild_id: int, message_id: int, channel_id: int,
    quote: str, quote_user: str | None, quote_uid: int | None,
) -> None:
    posted_at = datetime.now(timezone.utc).isoformat()
    with db_conn() as conn:
        conn.execute(
            "INSERT INTO rename_posts (guild_id, message_id, channel_id, quote, quote_user, quote_uid, posted_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (guild_id, message_id, channel_id, quote, quote_user, quote_uid, posted_at),
        )
        conn.commit()


def get_rename_posts_for_year(
    guild_id: int, year_start_utc: str, year_end_utc: str, voting_enabled_at: str | None
) -> list[sqlite3.Row]:
    since = max(year_start_utc, voting_enabled_at) if voting_enabled_at else year_start_utc
    with db_conn() as conn:
        return conn.execute(
            "SELECT * FROM rename_posts WHERE guild_id=? AND posted_at >= ? AND posted_at <= ? ORDER BY posted_at",
            (guild_id, since, year_end_utc),
        ).fetchall()


# ── brackets ─────────────────────────────────────────────────────────────────

def create_bracket(guild_id: int, year: int, size: int, voting_hours: int) -> int:
    with db_conn() as conn:
        cur = conn.execute(
            "INSERT INTO brackets (guild_id, year, size, voting_hours, created_at) VALUES (?, ?, ?, ?, ?)",
            (guild_id, year, size, voting_hours, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        return cur.lastrowid


def get_active_bracket(guild_id: int) -> sqlite3.Row | None:
    with db_conn() as conn:
        return conn.execute(
            "SELECT * FROM brackets WHERE guild_id=? AND status='active' ORDER BY created_at DESC LIMIT 1",
            (guild_id,),
        ).fetchone()


def create_bracket_entry(bracket_id: int, seed: int, quote: str, quote_user: str | None, season_reactions: int) -> int:
    with db_conn() as conn:
        cur = conn.execute(
            "INSERT INTO bracket_entries (bracket_id, seed, quote, quote_user, season_reactions) VALUES (?, ?, ?, ?, ?)",
            (bracket_id, seed, quote, quote_user, season_reactions),
        )
        conn.commit()
        return cur.lastrowid


def get_bracket_entry(entry_id: int) -> sqlite3.Row | None:
    with db_conn() as conn:
        return conn.execute("SELECT * FROM bracket_entries WHERE id=?", (entry_id,)).fetchone()


def create_bracket_matchup(
    bracket_id: int, round_num: int, match_num: int,
    entry_a_id: int, entry_b_id: int,
) -> int:
    with db_conn() as conn:
        cur = conn.execute(
            "INSERT INTO bracket_matchups (bracket_id, round, match_num, entry_a_id, entry_b_id) "
            "VALUES (?, ?, ?, ?, ?)",
            (bracket_id, round_num, match_num, entry_a_id, entry_b_id),
        )
        conn.commit()
        return cur.lastrowid


def update_matchup_posted(matchup_id: int, message_id: int, channel_id: int, ends_at: str) -> None:
    with db_conn() as conn:
        conn.execute(
            "UPDATE bracket_matchups SET message_id=?, channel_id=?, ends_at=? WHERE id=?",
            (message_id, channel_id, ends_at, matchup_id),
        )
        conn.commit()


def get_active_round_matchups(bracket_id: int, round_num: int) -> list[sqlite3.Row]:
    with db_conn() as conn:
        return conn.execute(
            "SELECT * FROM bracket_matchups WHERE bracket_id=? AND round=? ORDER BY match_num",
            (bracket_id, round_num),
        ).fetchall()


def set_matchup_winner(matchup_id: int, winner_entry_id: int) -> None:
    with db_conn() as conn:
        conn.execute(
            "UPDATE bracket_matchups SET winner_entry_id=?, status='complete' WHERE id=?",
            (winner_entry_id, matchup_id),
        )
        conn.commit()


def get_round_winners_ordered(bracket_id: int, round_num: int) -> list[sqlite3.Row]:
    """Winners from a completed round, ordered by match_num for correct bracket pairing."""
    with db_conn() as conn:
        rows = conn.execute(
            "SELECT winner_entry_id FROM bracket_matchups "
            "WHERE bracket_id=? AND round=? AND status='complete' ORDER BY match_num",
            (bracket_id, round_num),
        ).fetchall()
    return [row["winner_entry_id"] for row in rows]


def advance_bracket_round(bracket_id: int) -> int:
    with db_conn() as conn:
        conn.execute("UPDATE brackets SET current_round = current_round + 1 WHERE id=?", (bracket_id,))
        conn.commit()
        row = conn.execute("SELECT current_round FROM brackets WHERE id=?", (bracket_id,)).fetchone()
        return row["current_round"]


def complete_bracket(bracket_id: int) -> None:
    with db_conn() as conn:
        conn.execute("UPDATE brackets SET status='complete' WHERE id=?", (bracket_id,))
        conn.commit()
