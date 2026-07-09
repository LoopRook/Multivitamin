import logging
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
    "bracket_channel", "bracket_size", "bracket_voting_hours", "voting_enabled_at",
    "bracket_pacing", "bracket_source_channel", "pre_bracket_name",
    "quote_interval_days", "quote_weekdays",
    "credit_style", "credit_mentions",
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
    bracket_channel       INTEGER,
    bracket_size          INTEGER DEFAULT 8,
    bracket_voting_hours  INTEGER DEFAULT 24,
    voting_enabled_at     TEXT,
    bracket_pacing        TEXT    DEFAULT 'round',
    bracket_source_channel INTEGER,
    pre_bracket_name      TEXT,
    quote_interval_days   INTEGER DEFAULT 1,  -- rename cadence: every N days (interval mode)
    quote_weekdays        TEXT                -- comma list of weekday nums 0=Mon..6=Sun (weekday mode; overrides interval)
)
"""

_CREATE_BOT_ADMINS = """
CREATE TABLE IF NOT EXISTS bot_admins (
    guild_id    INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    added_by    INTEGER,
    added_at    TEXT    NOT NULL,
    PRIMARY KEY (guild_id, user_id)
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

_CREATE_FORWARD_NOMINATIONS = """
CREATE TABLE IF NOT EXISTS forward_nominations (
    guild_id    INTEGER NOT NULL,
    channel_id  INTEGER NOT NULL,
    quote       TEXT    NOT NULL,
    message_id  INTEGER NOT NULL,   -- the first forward's message id (for reference)
    created_at  TEXT    NOT NULL,
    PRIMARY KEY (guild_id, channel_id, quote)
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
    icon_user   TEXT,              -- who posted the icon this card was built from
    icon_uid    INTEGER,
    image_url   TEXT,
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
    created_at    TEXT    NOT NULL,
    label         TEXT,             -- display name, e.g. '2026', 'Halloween 2026', 'TEST'
    range_start   TEXT,             -- ISO UTC start of the seeding window (NULL for test)
    range_end     TEXT,             -- ISO UTC end of the seeding window (NULL for test)
    pacing        TEXT,             -- 'round'|'daily' snapshot at launch (NULL for legacy → falls back to live config)
    champion_quote TEXT,            -- winning entry, recorded at crowning (for /bracket history)
    champion_user  TEXT,            -- who submitted the winning entry
    champion_uid   INTEGER          -- ...and their user id, so history can re-resolve/mention them
)
"""

_CREATE_SEASONS = """
CREATE TABLE IF NOT EXISTS seasons (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    name        TEXT    NOT NULL,
    start_at    TEXT    NOT NULL,   -- ISO UTC
    end_at      TEXT    NOT NULL,   -- ISO UTC
    created_at  TEXT    NOT NULL
)
"""

_CREATE_CUSTOM_FEATURES = """
CREATE TABLE IF NOT EXISTS custom_features (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id       INTEGER NOT NULL,
    name           TEXT    NOT NULL,   -- display caption AND key, e.g. 'Meme of the Day'
    emoji          TEXT,               -- optional prefix emoji
    content_type   TEXT    NOT NULL,   -- 'media' | 'link' | 'music' | 'text'
    source_channel INTEGER NOT NULL,   -- channel to sample from
    post_channel   INTEGER NOT NULL,   -- channel to post into
    post_time      TEXT    NOT NULL,   -- 'HH:MM' in the guild timezone
    enabled        INTEGER DEFAULT 1,
    last_run_date  TEXT,               -- 'YYYY-MM-DD' in guild tz (double-fire guard)
    created_at     TEXT    NOT NULL,
    command        TEXT,               -- optional per-guild slash-command slug (null = none)
    run_access     TEXT DEFAULT 'admin',  -- who may run /<command>: 'admin' | 'everyone' | 'roles'
    run_roles      TEXT,               -- comma-separated role IDs (when run_access='roles')
    interval_days  INTEGER DEFAULT 1,  -- cadence: every N days (interval mode)
    weekdays       TEXT                -- comma list of weekday nums 0=Mon..6=Sun (weekday mode; overrides interval)
)
"""

_CREATE_BRACKET_ENTRIES = """
CREATE TABLE IF NOT EXISTS bracket_entries (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    bracket_id       INTEGER NOT NULL,
    seed             INTEGER NOT NULL,
    quote            TEXT    NOT NULL,
    quote_user       TEXT,
    quote_uid        INTEGER,
    icon_user        TEXT,          -- credit for the card's icon (carried from rename_posts)
    icon_uid         INTEGER,
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

# Columns added after initial schema — safe to run against existing DBs
_CONFIG_MIGRATIONS = [
    ("timezone",             "TEXT DEFAULT 'US/Eastern'"),
    ("quote_time",           "TEXT DEFAULT '4:00'"),
    ("song_time",            "TEXT DEFAULT '10:00'"),
    ("last_quote_date",      "TEXT"),
    ("last_song_date",       "TEXT"),
    ("enable_cooldown",      "INTEGER DEFAULT 1"),
    ("enable_voting",        "INTEGER DEFAULT 0"),
    ("bracket_channel",      "INTEGER"),
    ("bracket_size",         "INTEGER DEFAULT 8"),
    ("bracket_voting_hours", "INTEGER DEFAULT 24"),
    ("voting_enabled_at",    "TEXT"),
    ("bracket_pacing",       "TEXT DEFAULT 'round'"),
    ("song_migrated",        "INTEGER DEFAULT 0"),
    ("bracket_source_channel", "INTEGER"),
    ("pre_bracket_name",       "TEXT"),
    ("quote_interval_days",    "INTEGER DEFAULT 1"),
    ("quote_weekdays",         "TEXT"),
    ("credit_style",           "TEXT DEFAULT 'nickname'"),  # 'nickname' | 'username'
    ("credit_mentions",        "INTEGER DEFAULT 0"),        # 1 = @mention contributors in posts
]

_RENAME_POSTS_MIGRATIONS = [
    ("image_url", "TEXT"),
    ("icon_user", "TEXT"),
    ("icon_uid",  "INTEGER"),
]

_BRACKET_MIGRATIONS = [
    ("label",       "TEXT"),
    ("range_start", "TEXT"),
    ("range_end",   "TEXT"),
    ("pacing",      "TEXT"),
    ("champion_quote", "TEXT"),
    ("champion_user",  "TEXT"),
    ("champion_uid",   "INTEGER"),
]

_BRACKET_ENTRIES_MIGRATIONS = [
    ("quote_uid", "INTEGER"),
    ("icon_user", "TEXT"),
    ("icon_uid",  "INTEGER"),
]

# custom_features already ships in live DBs, so new columns must be ALTER-added.
_CUSTOM_FEATURES_MIGRATIONS = [
    ("command",       "TEXT"),
    ("run_access",    "TEXT DEFAULT 'admin'"),
    ("run_roles",     "TEXT"),
    ("interval_days", "INTEGER DEFAULT 1"),
    ("weekdays",      "TEXT"),
]


def db_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with db_conn() as conn:
        conn.execute(_CREATE_CONFIG)
        conn.execute(_CREATE_BOT_ADMINS)
        conn.execute(_CREATE_HISTORY)
        conn.execute(_CREATE_RENAME_POSTS)
        conn.execute(_CREATE_FORWARD_NOMINATIONS)
        conn.execute(_CREATE_BRACKETS)
        conn.execute(_CREATE_BRACKET_ENTRIES)
        conn.execute(_CREATE_BRACKET_MATCHUPS)
        conn.execute(_CREATE_SEASONS)
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_seasons_guild_name ON seasons(guild_id, name COLLATE NOCASE)")
        conn.execute(_CREATE_CUSTOM_FEATURES)
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_custom_features_guild_name ON custom_features(guild_id, name COLLATE NOCASE)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_history_guild_user ON picks_history(guild_id, user_id, category)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_history_guild_cat_time ON picks_history(guild_id, category, picked_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_rename_posts_guild ON rename_posts(guild_id, posted_at)")
        for col, definition in _CONFIG_MIGRATIONS:
            try:
                conn.execute(f"ALTER TABLE server_config ADD COLUMN {col} {definition}")
            except sqlite3.OperationalError:
                pass
        for col, definition in _RENAME_POSTS_MIGRATIONS:
            try:
                conn.execute(f"ALTER TABLE rename_posts ADD COLUMN {col} {definition}")
            except sqlite3.OperationalError:
                pass
        for col, definition in _BRACKET_MIGRATIONS:
            try:
                conn.execute(f"ALTER TABLE brackets ADD COLUMN {col} {definition}")
            except sqlite3.OperationalError:
                pass
        for col, definition in _BRACKET_ENTRIES_MIGRATIONS:
            try:
                conn.execute(f"ALTER TABLE bracket_entries ADD COLUMN {col} {definition}")
            except sqlite3.OperationalError:
                pass
        for col, definition in _CUSTOM_FEATURES_MIGRATIONS:
            try:
                conn.execute(f"ALTER TABLE custom_features ADD COLUMN {col} {definition}")
            except sqlite3.OperationalError:
                pass
        synced = _migrate_song_to_slug_model(conn)
    log.info("Database ready at %s", DB_FILE)
    return synced


def _migrate_song_to_slug_model(conn: sqlite3.Connection) -> list[int]:
    """
    Transition each guild to the slug-command model, once (gated by
    song_migrated < 2). Per guild:
      • First time (song_migrated=0): create a 'Song of the Day' music feature
        from the guild's legacy song config, if any.
      • Wipe every OTHER custom feature (they predate required slugs — the admin
        recreates them cleanly with the slugs they want).
      • Give the surviving 'Song of the Day' feature the reserved-free slug 'song'
        (the old built-in /song command is gone, freeing the name).
      • Mark song_migrated=2.
    Returns the guild IDs that changed, so the caller can re-sync their commands.
    """
    rows = conn.execute(
        "SELECT guild_id, music_channel, song_post_channel, song_time, "
        "enable_daily_song, last_song_date, COALESCE(song_migrated,0) AS sm "
        "FROM server_config WHERE COALESCE(song_migrated,0) < 2"
    ).fetchall()
    now = datetime.now(timezone.utc).isoformat()
    changed: list[int] = []
    for r in rows:
        gid = r["guild_id"]
        if r["sm"] == 0 and r["music_channel"] and r["song_post_channel"]:
            conn.execute(
                "INSERT OR IGNORE INTO custom_features "
                "(guild_id, name, emoji, content_type, source_channel, post_channel, "
                " post_time, enabled, last_run_date, created_at, command) "
                "VALUES (?, 'Song of the Day', '🎵', 'music', ?, ?, ?, ?, ?, ?, 'song')",
                (gid, r["music_channel"], r["song_post_channel"], r["song_time"] or "10:00",
                 1 if (r["enable_daily_song"] is None or r["enable_daily_song"]) else 0,
                 r["last_song_date"], now),
            )
        # Wipe pre-slug features (everything except the song feature).
        conn.execute(
            "DELETE FROM custom_features WHERE guild_id=? AND name<>'Song of the Day' COLLATE NOCASE",
            (gid,),
        )
        # Ensure the song feature owns the 'song' slug.
        conn.execute(
            "UPDATE custom_features SET command='song' "
            "WHERE guild_id=? AND name='Song of the Day' COLLATE NOCASE "
            "AND (command IS NULL OR command='')",
            (gid,),
        )
        conn.execute("UPDATE server_config SET song_migrated=2 WHERE guild_id=?", (gid,))
        changed.append(gid)
    conn.commit()
    return changed


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


def show_config(guild_id: int) -> dict:
    """Return raw config row as a dict for display. Channel name resolution happens in bot_features."""
    return dict(get_config(guild_id))


# ── bot_admins ────────────────────────────────────────────────────────────────
# Bot-level admins are stored per guild. They grant admin command access to users
# who don't hold Discord's Manage Server permission. Managing the roster itself
# still requires Manage Server (see main.py).

def add_bot_admin(guild_id: int, user_id: int, added_by: int | None) -> bool:
    """Grant bot-admin to a user. Returns False if they were already an admin."""
    with db_conn() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO bot_admins (guild_id, user_id, added_by, added_at) VALUES (?, ?, ?, ?)",
            (guild_id, user_id, added_by, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        return cur.rowcount > 0


def remove_bot_admin(guild_id: int, user_id: int) -> bool:
    """Revoke bot-admin from a user. Returns False if they weren't an admin."""
    with db_conn() as conn:
        cur = conn.execute(
            "DELETE FROM bot_admins WHERE guild_id=? AND user_id=?",
            (guild_id, user_id),
        )
        conn.commit()
        return cur.rowcount > 0


def get_bot_admins(guild_id: int) -> list[int]:
    """Return the user IDs of all bot-admins for a guild."""
    with db_conn() as conn:
        rows = conn.execute(
            "SELECT user_id FROM bot_admins WHERE guild_id=? ORDER BY added_at",
            (guild_id,),
        ).fetchall()
    return [row["user_id"] for row in rows]


def is_bot_admin(guild_id: int, user_id: int) -> bool:
    with db_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM bot_admins WHERE guild_id=? AND user_id=?",
            (guild_id, user_id),
        ).fetchone()
    return row is not None


