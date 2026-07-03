import logging
import os
from datetime import datetime, timezone

import discord
import pytz

from db_utils import (
    init_db, set_config, show_config, get_config,
    cancel_bracket, get_active_bracket,
)
from bot_features import (
    process_rename,
    process_daily_song,
    scheduler_loop,
    build_mystats,
    build_contributors,
)
from bracket import (
    start_bracket,
    start_test_bracket,
    check_bracket_advancement,
    get_bracket_status_text,
    force_bracket_advance,
)

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logging.getLogger("discord.gateway").setLevel(logging.WARNING)
logging.getLogger("discord.http").setLevel(logging.WARNING)
logging.getLogger("discord.client").setLevel(logging.ERROR)

log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

TOKEN = os.getenv("DISCORD_TOKEN")

# ── Client ────────────────────────────────────────────────────────────────────

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.messages = True
client = discord.Client(intents=intents)

# ── Command tables ────────────────────────────────────────────────────────────

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
    "**Channels** (run each in the target channel):\n"
    "   `!setquotechannel` · `!seticonchannel` · `!setpostchannel`\n"
    "   `!setmusicchannel` · `!setsongpostchannel` · `!setbracketchannel`\n\n"
    "**Features:** `!enablefeature [quote|song|cooldown|voting]`\n"
    "             `!disablefeature [quote|song|cooldown|voting]`\n\n"
    "**Scheduling:**\n"
    "   `!settimezone <tz>` — e.g. `US/Eastern`, `Europe/London`\n"
    "   `!setscheduletime quote 8:00`\n"
    "   `!setscheduletime song 12:00`\n\n"
    "**Bracket:**\n"
    "   `!setbracketsize <4|8|16|32>`\n"
    "   `!setbracketvotingtime <hours>`\n"
    "   `!startbracket [year]` · `!testbracket`\n"
    "   `!forcebracketadvance` · `!bracketstatus` · `!cancelbracket`\n\n"
    "**Other:** `!showconfig` · `!preview rename` · `!preview song`\n"
    "   `!contributors [quote|icon|song]` · `!mystats`"
)

_NO_PERM = "⚠️ You don't have permission to use this command."

# ── Scheduler dedup ───────────────────────────────────────────────────────────

_scheduler_task: list = []


# ── Events ────────────────────────────────────────────────────────────────────

