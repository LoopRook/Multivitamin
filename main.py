import logging
import os

import discord
import pytz

from db_utils import init_db, set_config, show_config, get_config, cancel_bracket, cancel_bracket
from bracket import (
    start_bracket, start_test_bracket,
    check_bracket_advancement, get_bracket_status_text,
    force_bracket_advance,
)
from bot_features import (
    process_rename,
    process_daily_song,
    scheduler_loop,
    build_mystats,
    build_contributors,
)

# ── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logging.getLogger("discord.gateway").setLevel(logging.WARNING)
logging.getLogger("discord.http").setLevel(logging.WARNING)
logging.getLogger("discord.client").setLevel(logging.ERROR)

log = logging.getLogger(__name__)

# ── Config ───────────────────────────────────────────────────────────────────

TOKEN = os.getenv("DISCORD_TOKEN")

# ── Discord client ───────────────────────────────────────────────────────────

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.messages = True
client = discord.Client(intents=intents)

# ── Command tables ───────────────────────────────────────────────────────────

_CHANNEL_SETTERS: dict[str, tuple[str, str]] = {
    "!setquotechannel":    ("quote_channel",     "✅ This channel set as Quote Channel."),
    "!seticonchannel":     ("icon_channel",      "✅ This channel set as Icon Channel."),
    "!setpostchannel":     ("post_channel",      "✅ This channel set as Post Channel."),
    "!setmusicchannel":    ("music_channel",     "✅ This channel set as Music Channel."),
    "!setsongpostchannel": ("song_post_channel", "✅ This channel set as Song Post Channel."),
    "!setbracketchannel":  ("bracket_channel",   "✅ This channel set as Bracket Channel."),
}

_FEATURE_MAP: dict[str, tuple[str, str]] = {
    "quote":    ("enable_daily_quote", "Daily Quote"),
    "song":     ("enable_daily_song",  "Daily Song"),
    "cooldown": ("enable_cooldown",    "Cooldown"),
    "voting":   ("enable_voting",      "Voting"),
}

_SETUP_TEXT = (
    "**Bot Setup Guide:**\n"
    "**Channel setup** (run each command in the target channel):\n"
    "   `!setquotechannel` · `!seticonchannel` · `!setpostchannel`\n"
    "   `!setmusicchannel` · `!setsongpostchannel`\n\n"
    "**Features:** `!enablefeature [quote|song]` / `!disablefeature [quote|song]`\n\n"
    "**Scheduling:**\n"
    "   `!settimezone <tz>` — e.g. `US/Eastern`, `Europe/London`, `Asia/Tokyo`\n"
    "   `!setscheduletime quote 8:00` — set daily quote time\n"
    "   `!setscheduletime song 12:00` — set daily song time\n\n"
    "**Other:** `!showconfig` · `!preview rename` · `!preview song`\n"
    "   `!contributors [quote|icon|song]` · `!mystats`\n\n"
    "**Voting & Bracket:**\n"
    "   `!enablefeature voting` — start tracking rename posts (any reaction counts as a vote)\n"
    "   `!setbracketchannel` — run in your bracket channel\n"
    "   `!setbracketsize <4|8|16|32>` · `!setbracketvotingtime <hours>`\n"
    "   `!startbracket [year]` — seeds bracket by reaction count, uses Discord polls for matchups\n"
    "   `!testbracket` — test the full bracket flow using quote channel entries\n"
    "   `!forcebracketadvance` · `!cancelbracket` · `!bracketstatus`"
)

_NO_PERM = "⚠️ You don't have permission to use this command."

# ── Scheduler task tracking ──────────────────────────────────────────────────
# on_ready fires on every reconnect. We cancel the old task before spawning
# a new one to prevent duplicate schedulers accumulating over time.

_scheduler_task: list = []


@client.event
async def on_ready():
    log.info("✅ Logged in as %s", client.user)
    init_db()

    for task in _scheduler_task:
        if not task.done():
            task.cancel()
            log.info("🔄 Cancelled stale scheduler task.")
    _scheduler_task.clear()

    task = client.loop.create_task(scheduler_loop(client), name="scheduler_loop")
    _scheduler_task.append(task)
    log.info("⏰ Scheduler started.")