# ── picks_history ─────────────────────────────────────────────────────────────

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


# ── rename_posts ──────────────────────────────────────────────────────────────

def store_rename_post(
    guild_id: int, message_id: int, channel_id: int,
    quote: str, quote_user: str | None, quote_uid: int | None,
    image_url: str | None = None,
    icon_user: str | None = None, icon_uid: int | None = None,
) -> None:
    with db_conn() as conn:
        conn.execute(
            "INSERT INTO rename_posts "
            "(guild_id, message_id, channel_id, quote, quote_user, quote_uid, "
            " icon_user, icon_uid, image_url, posted_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (guild_id, message_id, channel_id, quote, quote_user, quote_uid,
             icon_user, icon_uid, image_url, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()


def get_rename_posts_in_range(
    guild_id: int, start_utc: str, end_utc: str, floor_utc: str | None = None,
) -> list[sqlite3.Row]:
    """
    Tracked rename posts with posted_at in [start_utc, end_utc].
    *floor_utc* (typically voting_enabled_at) raises the lower bound so posts
    from before tracking began are never included.
    """
    since = max(start_utc, floor_utc) if floor_utc else start_utc
    with db_conn() as conn:
        return conn.execute(
            "SELECT * FROM rename_posts "
            "WHERE guild_id=? AND posted_at >= ? AND posted_at <= ? ORDER BY posted_at",
            (guild_id, since, end_utc),
        ).fetchall()


def get_rename_post_by_message_id(guild_id: int, message_id: int) -> sqlite3.Row | None:
    """Look up a tracked rename post by the Discord message id (any tracked copy)."""
    with db_conn() as conn:
        return conn.execute(
            "SELECT * FROM rename_posts WHERE guild_id=? AND message_id=? LIMIT 1",
            (guild_id, message_id),
        ).fetchone()


def record_forward_nomination(guild_id: int, channel_id: int, quote: str, message_id: int) -> bool:
    """
    Note that *quote* was forwarded into *channel_id*. Returns True if this is the
    first forward of that rename into that channel, or False if it's a duplicate
    (the (guild, channel, quote) primary key already exists).
    """
    with db_conn() as conn:
        try:
            conn.execute(
                "INSERT INTO forward_nominations (guild_id, channel_id, quote, message_id, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (guild_id, channel_id, quote, message_id, datetime.now(timezone.utc).isoformat()),
            )
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False


# Backwards-compatible alias — year brackets are just a range query.
def get_rename_posts_for_year(
    guild_id: int, year_start_utc: str, year_end_utc: str, voting_enabled_at: str | None,
) -> list[sqlite3.Row]:
    return get_rename_posts_in_range(guild_id, year_start_utc, year_end_utc, voting_enabled_at)


# ── brackets ──────────────────────────────────────────────────────────────────

def create_bracket(
    guild_id: int, year: int, size: int, voting_hours: int,
    label: str | None = None, range_start: str | None = None, range_end: str | None = None,
    pacing: str | None = None,
) -> int:
    with db_conn() as conn:
        cur = conn.execute(
            "INSERT INTO brackets (guild_id, year, size, voting_hours, created_at, label, range_start, range_end, pacing) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (guild_id, year, size, voting_hours, datetime.now(timezone.utc).isoformat(),
             label, range_start, range_end, pacing),
        )
        conn.commit()
        return cur.lastrowid


