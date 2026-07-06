import logging
import os
from datetime import datetime

import discord
import pytz

from db_utils import (
    init_db, set_config, get_config,
    cancel_bracket, get_active_bracket,
    add_bot_admin, remove_bot_admin, get_bot_admins, is_bot_admin,
    add_season, get_seasons, remove_season,
)
from bot_features import (
    process_rename,
    process_daily_song,
    scheduler_loop,
    build_mystats,
    build_contributors,
    build_config,
)
from bracket import (
    start_bracket,
    start_season_bracket,
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
}

_SETUP_TEXT = (
    "**Bot Setup Guide:**\n"
    "**Channels** (run each in the target channel):\n"
    "   `!setquotechannel` · `!seticonchannel` · `!setpostchannel`\n"
    "   `!setmusicchannel` · `!setsongpostchannel` · `!setbracketchannel`\n\n"
    "**Features:** `!enablefeature [quote|song|cooldown]`\n"
    "             `!disablefeature [quote|song|cooldown]`\n"
    "   *(Bracket tracking is always on once a Post Channel is set.)*\n\n"
    "**Scheduling:**\n"
    "   `!settimezone <tz>` — e.g. `US/Eastern`, `Europe/London`\n"
    "   `!setscheduletime quote 8:00`\n"
    "   `!setscheduletime song 12:00`\n\n"
    "**Bracket:**\n"
    "   `!setbracketsize <4|8|16|32>`\n"
    "   `!setbracketvotingtime <hours>`\n"
    "   `!setbracketpacing [round|daily]`\n"
    "   `!startbracket [year]` · `!startbracket season <name>` · `!testbracket`\n"
    "   `!forcebracketadvance` · `!bracketstatus` · `!cancelbracket`\n\n"
    "**Seasons:**\n"
    "   `!addseason <start> <end> <name>` — dates as `YYYY-MM-DD`\n"
    "   `!listseasons` · `!removeseason <name>`\n\n"
    "**Admins** (Manage Server):\n"
    "   `!addadmin @user` · `!removeadmin @user` · `!listadmins`\n\n"
    "**Other:** `!showconfig` · `!preview rename` · `!preview song`\n"
    "   `!contributors [quote|icon|song]` · `!mystats`\n\n"
    "**See every command:** `!help` (or `!commands`)"
)

_NO_PERM = "⚠️ You don't have permission to use this command."


def build_help_embed(is_admin: bool = False, is_manager: bool = False) -> discord.Embed:
    """
    Command reference grouped by category.
    Admin-only sections are included only when *is_admin* is True, so ordinary
    members see just the commands they can actually run.  The bot-admin roster
    section is shown only to *is_manager* (Manage Server) users.
    """
    if is_admin:
        description = (
            "All commands use the `!` prefix. The **(Admin)** sections below require the "
            "*Manage Server* permission or bot-admin status. The bot ignores replies, so "
            "quoting a message won't trigger anything."
        )
    else:
        description = (
            "All commands use the `!` prefix. The bot ignores replies, so quoting a message "
            "won't trigger anything."
        )

    embed = discord.Embed(
        title="📖 Command Reference",
        description=description,
        color=discord.Color.blurple(),
    )

    embed.add_field(
        name="🌐 Everyone",
        value=(
            "`!rename` — trigger a rename now\n"
            "`!song` — post the song of the day\n"
            "`!mystats` — your submission counts & last picks\n"
            "`!help` · `!commands` — show this list"
        ),
        inline=False,
    )

    if not is_admin:
        return embed

    embed.add_field(
        name="📺 Channels (Admin) — run inside the target channel",
        value=(
            "`!setquotechannel` — quote submissions\n"
            "`!seticonchannel` — icon images\n"
            "`!setpostchannel` — official rename cards (tracked for brackets)\n"
            "`!setmusicchannel` — song links\n"
            "`!setsongpostchannel` — daily song posts\n"
            "`!setbracketchannel` — bracket matchups & results"
        ),
        inline=False,
    )
    embed.add_field(
        name="🎚️ Features (Admin)",
        value=(
            "`!enablefeature <name>` · `!disablefeature <name>`\n"
            "Names: `quote`, `song`, `cooldown`\n"
            "*(Bracket tracking is always on once a Post Channel is set.)*"
        ),
        inline=False,
    )
    embed.add_field(
        name="⏰ Scheduling (Admin)",
        value=(
            "`!settimezone <tz>` — IANA name, e.g. `US/Eastern`\n"
            "`!setscheduletime quote 8:00`\n"
            "`!setscheduletime song 12:00`"
        ),
        inline=False,
    )
    embed.add_field(
        name="🏆 Bracket Setup (Admin)",
        value=(
            "`!setbracketsize <4|8|16|32>`\n"
            "`!setbracketvotingtime <hours>` — 1 to 168\n"
            "`!setbracketpacing <round|daily>` — all matchups at once, or one at a time"
        ),
        inline=False,
    )
    embed.add_field(
        name="🏆 Bracket Actions (Admin)",
        value=(
            "`!startbracket [year]` — seed a bracket for a calendar year\n"
            "`!startbracket season <name>` — seed from a season's window\n"
            "`!testbracket` — test bracket with random scores\n"
            "`!forcebracketadvance` — tally current polls now & advance\n"
            "`!bracketstatus` — current round, pacing & progress\n"
            "`!cancelbracket` — delete the active bracket"
        ),
        inline=False,
    )
    embed.add_field(
        name="📅 Seasons (Admin)",
        value=(
            "`!addseason <start> <end> <name>` — dates as `YYYY-MM-DD`\n"
            "`!listseasons` — list defined seasons\n"
            "`!removeseason <name>` — delete a season"
        ),
        inline=False,
    )
    embed.add_field(
        name="ℹ️ Info & Preview (Admin)",
        value=(
            "`!showconfig` — current settings\n"
            "`!contributors <quote|icon|song>` — submission leaderboard\n"
            "`!preview rename` · `!preview song` — dry run, posts here only\n"
            "`!setup` — quick setup guide"
        ),
        inline=False,
    )
    if is_manager:
        embed.add_field(
            name="👑 Bot Admins (Manage Server only)",
            value=(
                "`!addadmin @user` — grant bot-admin access\n"
                "`!removeadmin @user` — revoke bot-admin access\n"
                "`!listadmins` — list current bot-admins"
            ),
            inline=False,
        )
    return embed


