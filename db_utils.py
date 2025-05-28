import sqlite3
import os

DB_FILE = os.getenv('DB_FILE', '/data/server_config.db')


CREATE_TABLE = '''CREATE TABLE IF NOT EXISTS server_config (
    guild_id INTEGER PRIMARY KEY,
    quote_channel INTEGER,
    icon_channel INTEGER,
    post_channel INTEGER,
    music_channel INTEGER,
    song_post_channel INTEGER,
    enable_daily_quote INTEGER DEFAULT 1,
    enable_daily_song INTEGER DEFAULT 1
)'''

def db_conn():
    return sqlite3.connect(DB_FILE)

def get_config(guild_id):
    with db_conn() as conn:
        row = conn.execute('SELECT * FROM server_config WHERE guild_id=?', (guild_id,)).fetchone()
        if not row:
            conn.execute('INSERT INTO server_config (guild_id) VALUES (?)', (guild_id,))
            conn.commit()
            row = conn.execute('SELECT * FROM server_config WHERE guild_id=?', (guild_id,)).fetchone()
        return row

def set_config(guild_id, field, value):
    with db_conn() as conn:
        conn.execute(f'UPDATE server_config SET {field}=? WHERE guild_id=?', (value, guild_id))
        conn.commit()

def show_config(guild_id):
    cfg = get_config(guild_id)
    fields = ['Guild ID', 'Quote Channel', 'Icon Channel', 'Post Channel', 'Music Channel', 'Song Post Channel', 'Quote Feature Enabled', 'Song Feature Enabled']
    return '\n'.join([f"{fields[i]}: {cfg[i] if cfg[i] is not None else 'Not Set'}" for i in range(len(fields))])

def init_db():
    with db_conn() as conn:
        conn.execute(CREATE_TABLE)