def get_active_bracket(guild_id: int) -> sqlite3.Row | None:
    with db_conn() as conn:
        return conn.execute(
            "SELECT * FROM brackets WHERE guild_id=? AND status='active' ORDER BY created_at DESC LIMIT 1",
            (guild_id,),
        ).fetchone()


def create_bracket_entry(
    bracket_id: int, seed: int, quote: str, quote_user: str | None, season_reactions: int,
    quote_uid: int | None = None, icon_user: str | None = None, icon_uid: int | None = None,
) -> int:
    with db_conn() as conn:
        cur = conn.execute(
            "INSERT INTO bracket_entries "
            "(bracket_id, seed, quote, quote_user, quote_uid, icon_user, icon_uid, season_reactions) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (bracket_id, seed, quote, quote_user, quote_uid, icon_user, icon_uid, season_reactions),
        )
        conn.commit()
        return cur.lastrowid


def set_bracket_entry_icon(entry_id: int, icon_user: str | None, icon_uid: int | None) -> None:
    """Backfill an entry's icon credit — test brackets pick their icons after seeding."""
    with db_conn() as conn:
        conn.execute(
            "UPDATE bracket_entries SET icon_user=?, icon_uid=? WHERE id=?",
            (icon_user, icon_uid, entry_id),
        )
        conn.commit()