@client.event
async def on_ready():
    global _scheduler_task
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

    # ── Channel setters ───────────────────────────────────────────────────
    for cmd, (field, reply) in _CHANNEL_SETTERS.items():
        if content_lower.startswith(cmd):
            if not is_admin:
                await message.channel.send(_NO_PERM)
                return
            set_config(gid, field, message.channel.id)
            await message.channel.send(reply)
            return

    # ── Enable / disable feature ──────────────────────────────────────────
    if content_lower.startswith("!enablefeature ") or content_lower.startswith("!disablefeature "):
        if not is_admin:
            await message.channel.send(_NO_PERM)
            return
        enabling = content_lower.startswith("!enablefeature ")
        arg = content_lower.split(" ", 1)[1].strip()
        if arg in _FEATURE_MAP:
            db_field, label = _FEATURE_MAP[arg]
            set_config(gid, db_field, 1 if enabling else 0)
            if arg == "voting" and enabling:
                cfg_now = get_config(gid)
                if not cfg_now["voting_enabled_at"]:
                    set_config(gid, "voting_enabled_at", datetime.now(timezone.utc).isoformat())
            verb = "enabled" if enabling else "disabled"
            await message.channel.send(f"✅ {label} feature {verb}.")
        else:
            await message.channel.send(f'⚠️ Unknown feature "{arg}". Use `quote`, `song`, `cooldown`, or `voting`.')
        return

    # ── Set timezone ──────────────────────────────────────────────────────
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
                f'Use a tz database name, e.g. `US/Eastern`, `Europe/London`, `Asia/Tokyo`.\n'
                f'Full list: <https://en.wikipedia.org/wiki/List_of_tz_database_time_zones>'
            )
            return
        set_config(gid, "timezone", tz_str)
        await message.channel.send(f"✅ Timezone set to `{tz_str}`.")
        return

    # ── Set schedule time ─────────────────────────────────────────────────
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

    # ── Show config ───────────────────────────────────────────────────────
    if content_lower.startswith("!showconfig"):
        if not is_admin:
            await message.channel.send(_NO_PERM)
            return
        await message.channel.send(f"```\n{show_config(gid)}\n```")
        return

    # ── Setup help ────────────────────────────────────────────────────────
    if content_lower.startswith("!setup"):
        if not is_admin:
            await message.channel.send(_NO_PERM)
            return
        await message.channel.send(_SETUP_TEXT)
        return

    # ── Preview ───────────────────────────────────────────────────────────
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

    # ── Contributors ──────────────────────────────────────────────────────
    if content_lower.startswith("!contributors"):
        if not is_admin:
            await message.channel.send(_NO_PERM)
            return
        parts = content_lower.split()
        if len(parts) < 2 or parts[1] not in ("quote", "icon", "song"):
            await message.channel.send("⚠️ Usage: `!contributors [quote|icon|song]`")
            return
        status = await message.channel.send(f"⏳ Scanning {parts[1]} channel...")
        result = await build_contributors(gid, client, parts[1])
        await status.edit(content=result)
        return

    # ── Set bracket size ──────────────────────────────────────────────────
    if content_lower.startswith("!setbracketsize "):
        if not is_admin:
            await message.channel.send(_NO_PERM)
            return
        parts = content.split()
        if len(parts) < 2:
            await message.channel.send("⚠️ Usage: `!setbracketsize <4|8|16|32>`")
            return
        try:
            import math
            size = int(parts[1])
            assert size >= 4 and math.log2(size).is_integer()
        except Exception:
            await message.channel.send("⚠️ Bracket size must be a power of 2: `4`, `8`, `16`, or `32`.")
            return
        set_config(gid, "bracket_size", size)
        await message.channel.send(f"✅ Bracket size set to **{size}**.")
        return

    # ── Set bracket voting time ───────────────────────────────────────────
    if content_lower.startswith("!setbracketvotingtime "):
        if not is_admin:
            await message.channel.send(_NO_PERM)
            return
        parts = content.split()
        if len(parts) < 2:
            await message.channel.send("⚠️ Usage: `!setbracketvotingtime <hours>` (e.g. `24`)")
            return
        try:
            hours = int(parts[1])
            assert 1 <= hours <= 168
        except Exception:
            await message.channel.send("⚠️ Hours must be a whole number between 1 and 168.")
            return
        set_config(gid, "bracket_voting_hours", hours)
        await message.channel.send(f"✅ Bracket voting window set to **{hours} hour(s)** per matchup.")
        return

    # ── Start real bracket ────────────────────────────────────────────────
    if content_lower.startswith("!startbracket"):
        if not is_admin:
            await message.channel.send(_NO_PERM)
            return
        parts = content.split()
        year  = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else datetime.now().year
        await message.channel.send(f"⏳ Seeding {year} bracket...")
        success, msg = await start_bracket(gid, client, year)
        await message.channel.send(msg)
        return

    # ── Test bracket ──────────────────────────────────────────────────────
    if content_lower.startswith("!testbracket"):
        if not is_admin:
            await message.channel.send(_NO_PERM)
            return
        await message.channel.send("⏳ Setting up test bracket...")
        success, msg = await start_test_bracket(gid, client)
        await message.channel.send(msg)
        return

    # ── Force bracket advance ─────────────────────────────────────────────
    if content_lower.startswith("!forcebracketadvance"):
        if not is_admin:
            await message.channel.send(_NO_PERM)
            return
        success, msg = await force_bracket_advance(gid, client)
        await message.channel.send(msg)
        return

    # ── Bracket status ────────────────────────────────────────────────────
    if content_lower.startswith("!bracketstatus"):
        if not is_admin:
            await message.channel.send(_NO_PERM)
            return
        await message.channel.send(get_bracket_status_text(gid))
        return

    # ── Cancel bracket ────────────────────────────────────────────────────
    if content_lower.startswith("!cancelbracket"):
        if not is_admin:
            await message.channel.send(_NO_PERM)
            return
        bracket = get_active_bracket(gid)
        if not bracket:
            await message.channel.send("⚠️ No active bracket to cancel.")
            return
        cancel_bracket(bracket["id"])
        await message.channel.send("🗑️ Active bracket cancelled and removed.")
        return

    # ── My stats ──────────────────────────────────────────────────────────
    if content_lower.startswith("!mystats"):
        status = await message.channel.send("⏳ Scanning channels for your stats...")
        result = await build_mystats(gid, client, message.author.id, message.author.display_name)
        await status.edit(content=result)
        return

    # ── Manual rename ─────────────────────────────────────────────────────
    if content_lower.startswith("!rename"):
        cfg = get_config(gid)
        if cfg["enable_daily_quote"]:
            await process_rename(gid, client, override_post_channel=message.channel)
        else:
            await message.channel.send("⚠️ Daily Quote feature is disabled for this server.")
        return

    # ── Manual song ───────────────────────────────────────────────────────
    if content_lower.startswith("!song"):
        cfg = get_config(gid)
        if cfg["enable_daily_song"]:
            await process_daily_song(gid, client)
        else:
            await message.channel.send("⚠️ Daily Song feature is disabled for this server.")
        return


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if not TOKEN:
        log.critical("DISCORD_TOKEN environment variable not set — exiting.")
    else:
        client.run(TOKEN)
