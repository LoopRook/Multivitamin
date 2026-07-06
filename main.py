import logging
import os
from datetime import datetime
from typing import Literal, Optional

import discord
import pytz
from discord import app_commands

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

TOKEN = os.getenv("DISCORD_TOKEN")

# ── Shared config tables ──────────────────────────────────────────────────────

_FEATURE_MAP: dict[str, tuple[str, str]] = {
    "quote":    ("enable_daily_quote", "Daily Quote"),
    "song":     ("enable_daily_song",  "Daily Song"),
    "cooldown": ("enable_cooldown",    "Cooldown"),
}

_SETUP_TEXT = (
    "**Bot Setup Guide** — all commands are slash (`/`) commands.\n\n"
    "**Channels** (each takes an optional channel; defaults to where you run it):\n"
    "   `/config postchannel` — official rename cards (tracked for brackets)\n"
    "   `/config quotechannel` · `/config iconchannel` · `/config musicchannel`\n"
    "   `/config songpostchannel` · `/config bracketchannel`\n\n"
    "**Features:** `/config feature <quote|song|cooldown> <on/off>`\n"
    "   *(Bracket tracking is always on once a Post Channel is set.)*\n\n"
    "**Scheduling:**\n"
    "   `/config timezone <tz>` — e.g. `US/Eastern`, `Europe/London`\n"
    "   `/config scheduletime <quote|song> <H:MM>`\n\n"
    "**Bracket:**\n"
    "   `/bracket size` · `/bracket votingtime` · `/bracket pacing`\n"
    "   `/bracket start [year] [season]` · `/bracket test`\n"
    "   `/bracket forceadvance` · `/bracket status` · `/bracket cancel`\n\n"
    "**Seasons:** `/season add <start> <end> <name>` · `/season list` · `/season remove`\n\n"
    "**Admins** (Manage Server): `/admin add` · `/admin remove` · `/admin list`\n\n"
    "**Other:** `/showconfig` · `/preview` · `/contributors` · `/mystats`\n\n"
    "**See every command:** `/help`"
)

_WELCOME_TEXT = (
    "👋 **Thanks for adding me!**\n"
    "I rename your server daily from community-submitted quotes, post a daily song, and run "
    "reaction-seeded bracket championships for your favourite names.\n\n"
    "**Get started:** run `/setup` for the full guide, or `/help` to see every command.\n"
    "Most servers begin with `/config postchannel` and `/config quotechannel`.\n"
    "*(Slash commands can take a few minutes to appear right after inviting.)*"
)

# Old prefix commands people might still type — nudge them to slash.
_LEGACY_HINTS = {
    "!help", "!commands", "!setup", "!rename", "!song",
    "!mystats", "!bracketstatus", "!showconfig", "!startbracket", "!testbracket",
}