def get_bracket_entry(entry_id: int) -> sqlite3.Row | None:
    with db_conn() as conn:
        return conn.execute("SELECT * FROM bracket_entries WHERE id=?", (entry_id,)).fetchone()


def create_bracket_matchup(bracket_id: int, round_num: int, match_num: int, entry_a_id: int, entry_b_id: int) -> int:
    with db_conn() as conn:
        cur = conn.execute(
            "INSERT INTO bracket_matchups (bracket_id, round, match_num, entry_a_id, entry_b_id) VALUES (?, ?, ?, ?, ?)",
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


def get_round_winners_ordered(bracket_id: int, round_num: int) -> list[int]:
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
        return conn.execute("SELECT current_round FROM brackets WHERE id=?", (bracket_id,)).fetchone()["current_round"]


def complete_bracket(bracket_id: int) -> None:
    with db_conn() as conn:
        conn.execute("UPDATE brackets SET status='complete' WHERE id=?", (bracket_id,))
        conn.commit()


def set_bracket_champion(
    bracket_id: int, quote: str, quote_user: str | None, quote_uid: int | None = None,
) -> None:
    """Record a completed bracket's winner, for /bracket history."""
    with db_conn() as conn:
        conn.execute(
            "UPDATE brackets SET champion_quote=?, champion_user=?, champion_uid=? WHERE id=?",
            (quote, quote_user, quote_uid, bracket_id),
        )
        conn.commit()


def get_bracket_history(guild_id: int, limit: int = 15) -> list[sqlite3.Row]:
    """Completed real brackets (newest first) with their champions. Excludes test brackets."""
    with db_conn() as conn:
        return conn.execute(
            "SELECT * FROM brackets WHERE guild_id=? AND status='complete' AND year != 0 "
            "ORDER BY created_at DESC LIMIT ?",
            (guild_id, limit),
        ).fetchall()


def cancel_bracket(bracket_id: int) -> None:
    """Hard-delete a bracket and all its entries/matchups. Used for test cleanup."""
    with db_conn() as conn:
        conn.execute("DELETE FROM bracket_matchups WHERE bracket_id=?", (bracket_id,))
        conn.execute("DELETE FROM bracket_entries  WHERE bracket_id=?", (bracket_id,))
        conn.execute("DELETE FROM brackets          WHERE id=?",         (bracket_id,))
        conn.commit()


def reset_guild(guild_id: int) -> None:
    """
    Delete ALL of a guild's data across every table (irreversible). Returns the
    server to a fresh state — used by /admin reset for testing/cleanup.
    """
    with db_conn() as conn:
        # bracket_entries/matchups are keyed by bracket_id, so clear them first.
        conn.execute(
            "DELETE FROM bracket_matchups WHERE bracket_id IN (SELECT id FROM brackets WHERE guild_id=?)",
            (guild_id,))
        conn.execute(
            "DELETE FROM bracket_entries WHERE bracket_id IN (SELECT id FROM brackets WHERE guild_id=?)",
            (guild_id,))
        for table in ("brackets", "rename_posts", "forward_nominations", "seasons",
                      "custom_features", "picks_history", "bot_admins", "server_config"):
            conn.execute(f"DELETE FROM {table} WHERE guild_id=?", (guild_id,))
        conn.commit()


# ── seasons ───────────────────────────────────────────────────────────────────
# A season is a named date range per guild. Brackets can be seeded from a season's
# window (e.g. monthly or holiday competitions) instead of a whole calendar year.

def add_season(guild_id: int, name: str, start_at: str, end_at: str) -> bool:
    """Create a season. Returns False if the guild already has one by that name (case-insensitive)."""
    with db_conn() as conn:
        try:
            conn.execute(
                "INSERT INTO seasons (guild_id, name, start_at, end_at, created_at) VALUES (?, ?, ?, ?, ?)",
                (guild_id, name, start_at, end_at, datetime.now(timezone.utc).isoformat()),
            )
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False


def get_season(guild_id: int, name: str) -> sqlite3.Row | None:
    with db_conn() as conn:
        return conn.execute(
            "SELECT * FROM seasons WHERE guild_id=? AND name=? COLLATE NOCASE",
            (guild_id, name),
        ).fetchone()


def get_seasons(guild_id: int) -> list[sqlite3.Row]:
    with db_conn() as conn:
        return conn.execute(
            "SELECT * FROM seasons WHERE guild_id=? ORDER BY start_at",
            (guild_id,),
        ).fetchall()


def remove_season(guild_id: int, name: str) -> bool:
    """Delete a season by name. Returns False if no such season existed."""
    with db_conn() as conn:
        cur = conn.execute(
            "DELETE FROM seasons WHERE guild_id=? AND name=? COLLATE NOCASE",
            (guild_id, name),
        )
        conn.commit()
        return cur.rowcount > 0


# ── custom_features ───────────────────────────────────────────────────────────
# Admin-defined "X of the day" features (e.g. Meme of the Day). Each is a per-guild
# named row with its own source/post channels and post time. Song-of-the-day is
# migrated into this table as a 'music' feature.

def add_custom_feature(
    guild_id: int, name: str, emoji: str | None, content_type: str,
    source_channel: int, post_channel: int, post_time: str,
    command: str | None = None, run_access: str = "admin", run_roles: str | None = None,
) -> bool:
    """Create a custom feature. Returns False if the guild already has one by that name (case-insensitive)."""
    with db_conn() as conn:
        try:
            conn.execute(
                "INSERT INTO custom_features "
                "(guild_id, name, emoji, content_type, source_channel, post_channel, post_time, "
                " created_at, command, run_access, run_roles) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (guild_id, name, emoji, content_type, source_channel, post_channel, post_time,
                 datetime.now(timezone.utc).isoformat(), command, run_access, run_roles),
            )
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False


