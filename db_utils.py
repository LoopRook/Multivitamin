import logging
import os
import sqlite3

log = logging.getLogger(__name__)

DB_FILE = os.getenv("DB_FILE", "/data/server_config.db")

# Allowlist guards against accidental (or malicious) field injection in set_config.
_VALID_FIELDS = frozenset({
    "quote_channel",
    "icon_channel",
    "post_channel",
    "music_channel",
    "song_post_channel",
    "enable_daily_quote",
    "enable_daily_song",
})

CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS server_config (
    guild_id           INTEGER PRIMARY KEY,
    quote_channel      INTEGER,
    icon_channel       INTEGER,
    post_channel       INTEGER,
    music_channel      INTEGER,
    song_post_channel  INTEGER,
    enable_daily_quote INTEGER DEFAULT 1,
    enable_daily_song  INTEGER DEFAULT 1
)
"""

_FIELD_LABELS = [
    "Guild ID",
    "Quote Channel",
    "Icon Channel",
    "Post Channel",
    "Music Channel",
    "Song Post Channel",
    "Quote Feature Enabled",
    "Song Feature Enabled",
]


def db_conn():
    # check_same_thread=False is safe here because all DB access is serialised
    # through asyncio's single-threaded event loop.
    return sqlite3.connect(DB_FILE, check_same_thread=False)


def init_db() -> None:
    with db_conn() as conn:
        conn.execute(CREATE_TABLE)
    log.info("Database ready at %s", DB_FILE)


def get_config(guild_id: int) -> tuple:
    with db_conn() as conn:
        row = conn.execute(
            "SELECT * FROM server_config WHERE guild_id=?", (guild_id,)
        ).fetchone()
        if not row:
            # INSERT OR IGNORE avoids a rare race if two events fire simultaneously.
            conn.execute(
                "INSERT OR IGNORE INTO server_config (guild_id) VALUES (?)", (guild_id,)
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM server_config WHERE guild_id=?", (guild_id,)
            ).fetchone()
        return row


def set_config(guild_id: int, field: str, value) -> None:
    if field not in _VALID_FIELDS:
        raise ValueError(f"Invalid config field: {field!r}")
    with db_conn() as conn:
        conn.execute(
            f"UPDATE server_config SET {field}=? WHERE guild_id=?", (value, guild_id)
        )
        conn.commit()


def show_config(guild_id: int) -> str:
    cfg = get_config(guild_id)
    return "\n".join(
        f"{label}: {val if val is not None else 'Not Set'}"
        for label, val in zip(_FIELD_LABELS, cfg)
    )