def _parse_season_dates(start_str: str, end_str: str, tz) -> tuple[str, str]:
    """
    Parse two YYYY-MM-DD dates (interpreted in *tz*) into ISO UTC bounds:
    the start's 00:00:00 and the end's 23:59:59. Raises ValueError on a bad
    format or if the end date precedes the start date.
    """
    start_d = datetime.strptime(start_str, "%Y-%m-%d")
    end_d   = datetime.strptime(end_str,   "%Y-%m-%d")
    start_utc = tz.localize(start_d.replace(hour=0,  minute=0,  second=0)).astimezone(pytz.utc)
    end_utc   = tz.localize(end_d.replace(hour=23, minute=59, second=59)).astimezone(pytz.utc)
    if end_utc < start_utc:
        raise ValueError("end date precedes start date")
    return start_utc.isoformat(), end_utc.isoformat()


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
    # Manage Server holders always count as admins; so do users granted bot-admin.
    # Managing the bot-admin roster itself requires Manage Server (is_manager).
    is_manager    = message.author.guild_permissions.manage_guild
    is_admin      = is_manager or is_bot_admin(gid, message.author.id)

    # ── Help / command list (anyone) ──────────────────────────────────────
    if content_lower.startswith("!help") or content_lower.startswith("!commands"):
        embed = build_help_embed(is_admin, is_manager)
        try:
            await message.channel.send(embed=embed)
        except discord.HTTPException:
            # Fall back to plain text if embeds aren't permitted here, keeping
            # the same admin/non-admin gating as the embed.
            lines = [f"**{embed.title}**", embed.description, ""]
            for f in embed.fields:
                lines += [f"__{f.name}__", f.value, ""]
            await message.channel.send("\n".join(lines))
        return

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
            verb = "enabled" if enabling else "disabled"
            await message.channel.send(f"✅ {label} feature {verb}.")
        elif arg == "voting":
            await message.channel.send(
                "ℹ️ Voting is no longer a toggle — bracket tracking is always on once a "
                "**Post Channel** is set (`!setpostchannel`). See `!help` for bracket & season commands."
            )
        else:
            await message.channel.send(f'⚠️ Unknown feature "{arg}". Use `quote`, `song`, or `cooldown`.')
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
        cfg_text = await build_config(gid, client)
        await message.channel.send(f"```\n{cfg_text}\n```")
        return

    # ── Setup help ────────────────────────────────────────────────────────
    if content_lower.startswith("!setup"):
        if not is_admin:
            await message.channel.send(_NO_PERM)
            return
        await message.channel.send(_SETUP_TEXT)
        return

    # ── Bot admin roster (Manage Server only) ─────────────────────────────
    if content_lower.startswith("!addadmin") or content_lower.startswith("!removeadmin"):
        if not is_manager:
            await message.channel.send(
                "⚠️ Only members with the **Manage Server** permission can manage bot-admins."
            )
            return
        adding = content_lower.startswith("!addadmin")
        verb   = "addadmin" if adding else "removeadmin"

        # Resolve the target: prefer a mention, otherwise a raw user ID.
        target_id = target_name = None
        if message.mentions:
            target      = message.mentions[0]
            target_id   = target.id
            target_name = target.display_name
        else:
            parts = content.split()
            if len(parts) >= 2 and parts[1].isdigit():
                target_id = int(parts[1])
                member    = message.guild.get_member(target_id)
                target_name = member.display_name if member else str(target_id)

        if not target_id:
            await message.channel.send(f"⚠️ Usage: `!{verb} @user` (you can also pass a user ID).")
            return

        if adding:
            created = add_bot_admin(gid, target_id, message.author.id)
            if created:
                await message.channel.send(f"✅ **{target_name}** is now a bot-admin and can use admin commands.")
            else:
                await message.channel.send(f"ℹ️ **{target_name}** is already a bot-admin.")
        else:
            removed = remove_bot_admin(gid, target_id)
            if removed:
                await message.channel.send(f"✅ **{target_name}** is no longer a bot-admin.")
            else:
                await message.channel.send(f"ℹ️ **{target_name}** wasn't a bot-admin.")
        return

    # ── List bot admins (Manage Server only) ──────────────────────────────
    if content_lower.startswith("!listadmins"):
        if not is_manager:
            await message.channel.send(
                "⚠️ Only members with the **Manage Server** permission can view the bot-admin list."
            )
            return
        admin_ids = get_bot_admins(gid)
        if not admin_ids:
            await message.channel.send(
                "No bot-admins configured. Anyone with the **Manage Server** permission already has admin access."
            )
            return
        lines = ["**Bot Admins** *(in addition to Manage Server holders)*:"]
        for uid in admin_ids:
            member = message.guild.get_member(uid)
            lines.append(f"• {member.display_name if member else f'User {uid}'} (`{uid}`)")
        await message.channel.send("\n".join(lines))
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

    # ── Set bracket pacing ────────────────────────────────────────────────
    if content_lower.startswith("!setbracketpacing"):
        if not is_admin:
            await message.channel.send(_NO_PERM)
            return
        parts = content_lower.split()
        if len(parts) < 2 or parts[1] not in ("round", "daily"):
            await message.channel.send("⚠️ Usage: `!setbracketpacing [round|daily]`")
            return
        pacing = parts[1]
        set_config(gid, "bracket_pacing", pacing)
        detail = "matchups will post all at once" if pacing == "round" else "one matchup per day"
        await message.channel.send(f"✅ Bracket pacing set to **{pacing}** — {detail}.")
        return

    # ── Seasons ───────────────────────────────────────────────────────────
    if content_lower.startswith("!addseason"):
        if not is_admin:
            await message.channel.send(_NO_PERM)
            return
        parts = content.split(None, 3)   # !addseason, <start>, <end>, <name...>
        if len(parts) < 4:
            await message.channel.send(
                "⚠️ Usage: `!addseason <start> <end> <name>`\n"
                "Dates are `YYYY-MM-DD` in your server's timezone. "
                "Example: `!addseason 2026-10-01 2026-10-31 Halloween 2026`"
            )
            return
        _, start_str, end_str, name = parts
        name = name.strip()
        cfg  = get_config(gid)
        try:
            tz = pytz.timezone(cfg["timezone"] or "US/Eastern")
        except pytz.exceptions.UnknownTimeZoneError:
            tz = pytz.timezone("US/Eastern")
        try:
            start_utc, end_utc = _parse_season_dates(start_str, end_str, tz)
        except ValueError:
            await message.channel.send(
                "⚠️ Invalid dates. Use `YYYY-MM-DD` for both, and make sure the end is on/after the start."
            )
            return
        if add_season(gid, name, start_utc, end_utc):
            await message.channel.send(
                f"✅ Season **{name}** created: {start_str} → {end_str}.\n"
                f"Start it any time with `!startbracket season {name}`."
            )
        else:
            await message.channel.send(f'⚠️ A season named "{name}" already exists. Remove it first with `!removeseason`.')
        return

    if content_lower.startswith("!listseasons"):
        if not is_admin:
            await message.channel.send(_NO_PERM)
            return
        seasons = get_seasons(gid)
        if not seasons:
            await message.channel.send("No seasons defined. Create one with `!addseason <start> <end> <name>`.")
            return
        lines = ["**Seasons:**"]
        for s in seasons:
            lines.append(f'• **{s["name"]}** — {s["start_at"][:10]} → {s["end_at"][:10]}')
        lines.append("\nStart one with `!startbracket season <name>`.")
        await message.channel.send("\n".join(lines))
        return

    if content_lower.startswith("!removeseason"):
        if not is_admin:
            await message.channel.send(_NO_PERM)
            return
        arg = content.split(None, 1)
        name = arg[1].strip() if len(arg) > 1 else ""
        if not name:
            await message.channel.send("⚠️ Usage: `!removeseason <name>`")
            return
        if remove_season(gid, name):
            await message.channel.send(f"🗑️ Season **{name}** removed.")
        else:
            await message.channel.send(f'⚠️ No season named "{name}". See `!listseasons`.')
        return

    # ── Start real bracket (by year or by season) ─────────────────────────
    if content_lower.startswith("!startbracket"):
        if not is_admin:
            await message.channel.send(_NO_PERM)
            return
        parts = content.split()
        if len(parts) > 1 and parts[1].lower() == "season":
            # Everything after "season" is the season name (may contain spaces).
            season_name = content.split(None, 2)[2].strip() if len(parts) > 2 else ""
            if not season_name:
                await message.channel.send("⚠️ Usage: `!startbracket season <name>` — see `!listseasons`.")
                return
            await message.channel.send(f'⏳ Seeding "{season_name}" season bracket...')
            success, msg = await start_season_bracket(gid, client, season_name)
            await message.channel.send(msg)
            return
        year = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else datetime.now().year
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