def get_custom_feature_by_command(guild_id: int, command: str) -> sqlite3.Row | None:
    """Look up a feature by its slash-command slug (case-insensitive)."""
    with db_conn() as conn:
        return conn.execute(
            "SELECT * FROM custom_features WHERE guild_id=? AND command=? COLLATE NOCASE",
            (guild_id, command),
        ).fetchone()


def set_custom_feature_command(guild_id: int, name: str, command: str | None) -> bool:
    """Set (or clear, with None) a feature's slash-command slug. Returns False if no such feature."""
    with db_conn() as conn:
        cur = conn.execute(
            "UPDATE custom_features SET command=? WHERE guild_id=? AND name=? COLLATE NOCASE",
            (command, guild_id, name),
        )
        conn.commit()
        return cur.rowcount > 0


def set_custom_feature_access(guild_id: int, name: str, run_access: str, run_roles: str | None) -> bool:
    """Set who may run a feature's command ('admin'|'everyone'|'roles'). Returns False if no such feature."""
    with db_conn() as conn:
        cur = conn.execute(
            "UPDATE custom_features SET run_access=?, run_roles=? WHERE guild_id=? AND name=? COLLATE NOCASE",
            (run_access, run_roles, guild_id, name),
        )
        conn.commit()
        return cur.rowcount > 0