def build_help_embed(is_admin: bool = False, is_manager: bool = False) -> discord.Embed:
    """
    Slash-command reference grouped by category.
    Admin-only sections are included only when *is_admin* is True; the bot-admin
    roster section is shown only to *is_manager* (Manage Server) users.
    """
    embed = discord.Embed(
        title="📖 Command Reference",
        description="All commands are slash (`/`) commands — type `/` and pick this bot from the list.",
        color=discord.Color.blurple(),
    )
    embed.add_field(
        name="🌐 Everyone",
        value=(
            "`/rename` — trigger a rename now\n"
            "`/song` — post the song of the day\n"
            "`/mystats` — your submission counts & last picks\n"
            "`/help` — show this list"
        ),
        inline=False,
    )
    if not is_admin:
        return embed

    embed.add_field(
        name="📺 Channels (Admin) — `/config …` (optional channel arg)",
        value=(
            "`/config quotechannel` — quote submissions\n"
            "`/config iconchannel` — icon images\n"
            "`/config postchannel` — official rename cards (tracked for brackets)\n"
            "`/config musicchannel` — song links\n"
            "`/config songpostchannel` — daily song posts\n"
            "`/config bracketchannel` — bracket matchups & results"
        ),
        inline=False,
    )
    embed.add_field(
        name="🎚️ Features & Scheduling (Admin)",
        value=(
            "`/config feature <quote|song|cooldown> <on/off>`\n"
            "*(Bracket tracking is always on once a Post Channel is set.)*\n"
            "`/config timezone <tz>` — IANA name, e.g. `US/Eastern`\n"
            "`/config scheduletime <quote|song> <H:MM>`"
        ),
        inline=False,
    )
    embed.add_field(
        name="🏆 Bracket (Admin)",
        value=(
            "`/bracket size <4|8|16|32>` · `/bracket votingtime <hours>`\n"
            "`/bracket pacing <round|daily>` — all at once, or one per day\n"
            "`/bracket start [year] [season]` — seed from a year or a season\n"
            "`/bracket test` — test bracket with random scores\n"
            "`/bracket forceadvance` · `/bracket status` · `/bracket cancel`"
        ),
        inline=False,
    )
    embed.add_field(
        name="📅 Seasons (Admin)",
        value=(
            "`/season add <start> <end> <name>` — dates as `YYYY-MM-DD`\n"
            "`/season list` — list defined seasons\n"
            "`/season remove <name>` — delete a season"
        ),
        inline=False,
    )
    embed.add_field(
        name="ℹ️ Info & Preview (Admin)",
        value=(
            "`/showconfig` — current settings\n"
            "`/contributors <quote|icon|song>` — submission leaderboard\n"
            "`/preview <rename|song>` — dry run, posts here only\n"
            "`/setup` — quick setup guide"
        ),
        inline=False,
    )
    if is_manager:
        embed.add_field(
            name="👑 Bot Admins (Manage Server only)",
            value=(
                "`/admin add <user>` — grant bot-admin access\n"
                "`/admin remove <user>` — revoke bot-admin access\n"
                "`/admin list` — list current bot-admins"
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


# ── Client ────────────────────────────────────────────────────────────────────

class QotdClient(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        # message_content is required to scan quote/icon/song channel history.
        intents.message_content = True
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self._scheduler_task = None

    async def setup_hook(self) -> None:
        # Runs once after login, before the gateway connects — good place to
        # prepare the DB and register/sync slash commands globally.
        init_db()
        await self.tree.sync()
        log.info("✅ Slash commands synced globally.")

    async def on_ready(self) -> None:
        log.info("✅ Logged in as %s (%d guild(s))", self.user, len(self.guilds))
        # (Re)start the scheduler, cancelling any stale task from a prior connect.
        if self._scheduler_task and not self._scheduler_task.done():
            self._scheduler_task.cancel()
            log.info("🔄 Cancelled stale scheduler task.")
        self._scheduler_task = self.loop.create_task(scheduler_loop(self), name="scheduler_loop")
        log.info("⏰ Scheduler started.")

    async def on_guild_join(self, guild: discord.Guild) -> None:
        log.info("➕ Joined guild %s (%s)", guild.id, guild.name)
        get_config(guild.id)  # ensure a config row exists
        channel = _welcome_channel(guild)
        if channel:
            try:
                await channel.send(_WELCOME_TEXT)
            except discord.HTTPException as e:
                log.warning("[%s] Could not send welcome message: %s", guild.id, e)

    async def on_guild_remove(self, guild: discord.Guild) -> None:
        # Keep the guild's data in case of a re-invite; just log the departure.
        log.info("➖ Removed from guild %s (%s) — data retained.", guild.id, guild.name)

    async def on_message(self, message: discord.Message) -> None:
        # The bot uses slash commands now; nudge anyone still typing old ! commands.
        if message.author.bot or not message.guild:
            return
        first = message.content.strip().lower().split(" ", 1)[0]
        if first in _LEGACY_HINTS:
            try:
                await message.channel.send(
                    "ℹ️ I've moved to **slash commands** — type `/help` to see everything "
                    "(start typing `/` and pick me from the list)."
                )
            except discord.HTTPException:
                pass


client = QotdClient()


def _welcome_channel(guild: discord.Guild) -> Optional[discord.abc.Messageable]:
    """Best channel to greet a new guild in: system channel, else first sendable text channel."""
    me = guild.me
    if guild.system_channel and guild.system_channel.permissions_for(me).send_messages:
        return guild.system_channel
    for ch in guild.text_channels:
        if ch.permissions_for(me).send_messages:
            return ch
    return None


# ── Permission checks ─────────────────────────────────────────────────────────

def _perms(interaction: discord.Interaction) -> tuple[bool, bool]:
    """Return (is_admin, is_manager). is_admin = Manage Server OR bot-admin."""
    is_manager = interaction.user.guild_permissions.manage_guild
    is_admin   = is_manager or is_bot_admin(interaction.guild_id, interaction.user.id)
    return is_admin, is_manager


def admin_only():
    async def predicate(interaction: discord.Interaction) -> bool:
        is_admin, _ = _perms(interaction)
        return is_admin
    return app_commands.check(predicate)


def manager_only():
    async def predicate(interaction: discord.Interaction) -> bool:
        return interaction.user.guild_permissions.manage_guild
    return app_commands.check(predicate)


@client.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
    if isinstance(error, app_commands.CheckFailure):
        text = "⚠️ You don't have permission to use this command."
    else:
        log.exception("Slash command error in guild %s: %s", interaction.guild_id, error)
        text = "⚠️ Something went wrong running that command."
    try:
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)
    except discord.HTTPException:
        pass


# ── /config group ─────────────────────────────────────────────────────────────

config_group = app_commands.Group(name="config", description="Server configuration", guild_only=True)


async def _set_channel(interaction: discord.Interaction, field: str, label: str,
                       channel: Optional[discord.TextChannel]) -> None:
    target = channel or interaction.channel
    set_config(interaction.guild_id, field, target.id)
    await interaction.response.send_message(f"✅ {label} set to {target.mention}.", ephemeral=True)


@config_group.command(name="quotechannel", description="Set the channel where users post quotes")
@admin_only()
async def config_quotechannel(interaction: discord.Interaction, channel: Optional[discord.TextChannel] = None):
    await _set_channel(interaction, "quote_channel", "Quote channel", channel)


@config_group.command(name="iconchannel", description="Set the channel where users post icon images")
@admin_only()
async def config_iconchannel(interaction: discord.Interaction, channel: Optional[discord.TextChannel] = None):
    await _set_channel(interaction, "icon_channel", "Icon channel", channel)


@config_group.command(name="postchannel", description="Set the channel where rename cards are posted (tracked for brackets)")
@admin_only()
async def config_postchannel(interaction: discord.Interaction, channel: Optional[discord.TextChannel] = None):
    await _set_channel(interaction, "post_channel", "Post channel", channel)


@config_group.command(name="musicchannel", description="Set the channel where users post song links")
@admin_only()
async def config_musicchannel(interaction: discord.Interaction, channel: Optional[discord.TextChannel] = None):
    await _set_channel(interaction, "music_channel", "Music channel", channel)


@config_group.command(name="songpostchannel", description="Set the channel where the song of the day is posted")
@admin_only()
async def config_songpostchannel(interaction: discord.Interaction, channel: Optional[discord.TextChannel] = None):
    await _set_channel(interaction, "song_post_channel", "Song post channel", channel)


@config_group.command(name="bracketchannel", description="Set the channel where bracket matchups & results post")
@admin_only()
async def config_bracketchannel(interaction: discord.Interaction, channel: Optional[discord.TextChannel] = None):
    await _set_channel(interaction, "bracket_channel", "Bracket channel", channel)


@config_group.command(name="feature", description="Enable or disable a feature")
@app_commands.describe(feature="Which feature", enabled="Turn it on or off")
@admin_only()
async def config_feature(interaction: discord.Interaction,
                         feature: Literal["quote", "song", "cooldown"], enabled: bool):
    field, label = _FEATURE_MAP[feature]
    set_config(interaction.guild_id, field, 1 if enabled else 0)
    verb = "enabled" if enabled else "disabled"
    await interaction.response.send_message(f"✅ {label} feature {verb}.", ephemeral=True)


@config_group.command(name="timezone", description="Set the server timezone (IANA name)")
@app_commands.describe(tz="e.g. US/Eastern, Europe/London, Asia/Tokyo")
@admin_only()
async def config_timezone(interaction: discord.Interaction, tz: str):
    try:
        pytz.timezone(tz)
    except pytz.exceptions.UnknownTimeZoneError:
        await interaction.response.send_message(
            f"⚠️ Unknown timezone `{tz}`. Use a tz database name, e.g. `US/Eastern`, `Europe/London`.\n"
            f"Full list: <https://en.wikipedia.org/wiki/List_of_tz_database_time_zones>",
            ephemeral=True,
        )
        return
    set_config(interaction.guild_id, "timezone", tz)
    await interaction.response.send_message(f"✅ Timezone set to `{tz}`.", ephemeral=True)


@config_group.command(name="scheduletime", description="Set the daily quote or song time (24-hour)")
@app_commands.describe(which="quote or song", time="H:MM or HH:MM, 24-hour, e.g. 8:00")
@admin_only()
async def config_scheduletime(interaction: discord.Interaction,
                              which: Literal["quote", "song"], time: str):
    try:
        h, m = time.split(":")
        assert 0 <= int(h) <= 23 and 0 <= int(m) <= 59
    except Exception:
        await interaction.response.send_message(
            "⚠️ Time must be in `H:MM` or `HH:MM` format (24-hour).", ephemeral=True)
        return
    set_config(interaction.guild_id, "quote_time" if which == "quote" else "song_time", time)
    cfg = get_config(interaction.guild_id)
    tz_name = cfg["timezone"] or "US/Eastern"
    await interaction.response.send_message(
        f"✅ {which.title()} time set to `{time}` ({tz_name}).", ephemeral=True)


client.tree.add_command(config_group)


# ── /bracket group ────────────────────────────────────────────────────────────

bracket_group = app_commands.Group(name="bracket", description="Bracket championship", guild_only=True)


@bracket_group.command(name="size", description="Set the bracket size (power of 2)")
@admin_only()
async def bracket_size(interaction: discord.Interaction, size: Literal[4, 8, 16, 32]):
    set_config(interaction.guild_id, "bracket_size", size)
    await interaction.response.send_message(f"✅ Bracket size set to **{size}**.", ephemeral=True)


@bracket_group.command(name="votingtime", description="Set the voting window per matchup, in hours (1–168)")
@admin_only()
async def bracket_votingtime(interaction: discord.Interaction, hours: app_commands.Range[int, 1, 168]):
    set_config(interaction.guild_id, "bracket_voting_hours", hours)
    await interaction.response.send_message(
        f"✅ Bracket voting window set to **{hours} hour(s)** per matchup.", ephemeral=True)


@bracket_group.command(name="pacing", description="Post all matchups at once, or one per day")
@admin_only()
async def bracket_pacing(interaction: discord.Interaction, mode: Literal["round", "daily"]):
    set_config(interaction.guild_id, "bracket_pacing", mode)
    detail = "matchups will post all at once" if mode == "round" else "one matchup per day"
    await interaction.response.send_message(f"✅ Bracket pacing set to **{mode}** — {detail}.", ephemeral=True)


@bracket_group.command(name="start", description="Start a bracket for a calendar year or a season")
@app_commands.describe(year="Calendar year (defaults to current year)",
                       season="Season name (overrides year if given)")
@admin_only()
async def bracket_start(interaction: discord.Interaction,
                        year: Optional[int] = None, season: Optional[str] = None):
    await interaction.response.defer(ephemeral=True)
    if season:
        _, msg = await start_season_bracket(interaction.guild_id, client, season)
    else:
        y = year or datetime.now().year
        _, msg = await start_bracket(interaction.guild_id, client, y)
    await interaction.followup.send(msg, ephemeral=True)


@bracket_group.command(name="test", description="Start a test bracket from the quote channel (random scores)")
@admin_only()
async def bracket_test(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    _, msg = await start_test_bracket(interaction.guild_id, client)
    await interaction.followup.send(msg, ephemeral=True)


@bracket_group.command(name="forceadvance", description="Tally current polls now and advance the bracket")
@admin_only()
async def bracket_forceadvance(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    _, msg = await force_bracket_advance(interaction.guild_id, client)
    await interaction.followup.send(msg, ephemeral=True)


@bracket_group.command(name="status", description="Show the current bracket's round, pacing & progress")
@admin_only()
async def bracket_status(interaction: discord.Interaction):
    await interaction.response.send_message(get_bracket_status_text(interaction.guild_id), ephemeral=True)


@bracket_group.command(name="cancel", description="Delete the active bracket")
@admin_only()
async def bracket_cancel(interaction: discord.Interaction):
    bracket = get_active_bracket(interaction.guild_id)
    if not bracket:
        await interaction.response.send_message("⚠️ No active bracket to cancel.", ephemeral=True)
        return
    cancel_bracket(bracket["id"])
    await interaction.response.send_message("🗑️ Active bracket cancelled and removed.", ephemeral=True)


client.tree.add_command(bracket_group)


# ── /season group ─────────────────────────────────────────────────────────────

season_group = app_commands.Group(name="season", description="Named date ranges for brackets", guild_only=True)


@season_group.command(name="add", description="Define a season (date range) for scoped brackets")
@app_commands.describe(start="Start date YYYY-MM-DD", end="End date YYYY-MM-DD", name="Season name")
@admin_only()
async def season_add(interaction: discord.Interaction, start: str, end: str, name: str):
    name = name.strip()
    cfg = get_config(interaction.guild_id)
    try:
        tz = pytz.timezone(cfg["timezone"] or "US/Eastern")
    except pytz.exceptions.UnknownTimeZoneError:
        tz = pytz.timezone("US/Eastern")
    try:
        start_utc, end_utc = _parse_season_dates(start, end, tz)
    except ValueError:
        await interaction.response.send_message(
            "⚠️ Invalid dates. Use `YYYY-MM-DD` for both, and make sure the end is on/after the start.",
            ephemeral=True)
        return
    if add_season(interaction.guild_id, name, start_utc, end_utc):
        await interaction.response.send_message(
            f"✅ Season **{name}** created: {start} → {end}.\n"
            f"Start it any time with `/bracket start season:{name}`.", ephemeral=True)
    else:
        await interaction.response.send_message(
            f'⚠️ A season named "{name}" already exists. Remove it first with `/season remove`.', ephemeral=True)


@season_group.command(name="list", description="List defined seasons")
@admin_only()
async def season_list(interaction: discord.Interaction):
    seasons = get_seasons(interaction.guild_id)
    if not seasons:
        await interaction.response.send_message(
            "No seasons defined. Create one with `/season add`.", ephemeral=True)
        return
    lines = ["**Seasons:**"]
    for s in seasons:
        lines.append(f'• **{s["name"]}** — {s["start_at"][:10]} → {s["end_at"][:10]}')
    lines.append("\nStart one with `/bracket start season:<name>`.")
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


@season_group.command(name="remove", description="Delete a season")
@app_commands.describe(name="Season name to delete")
@admin_only()
async def season_remove(interaction: discord.Interaction, name: str):
    if remove_season(interaction.guild_id, name.strip()):
        await interaction.response.send_message(f"🗑️ Season **{name}** removed.", ephemeral=True)
    else:
        await interaction.response.send_message(
            f'⚠️ No season named "{name}". See `/season list`.', ephemeral=True)


client.tree.add_command(season_group)


# ── /admin group (Manage Server only) ─────────────────────────────────────────

admin_group = app_commands.Group(name="admin", description="Bot-admin roster (Manage Server only)", guild_only=True)


@admin_group.command(name="add", description="Grant a user bot-admin access")
@app_commands.describe(user="Member to grant bot-admin access")
@manager_only()
async def admin_add(interaction: discord.Interaction, user: discord.Member):
    if add_bot_admin(interaction.guild_id, user.id, interaction.user.id):
        await interaction.response.send_message(
            f"✅ **{user.display_name}** is now a bot-admin and can use admin commands.", ephemeral=True)
    else:
        await interaction.response.send_message(
            f"ℹ️ **{user.display_name}** is already a bot-admin.", ephemeral=True)


@admin_group.command(name="remove", description="Revoke a user's bot-admin access")
@app_commands.describe(user="Member to revoke bot-admin access from")
@manager_only()
async def admin_remove(interaction: discord.Interaction, user: discord.Member):
    if remove_bot_admin(interaction.guild_id, user.id):
        await interaction.response.send_message(
            f"✅ **{user.display_name}** is no longer a bot-admin.", ephemeral=True)
    else:
        await interaction.response.send_message(
            f"ℹ️ **{user.display_name}** wasn't a bot-admin.", ephemeral=True)


@admin_group.command(name="list", description="List current bot-admins")
@manager_only()
async def admin_list(interaction: discord.Interaction):
    admin_ids = get_bot_admins(interaction.guild_id)
    if not admin_ids:
        await interaction.response.send_message(
            "No bot-admins configured. Anyone with **Manage Server** already has admin access.", ephemeral=True)
        return
    lines = ["**Bot Admins** *(in addition to Manage Server holders)*:"]
    for uid in admin_ids:
        member = interaction.guild.get_member(uid)
        lines.append(f"• {member.display_name if member else f'User {uid}'} (`{uid}`)")
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


client.tree.add_command(admin_group)


# ── Top-level commands ────────────────────────────────────────────────────────

@client.tree.command(name="help", description="Show the command reference")
@app_commands.guild_only()
async def help_cmd(interaction: discord.Interaction):
    is_admin, is_manager = _perms(interaction)
    await interaction.response.send_message(embed=build_help_embed(is_admin, is_manager), ephemeral=True)


@client.tree.command(name="setup", description="Show the setup guide")
@app_commands.guild_only()
@admin_only()
async def setup_cmd(interaction: discord.Interaction):
    await interaction.response.send_message(_SETUP_TEXT, ephemeral=True)


@client.tree.command(name="showconfig", description="Show this server's current settings")
@app_commands.guild_only()
@admin_only()
async def showconfig_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    text = await build_config(interaction.guild_id, client)
    await interaction.followup.send(f"```\n{text}\n```", ephemeral=True)


@client.tree.command(name="contributors", description="Submission leaderboard for a channel")
@app_commands.guild_only()
@admin_only()
async def contributors_cmd(interaction: discord.Interaction, category: Literal["quote", "icon", "song"]):
    await interaction.response.defer(ephemeral=True)
    result = await build_contributors(interaction.guild_id, client, category)
    await interaction.followup.send(result, ephemeral=True)


@client.tree.command(name="preview", description="Dry run a rename or song, posted here only")
@app_commands.guild_only()
@admin_only()
async def preview_cmd(interaction: discord.Interaction, what: Literal["rename", "song"]):
    await interaction.response.defer(ephemeral=True)
    if what == "rename":
        await process_rename(interaction.guild_id, client, override_post_channel=interaction.channel, preview=True)
    else:
        await process_daily_song(interaction.guild_id, client, override_post_channel=interaction.channel, preview=True)
    await interaction.followup.send("✅ Preview posted above.", ephemeral=True)


@client.tree.command(name="mystats", description="Your submission counts and last-picked dates")
@app_commands.guild_only()
async def mystats_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    result = await build_mystats(interaction.guild_id, client, interaction.user.id, interaction.user.display_name)
    await interaction.followup.send(result, ephemeral=True)


@client.tree.command(name="rename", description="Trigger a server rename now")
@app_commands.guild_only()
async def rename_cmd(interaction: discord.Interaction):
    gid = interaction.guild_id
    if get_active_bracket(gid):
        await interaction.response.send_message(
            "⚠️ A bracket is currently running — renames are paused until it finishes. "
            "The winning name will become the server name.", ephemeral=True)
        return
    cfg = get_config(gid)
    if not cfg["enable_daily_quote"]:
        await interaction.response.send_message(
            "⚠️ Daily Quote feature is disabled for this server.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    await process_rename(gid, client, override_post_channel=interaction.channel)
    await interaction.followup.send("✅ Rename posted.", ephemeral=True)


@client.tree.command(name="song", description="Post the song of the day now")
@app_commands.guild_only()
async def song_cmd(interaction: discord.Interaction):
    gid = interaction.guild_id
    cfg = get_config(gid)
    if not cfg["enable_daily_song"]:
        await interaction.response.send_message(
            "⚠️ Daily Song feature is disabled for this server.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    await process_daily_song(gid, client)
    await interaction.followup.send("✅ Song of the day posted.", ephemeral=True)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if not TOKEN:
        log.critical("DISCORD_TOKEN environment variable not set — exiting.")
    else:
        client.run(TOKEN)
