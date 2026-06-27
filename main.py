import asyncio
import logging
import os

import discord

from db_utils import init_db, set_config, show_config, get_config
from bot_features import (
    process_rename,
    process_daily_song,
    schedule_rename,
    schedule_daily_song,
)

# ── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logging.getLogger("discord.gateway").setLevel(logging.WARNING)
logging.getLogger("discord.http").setLevel(logging.WARNING)
logging.getLogger("discord.client").setLevel(logging.ERROR)   # suppress PyNaCl/voice warnings

log = logging.getLogger(__name__)

# ── Config ───────────────────────────────────────────────────────────────────

TOKEN      = os.getenv("DISCORD_TOKEN")
QUOTE_TIME = os.getenv("QUOTE_TIME", "4:00")
SONG_TIME  = os.getenv("SONG_TIME",  "10:00")

# ── Discord client ───────────────────────────────────────────────────────────

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.messages = True
client = discord.Client(intents=intents)

# ── Command tables ───────────────────────────────────────────────────────────

# Maps command prefix -> (db field, success message)
_CHANNEL_SETTERS: dict[str, tuple[str, str]] = {
    "!setquotechannel":    ("quote_channel",     "✅ This channel set as Quote Channel."),
    "!seticonchannel":     ("icon_channel",      "✅ This channel set as Icon Channel."),
    "!setpostchannel":     ("post_channel",      "✅ This channel set as Post Channel."),
    "!setmusicchannel":    ("music_channel",     "✅ This channel set as Music Channel."),
    "!setsongpostchannel": ("song_post_channel", "✅ This channel set as Song Post Channel."),
}

# Maps feature keyword -> (db field, human label)
_FEATURE_MAP: dict[str, tuple[str, str]] = {
    "quote": ("enable_daily_quote", "Daily Quote"),
    "song":  ("enable_daily_song",  "Daily Song"),
}

_SETUP_TEXT = (
    "**Bot Setup Guide:**\n"
    "1. In each channel, run the appropriate command:\n"
    "   `!setquotechannel` · `!seticonchannel` · `!setpostchannel`\n"
    "   `!setmusicchannel` · `!setsongpostchannel`\n"
    "2. Toggle features: `!enablefeature [quote|song]` / `!disablefeature [quote|song]`\n"
    "3. Review settings: `!showconfig`\n"
    "4. All scheduled times are **EST**."
)

_NO_PERM = "⚠️ You don't have permission to use this command."

# ── Scheduler task tracking ──────────────────────────────────────────────────
#
# on_ready fires on EVERY reconnect (network drop, Discord restart, etc.),
# not just the first boot.  Without cancelling old tasks, each reconnect
# spawns a fresh pair of schedulers while the previous ones keep running —
# after a few days you end up with N copies all firing simultaneously.
#
# Fix: store task references and cancel the old ones before creating new ones.

_scheduler_tasks: list[asyncio.Task] = []


# ── Events ───────────────────────────────────────────────────────────────────

@client.event
async def on_ready():
    global _scheduler_tasks
    log.info("✅ Logged in as %s", client.user)
    init_db()

    # Cancel any schedulers left over from a previous connection.
    for task in _scheduler_tasks:
        if not task.done():
            task.cancel()
            log.info("🔄 Cancelled stale scheduler task: %s", task.get_name())

    _scheduler_tasks = [
        client.loop.create_task(schedule_rename(client, QUOTE_TIME),     name="schedule_rename"),
        client.loop.create_task(schedule_daily_song(client, SONG_TIME),  name="schedule_daily_song"),
    ]
    log.info("⏰ Scheduler tasks started (quote=%s, song=%s)", QUOTE_TIME, SONG_TIME)


@client.event
async def on_message(message: discord.Message):
    # Ignore bots, DMs, and quoted/reply messages.
    if message.author.bot or not message.guild:
        return
    if message.reference is not None:
        return

    content       = message.content.strip()
    content_lower = content.lower()
    gid           = message.guild.id
    is_admin      = message.author.guild_permissions.manage_guild

    # ── Channel setters (admin) ──────────────────────────────────────────
    for cmd, (field, reply) in _CHANNEL_SETTERS.items():
        if content_lower.startswith(cmd):
            if not is_admin:
                await message.channel.send(_NO_PERM)
                return
            set_config(gid, field, message.channel.id)
            await message.channel.send(reply)
            return

    # ── Enable / disable feature (admin) ────────────────────────────────
    if content_lower.startswith("!enablefeature ") or content_lower.startswith("!disablefeature "):
        if not is_admin:
            await message.channel.send(_NO_PERM)
            return
        enabling = content_lower.startswith("!enablefeature ")
        arg = content_lower.split(" ", 1)[1].strip()
        if arg in _FEATURE_MAP:
            db_field, label = _FEATURE_MAP[arg]
            set_config(gid, db_field, 1 if enabling else 0)
            verb = "enabled" if enabling else "disabled"
            await message.channel.send(f"✅ {label} feature {verb}.")
        else:
            await message.channel.send(f'⚠️ Unknown feature "{arg}". Use `quote` or `song`.')
        return

    # ── Show config (admin) ──────────────────────────────────────────────
    if content_lower.startswith("!showconfig"):
        if not is_admin:
            await message.channel.send(_NO_PERM)
            return
        await message.channel.send(f"```\n{show_config(gid)}\n```")
        return

    # ── Setup help (admin) ───────────────────────────────────────────────
    if content_lower.startswith("!setup"):
        if not is_admin:
            await message.channel.send(_NO_PERM)
            return
        await message.channel.send(_SETUP_TEXT)
        return

    # ── Public commands ──────────────────────────────────────────────────
    if content_lower.startswith("!rename"):
        cfg = get_config(gid)
        if cfg[6]:
            await process_rename(gid, client, override_post_channel=message.channel)
        else:
            await message.channel.send("⚠️ Daily Quote feature is disabled for this server.")
        return

    if content_lower.startswith("!song"):
        cfg = get_config(gid)
        if cfg[7]:
            await process_daily_song(gid, client)
        else:
            await message.channel.send("⚠️ Daily Song feature is disabled for this server.")
        return


# ── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if not TOKEN:
        log.critical("DISCORD_TOKEN environment variable not set — exiting.")
    else:
        client.run(TOKEN)