@client.event
async def on_message(message: discord.Message):
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
            # When voting is first enabled, record the timestamp so the bracket
            # knows which rename posts to include (only posts after this date).
            if arg == "voting" and enabling:
                cfg_now = get_config(gid)
                if not cfg_now["voting_enabled_at"]:
                    from datetime import datetime, timezone
                    set_config(gid, "voting_enabled_at", datetime.now(timezone.utc).isoformat())
            verb = "enabled" if enabling else "disabled"
            await message.channel.send(f"✅ {label} feature {verb}.")
        else:
            await message.channel.send(f'⚠️ Unknown feature "{arg}". Use `quote`, `song`, `cooldown`, or `voting`.')
        return

    # ── Set timezone (admin) ─────────────────────────────────────────────
    if content_lower.startswith("!settimezone "):
        if not is_admin:
            await message.channel.send(_NO_PERM)
            return
        tz_str = content.split(" ", 1)[1].strip()
        try:
            pytz.timezone(tz_str)
        except pytz.exceptions.UnknownTimeZoneError:
            await message.channel.send(
                f'⚠️ Unknown timezone `{tz_str}`.\n'
                f'Use a standard tz name, e.g. `US/Eastern`, `Europe/London`, `Asia/Tokyo`.\n'
                f'Full list: <https://en.wikipedia.org/wiki/List_of_tz_database_time_zones>'
            )
            return
        set_config(gid, "timezone", tz_str)
        await message.channel.send(f"✅ Timezone set to `{tz_str}`.")
        return

    # ── Set schedule time (admin) ────────────────────────────────────────
    if content_lower.startswith("!setscheduletime "):
        if not is_admin:
            await message.channel.send(_NO_PERM)
            return
        parts = content.split()
        if len(parts) < 3:
            await message.channel.send("⚠️ Usage: `!setscheduletime [quote|song] <H:MM>`")
            return
        which = parts[1].lower()
        time_str = parts[2]
        if which not in ("quote", "song"):
            await message.channel.send('⚠️ First argument must be `quote` or `song`.')
            return
        try:
            h, m = time_str.split(":")
            assert 0 <= int(h) <= 23 and 0 <= int(m) <= 59
        except Exception:
            await message.channel.send("⚠️ Time must be in `H:MM` or `HH:MM` format (24-hour).")
            return
        field = "quote_time" if which == "quote" else "song_time"
        set_config(gid, field, time_str)
        cfg = get_config(gid)
        tz_name = cfg["timezone"] or "US/Eastern"
        await message.channel.send(f"✅ {which.title()} time set to `{time_str}` ({tz_name}).")
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

    # ── Preview (admin) ──────────────────────────────────────────────────
    if content_lower.startswith("!preview "):
        if not is_admin:
            await message.channel.send(_NO_PERM)
            return
        arg = content_lower.split(" ", 1)[1].strip()
        if arg == "rename":
            await message.channel.send("⏳ Scanning channels for preview...")
            await process_rename(gid, client, override_post_channel=message.channel, preview=True)
        elif arg == "song":
            await message.channel.send("⏳ Scanning music channel for preview...")
            await process_daily_song(gid, client, override_post_channel=message.channel, preview=True)
        else:
            await message.channel.send('⚠️ Usage: `!preview rename` or `!preview song`')
        return

    # ── Contributors (admin) ─────────────────────────────────────────────
    if content_lower.startswith("!contributors"):
        if not is_admin:
            await message.channel.send(_NO_PERM)
            return
        parts = content_lower.split()
        if len(parts) < 2 or parts[1] not in ("quote", "icon", "song"):
            await message.channel.send("⚠️ Usage: `!contributors [quote|icon|song]`")
            return
        category = parts[1]
        status = await message.channel.send(f"⏳ Scanning {category} channel...")
        result = await build_contributors(gid, client, category)
        await status.edit(content=result)
        return

    # ── My stats (anyone) ────────────────────────────────────────────────
    if content_lower.startswith("!mystats"):
        status = await message.channel.send("⏳ Scanning channels for your stats...")
        result = await build_mystats(gid, client, message.author.id, message.author.display_name)
        await status.edit(content=result)
        return

    # ── Public commands ──────────────────────────────────────────────────
    if content_lower.startswith("!rename"):
        cfg = get_config(gid)
        if cfg["enable_daily_quote"]:
            await process_rename(gid, client, override_post_channel=message.channel)
        else:
            await message.channel.send("⚠️ Daily Quote feature is disabled for this server.")
        return

    if content_lower.startswith("!song"):
        cfg = get_config(gid)
        if cfg["enable_daily_song"]:
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