def get_custom_feature(guild_id: int, name: str) -> sqlite3.Row | None:
    with db_conn() as conn:
        return conn.execute(
            "SELECT * FROM custom_features WHERE guild_id=? AND name=? COLLATE NOCASE",
            (guild_id, name),
        ).fetchone()


_CUSTOM_FEATURE_EDITABLE = frozenset({
    "name", "emoji", "content_type", "source_channel", "post_channel", "post_time",
})


def update_custom_feature(feature_id: int, **fields) -> bool:
    """
    Update only the given (allowlisted) columns of a feature by id. Returns
    False if a new name collides with another feature in the same guild.
    Fields whose value is None are ignored (i.e. left unchanged).
    """
    cols = {k: v for k, v in fields.items() if k in _CUSTOM_FEATURE_EDITABLE and v is not None}
    if not cols:
        return True
    assignments = ", ".join(f"{k}=?" for k in cols)
    with db_conn() as conn:
        try:
            conn.execute(f"UPDATE custom_features SET {assignments} WHERE id=?",
                         (*cols.values(), feature_id))
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False


def get_custom_features(guild_id: int) -> list[sqlite3.Row]:
    with db_conn() as conn:
        return conn.execute(
            "SELECT * FROM custom_features WHERE guild_id=? ORDER BY post_time, name",
            (guild_id,),
        ).fetchall()


def count_custom_features(guild_id: int) -> int:
    with db_conn() as conn:
        return conn.execute(
            "SELECT COUNT(*) AS n FROM custom_features WHERE guild_id=?", (guild_id,),
        ).fetchone()["n"]


def remove_custom_feature(guild_id: int, name: str) -> bool:
    """Delete a custom feature by name. Returns False if no such feature existed."""
    with db_conn() as conn:
        cur = conn.execute(
            "DELETE FROM custom_features WHERE guild_id=? AND name=? COLLATE NOCASE",
            (guild_id, name),
        )
        conn.commit()
        return cur.rowcount > 0


def set_custom_feature_enabled(guild_id: int, name: str, enabled: bool) -> bool:
    """Enable/disable a custom feature. Returns False if no such feature existed."""
    with db_conn() as conn:
        cur = conn.execute(
            "UPDATE custom_features SET enabled=? WHERE guild_id=? AND name=? COLLATE NOCASE",
            (1 if enabled else 0, guild_id, name),
        )
        conn.commit()
        return cur.rowcount > 0


def get_custom_feature_by_id(feature_id: int) -> sqlite3.Row | None:
    with db_conn() as conn:
        return conn.execute("SELECT * FROM custom_features WHERE id=?", (feature_id,)).fetchone()


def set_custom_feature_schedule(feature_id: int, interval_days: int, weekdays: str | None) -> None:
    """Set a feature's cadence: interval_days (every N days) and/or weekdays (overrides). Handles NULL weekdays."""
    with db_conn() as conn:
        conn.execute(
            "UPDATE custom_features SET interval_days=?, weekdays=? WHERE id=?",
            (interval_days, weekdays, feature_id),
        )
        conn.commit()


def set_custom_feature_run_date(feature_id: int, date: str) -> None:
    """Record the last date a feature fired (guild-tz YYYY-MM-DD), for double-fire prevention."""
    with db_conn() as conn:
        conn.execute("UPDATE custom_features SET last_run_date=? WHERE id=?", (date, feature_id))
        conn.commit()


