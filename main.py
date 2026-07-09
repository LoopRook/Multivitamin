import logging
import os
import re
from datetime import datetime
from typing import Literal, Optional

import discord
import pytz
from discord import app_commands

from db_utils import (
    init_db, set_config, get_config,
    cancel_bracket, get_active_bracket, get_bracket_history,
    add_bot_admin, remove_bot_admin, get_bot_admins, is_bot_admin, reset_guild,
    add_season, get_seasons, remove_season,
    add_custom_feature, get_custom_features,
    remove_custom_feature, set_custom_feature_enabled, count_custom_features,
    get_custom_feature_by_command, set_custom_feature_access, update_custom_feature,
    get_rename_post_by_message_id, record_forward_nomination,
    get_custom_feature_by_id, set_custom_feature_schedule,
)
from bot_features import (
    process_rename,
    process_custom_daily,
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
    restore_pre_bracket_name,
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

__version__ = "1.0.0"

TOKEN = os.getenv("DISCORD_TOKEN")

# ── Shared config tables ──────────────────────────────────────────────────────

_FEATURE_MAP: dict[str, tuple[str, str]] = {
    "quote":    ("enable_daily_quote", "Daily Quote"),
    "cooldown": ("enable_cooldown",    "Cooldown"),
}

# Cap on admin-defined "X of the day" features per guild (bounds scheduler cost).
_MAX_CUSTOM_FEATURES = 10
# Content types offered by /daily add.
_CUSTOM_TYPE_HELP = {
    "media": "any image/gif/video upload or media link",
    "link":  "any web link",
    "music": "YouTube / Spotify / SoundCloud links",
    "text":  "a line of text",
}


def _valid_hhmm(t: str) -> bool:
    """True if *t* is a valid 24-hour H:MM / HH:MM time."""
    try:
        h, m = t.split(":")
        return 0 <= int(h) <= 23 and 0 <= int(m) <= 59
    except Exception:
        return False

_SETUP_INTRO = (
    "🛠️ **Quick setup** — pick your core channels and timezone below (each saves as you go).\n"
    "Then set up the rest:\n"
    "• `/bracket config` — bracket channel + optional best-of channel\n"
    "• `/daily setup` — your own 'X of the day' posts (meme, song, …)\n"
    "• `/config schedule` — rename time & how often (daily, every N days, or weekdays)\n"
    "• `/help` — every command"
)

# Curated timezones for the /setup dropdown; anything else via `/config timezone`.
_COMMON_TIMEZONES = [
    "US/Eastern", "US/Central", "US/Mountain", "US/Pacific", "America/Sao_Paulo",
    "Europe/London", "Europe/Paris", "Europe/Berlin", "Europe/Moscow", "Asia/Kolkata",
    "Asia/Shanghai", "Asia/Tokyo", "Australia/Sydney", "Pacific/Auckland", "UTC",
]

_WELCOME_TEXT = (
    "👋 **Thanks for adding me!**\n"
    "I rename your server daily from community-submitted quotes, run reaction-seeded bracket "
    "championships, and can post any 'X of the day' you like — meme, critter, song, and more.\n\n"
    "**Get started:** run `/setup` for the full guide, or `/help` to see every command.\n"
    "Most servers begin with `/config postchannel` and `/config quotechannel`, then "
    "`/bracket config` to set the bracket channel and (optionally) a best-of channel members "
    "forward their favorite renames into.\n"
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
            "`/mystats` — your submission counts & last picks\n"
            "`/bracket history` — past bracket champions (hall of champions)\n"
            "`/help` — show this list\n"
            "*Plus any per-feature commands this server has made (e.g. `/meme`, `/song`).*"
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
            "*(Bracket channel + best-of source are set in `/bracket config`.)*"
        ),
        inline=False,
    )
    embed.add_field(
        name="🎚️ Features & Scheduling (Admin)",
        value=(
            "`/config feature <quote|cooldown> <on/off>`\n"
            "*(Bracket tracking is always on once a Post Channel is set.)*\n"
            "`/config timezone <tz>` — IANA name, e.g. `US/Eastern`\n"
            "`/config schedule` — **guided**: rename time & frequency (daily, every N days, or weekdays)"
        ),
        inline=False,
    )
    embed.add_field(
        name="🗓️ Daily features (Admin) — your own 'X of the day'",
        value=(
            "`/daily setup` — **guided** step-by-step (channels, type, name, command)\n"
            "`/daily add <name> <command> <type> <source> <destination> <time> …` — one-shot\n"
            "Each feature gets its **own command** (e.g. `meme` → `/meme`, runs it on demand here).\n"
            "types: `media` (memes/gifs/images), `link`, `music`, `text`\n"
            "`/daily list` · `/daily toggle <command> <on/off>` · `/daily remove <command>`\n"
            "`/daily edit <command> [name] [type] [source] [destination] [time] [emoji]`\n"
            "`/daily schedule <command>` — **guided**: cadence (every N days or weekdays) & time\n"
            "`/daily access <command> <admin|everyone|role> [role]` — who can run it\n"
            "`/preview <command>` — dry-run it (like `/preview rename`)"
        ),
        inline=False,
    )
    embed.add_field(
        name="🏆 Bracket (Admin)",
        value=(
            "`/bracket config` — **guided**: bracket channel + optional best-of source\n"
            "`/bracket start` — **guided**: pick scope · size · voting · pacing → launch\n"
            "`/bracket test` — test bracket with random scores\n"
            "`/bracket forceadvance` · `/bracket status` · `/bracket cancel`"
        ),
        inline=False,
    )
    embed.add_field(
        name="📅 Seasons (Admin)",
        value=(
            "`/season` — **guided** panel: add (form), list, and remove named date windows\n"
            "Pick a season as the scope in `/bracket start`."
        ),
        inline=False,
    )
    embed.add_field(
        name="ℹ️ Info & Preview (Admin)",
        value=(
            "`/showconfig` — current settings + health warnings\n"
            "`/contributors <quote|icon>` — submission leaderboard\n"
            "`/preview <rename|command>` — dry-run a rename or a feature, here only\n"
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
                "`/admin list` — list current bot-admins\n"
                "`/admin reset` — ⚠️ wipe ALL this server's data (irreversible; testing/cleanup)"
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
        self._feature_cmds_loaded = False
        self._guilds_needing_sync: list[int] = []

    async def setup_hook(self) -> None:
        # Runs once after login, before the gateway connects — good place to
        # prepare the DB and register/sync slash commands globally.
        self._guilds_needing_sync = init_db() or []
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

        # Re-wire each guild's per-feature commands (e.g. /meme) into the tree so
        # their callbacks work after a restart. Discord already has them registered
        # from when they were created, so this is in-memory only (no sync). Guilds
        # whose commands changed during a DB migration this boot are re-synced once.
        # Guarded so reconnects don't repeat it.
        if not self._feature_cmds_loaded:
            for g in self.guilds:
                try:
                    _register_guild_feature_commands(g.id)
                except Exception:
                    log.exception("[%s] Failed to load feature commands.", g.id)
            for gid in self._guilds_needing_sync:
                try:
                    await self.tree.sync(guild=discord.Object(id=gid))
                except discord.HTTPException as e:
                    log.warning("[%s] Migration command sync failed: %s", gid, e)
            self._feature_cmds_loaded = True
            log.info("🔀 Per-guild feature commands loaded (%d re-synced).", len(self._guilds_needing_sync))

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
        if message.author.bot or not message.guild:
            return

        # Best-of nominations: react to forwards of rename cards in the source channel.
        await self._handle_forward_nomination(message)

        # The bot uses slash commands now; nudge anyone still typing old ! commands.
        first = message.content.strip().lower().split(" ", 1)[0]
        if first in _LEGACY_HINTS:
            try:
                await message.channel.send(
                    "ℹ️ I've moved to **slash commands** — type `/help` to see everything "
                    "(start typing `/` and pick me from the list)."
                )
            except discord.HTTPException:
                pass

    async def _handle_forward_nomination(self, message: discord.Message) -> None:
        """
        In the configured best-of source channel, react to native forwards of
        rename cards: ℹ️ when it's a valid, first-time nomination (counted), 🔁 when
        that rename was already forwarded here. Non-forwards, and forwards of
        anything that isn't a tracked rename, are ignored.
        """
        cfg = get_config(message.guild.id)
        source_id = cfg["bracket_source_channel"]
        if not source_id or message.channel.id != source_id:
            return
        ref = message.reference
        if not ref or getattr(ref, "type", None) != discord.MessageReferenceType.forward or not ref.message_id:
            return
        post = get_rename_post_by_message_id(message.guild.id, ref.message_id)
        if not post:
            return  # forwarded something that isn't a tracked rename
        is_new = record_forward_nomination(message.guild.id, message.channel.id, post["quote"], message.id)
        try:
            await message.add_reaction("ℹ️" if is_new else "🔁")
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


# ── Per-guild feature commands (e.g. /meme) ───────────────────────────────────
# Feature commands are registered per guild (guild-scoped), so they exist only in
# the server that defined them and update instantly. Names can't collide with the
# bot's global commands/groups.

_SLUG_RE = re.compile(r"^[a-z0-9_-]{1,32}$")
_RESERVED_COMMAND_NAMES = {
    "help", "setup", "showconfig", "contributors", "preview", "mystats", "rename",
    "daily", "bracket", "season", "admin", "config",
}


def _normalize_command_slug(raw: str) -> Optional[str]:
    """Lowercase/trim a command slug and validate it. Returns None if invalid or reserved."""
    s = (raw or "").strip().lower().lstrip("/")
    if not _SLUG_RE.match(s) or s in _RESERVED_COMMAND_NAMES:
        return None
    return s


def _can_run_feature(interaction: discord.Interaction, feat) -> bool:
    """Whether *interaction.user* may run this feature's command."""
    is_admin, _ = _perms(interaction)
    if is_admin:
        return True
    access = feat["run_access"] or "admin"
    if access == "everyone":
        return True
    if access == "roles":
        role_ids = {int(r) for r in (feat["run_roles"] or "").split(",") if r.strip().isdigit()}
        return any(role.id in role_ids for role in getattr(interaction.user, "roles", []))
    return False


def _make_feature_command(command: str, display_name: str) -> app_commands.Command:
    """Build a guild-scoped slash command that posts the named feature on demand."""
    async def _callback(interaction: discord.Interaction):
        feat = get_custom_feature_by_command(interaction.guild_id, command)
        if not feat:
            await interaction.response.send_message(
                "⚠️ That command is no longer configured.", ephemeral=True)
            return
        if not feat["enabled"]:
            await interaction.response.send_message(
                f"⚠️ **{feat['name']}** is currently paused.", ephemeral=True)
            return
        if not _can_run_feature(interaction, feat):
            await interaction.response.send_message(
                "⚠️ You don't have permission to use this command here.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        ok, detail = await process_custom_daily(
            interaction.guild_id, client, feat,
            override_post_channel=interaction.channel, on_demand=True)
        await interaction.followup.send("✅ Posted." if ok else f"⚠️ {detail}", ephemeral=True)

    desc = f"Post a random '{display_name}' now"[:100]
    return app_commands.Command(name=command, description=desc, callback=_callback)


def _register_guild_feature_commands(guild_id: int) -> None:
    """Rebuild a guild's feature commands in the in-memory tree (no network sync)."""
    gobj = discord.Object(id=guild_id)
    for existing in list(client.tree.get_commands(guild=gobj)):
        client.tree.remove_command(existing.name, guild=gobj)
    for feat in get_custom_features(guild_id):
        if feat["command"]:
            client.tree.add_command(_make_feature_command(feat["command"], feat["name"]), guild=gobj)


async def sync_guild_feature_commands(guild_id: int) -> None:
    """Rebuild + push a guild's feature commands to Discord. Call after any change."""
    _register_guild_feature_commands(guild_id)
    try:
        await client.tree.sync(guild=discord.Object(id=guild_id))
    except discord.HTTPException as e:
        log.warning("[%s] Failed to sync guild feature commands: %s", guild_id, e)


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


@config_group.command(name="feature", description="Enable or disable a feature")
@app_commands.describe(feature="Which feature", enabled="Turn it on or off")
@admin_only()
async def config_feature(interaction: discord.Interaction,
                         feature: Literal["quote", "cooldown"], enabled: bool):
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


@config_group.command(name="scheduletime", description="Set the daily rename (quote) time (24-hour)")
@app_commands.describe(which="quote (the daily rename)", time="H:MM or HH:MM, 24-hour, e.g. 8:00")
@admin_only()
async def config_scheduletime(interaction: discord.Interaction,
                              which: Literal["quote"], time: str):
    if not _valid_hhmm(time):
        await interaction.response.send_message(
            "⚠️ Time must be in `H:MM` or `HH:MM` format (24-hour).", ephemeral=True)
        return
    set_config(interaction.guild_id, "quote_time", time)
    cfg = get_config(interaction.guild_id)
    tz_name = cfg["timezone"] or "US/Eastern"
    await interaction.response.send_message(
        f"✅ Quote (rename) time set to `{time}` ({tz_name}).", ephemeral=True)


# ── /config schedule — guided rename time + frequency ─────────────────────────

_INTERVAL_CHOICES = [(1, "Daily"), (2, "Every 2 days"), (3, "Every 3 days"),
                     (5, "Every 5 days"), (7, "Every 7 days"),
                     (14, "Every 14 days"), (30, "Every 30 days")]
_WEEKDAY_CHOICES = [("0", "Monday"), ("1", "Tuesday"), ("2", "Wednesday"), ("3", "Thursday"),
                    ("4", "Friday"), ("5", "Saturday"), ("6", "Sunday")]
_WEEKDAY_ABBR = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


class _ScheduleTimeModal(discord.ui.Modal, title="Set the rename time"):
    time_in = discord.ui.TextInput(label="Time (24-hour H:MM, server timezone)",
                                   placeholder="8:00", max_length=5)

    def __init__(self, view: "_ScheduleView"):
        super().__init__()
        self._view = view

    async def on_submit(self, interaction: discord.Interaction):
        if not _valid_hhmm(self.time_in.value):
            await interaction.response.send_message(
                "⚠️ Time must be `H:MM` (24-hour), e.g. `8:00`.", ephemeral=True)
            return
        self._view._save_time(self.time_in.value.strip())
        await interaction.response.edit_message(content=self._view._render(), view=self._view)


class _ScheduleView(discord.ui.View):
    """
    Guided schedule: cadence (every N days OR specific weekdays) + time. Targets
    the server rename by default, or a custom feature when *feature* is given.
    """
    def __init__(self, author_id: int, guild_id: int, feature=None):
        super().__init__(timeout=300)
        self.author_id   = author_id
        self.guild_id    = guild_id
        self.feature_id  = feature["id"] if feature else None
        self.target_name = feature["name"] if feature else "the server rename"
        if feature:
            wd = (feature["weekdays"] or "").strip()
            self.interval = feature["interval_days"] or 1
        else:
            cfg = get_config(guild_id)
            wd = (cfg["quote_weekdays"] or "").strip()
            self.interval = cfg["quote_interval_days"] or 1
        self.mode     = "weekly" if wd else "interval"
        self.weekdays = {int(x) for x in wd.split(",") if x.strip().isdigit()} if wd else set()

        self.mode_select.options = [
            discord.SelectOption(label="Every N days", value="interval", default=(self.mode == "interval")),
            discord.SelectOption(label="Specific weekdays", value="weekly", default=(self.mode == "weekly")),
        ]
        self.interval_select.options = [
            discord.SelectOption(label=lbl, value=str(n), default=(n == self.interval))
            for n, lbl in _INTERVAL_CHOICES
        ]
        self.weekday_select.options = [
            discord.SelectOption(label=lbl, value=val, default=(int(val) in self.weekdays))
            for val, lbl in _WEEKDAY_CHOICES
        ]

    def _current_time(self) -> str:
        if self.feature_id:
            f = get_custom_feature_by_id(self.feature_id)
            return f["post_time"] if f else "?"
        return get_config(self.guild_id)["quote_time"] or "4:00"

    def _save_time(self, t: str) -> None:
        if self.feature_id:
            update_custom_feature(self.feature_id, post_time=t)
        else:
            set_config(self.guild_id, "quote_time", t)

    def _save_cadence(self) -> None:
        wd = ",".join(str(d) for d in sorted(self.weekdays)) if self.mode == "weekly" else None
        if self.feature_id:
            set_custom_feature_schedule(self.feature_id, self.interval, wd)
        elif self.mode == "weekly":
            set_config(self.guild_id, "quote_weekdays", wd)
        else:
            set_config(self.guild_id, "quote_weekdays", None)
            set_config(self.guild_id, "quote_interval_days", self.interval)

    def _render(self) -> str:
        tz = get_config(self.guild_id)["timezone"] or "US/Eastern"
        if self.mode == "weekly":
            days = ", ".join(_WEEKDAY_ABBR[d] for d in sorted(self.weekdays)) if self.weekdays \
                else "*(pick weekday(s) below)*"
            cad = f"Weekly on {days}"
        else:
            cad = "Daily" if self.interval <= 1 else f"Every {self.interval} days"
        return (
            f"🗓️ **Schedule — {self.target_name}**\n"
            f"• Frequency: **{cad}**\n"
            f"• Time: **{self._current_time()}** ({tz})\n\n"
            "Pick a **mode**, set the matching option below, optionally set the time, then **Save**. "
            "*(Weekday mode is how you get 'every Sunday'.)*"
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "This panel isn't yours — run `/config schedule` yourself.", ephemeral=True)
            return False
        return True

    @discord.ui.select(placeholder="Mode — every N days, or specific weekdays",
                       options=[discord.SelectOption(label="Every N days", value="interval")], row=0)
    async def mode_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        self.mode = select.values[0]
        await interaction.response.edit_message(content=self._render(), view=self)

    @discord.ui.select(placeholder="Every N days (interval mode)",
                       options=[discord.SelectOption(label="Daily", value="1")], row=1)
    async def interval_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        self.interval = int(select.values[0])
        self.mode = "interval"
        await interaction.response.edit_message(content=self._render(), view=self)

    @discord.ui.select(placeholder="Weekdays (weekday mode)", min_values=1, max_values=7,
                       options=[discord.SelectOption(label="Sunday", value="6")], row=2)
    async def weekday_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        self.weekdays = {int(v) for v in select.values}
        self.mode = "weekly"
        await interaction.response.edit_message(content=self._render(), view=self)

    @discord.ui.button(label="Set time", emoji="🕐", style=discord.ButtonStyle.secondary, row=3)
    async def time_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(_ScheduleTimeModal(self))

    @discord.ui.button(label="Save", emoji="✅", style=discord.ButtonStyle.success, row=3)
    async def save_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.mode == "weekly" and not self.weekdays:
            await interaction.response.send_message(
                "⚠️ Pick at least one weekday, or switch the mode to 'Every N days'.", ephemeral=True)
            return
        self._save_cadence()
        for c in self.children:
            c.disabled = True
        await interaction.response.edit_message(content=self._render() + "\n\n✅ **Saved.**", view=self)
        self.stop()


@config_group.command(name="schedule", description="Set how often & when the server renames (every N days or weekdays)")
@admin_only()
async def config_schedule(interaction: discord.Interaction):
    view = _ScheduleView(interaction.user.id, interaction.guild_id)
    await interaction.response.send_message(view._render(), view=view, ephemeral=True)


client.tree.add_command(config_group)


# ── /bracket group ────────────────────────────────────────────────────────────

bracket_group = app_commands.Group(name="bracket", description="Bracket championship", guild_only=True)


_SOURCE_INSTRUCTIONS = (
    "📌 **Best-of nominations**\n"
    "**Forward** your favorite rename cards into this channel (use Discord's **Forward** button — "
    "screenshots and re-uploads don't count) to nominate them for the bracket.\n"
    "• I'll react ℹ️ when a forward is counted, or 🔁 if that rename was already forwarded here.\n"
    "• **React** to the forwards you like — the most-reacted renames get seeded into the bracket."
)


async def _post_source_instructions(guild: discord.Guild, channel_id: int) -> None:
    """Post the how-to-nominate message in a newly set best-of channel and pin it (best effort)."""
    channel = guild.get_channel(channel_id)
    if not channel:
        return
    try:
        msg = await channel.send(_SOURCE_INSTRUCTIONS)
    except discord.HTTPException:
        return
    try:
        await msg.pin()
    except discord.HTTPException:
        pass  # missing Manage Messages — leave it unpinned


class _BracketConfigView(discord.ui.View):
    """Persistent bracket setup: where matchups post + the optional best-of source channel."""
    def __init__(self, author_id: int, guild_id: int):
        super().__init__(timeout=300)
        self.author_id = author_id
        self.guild_id  = guild_id

    def _render(self) -> str:
        cfg = get_config(self.guild_id)
        bc  = f"<#{cfg['bracket_channel']}>" if cfg["bracket_channel"] else "*not set*"
        src = f"<#{cfg['bracket_source_channel']}>" if cfg["bracket_source_channel"] else "*all tracked renames*"
        return (
            "🏆 **Bracket setup**\n"
            f"• Matchups post to: {bc}\n"
            f"• Seeded from: {src}\n\n"
            "Pick the channels below. The **best-of source** is optional — members "
            "**forward** rename cards there to nominate them (Discord's Forward button). "
            "Use **🚫 No best-of** to seed from every tracked rename instead. Hit **✅ Done** when finished."
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "This panel isn't yours — run `/bracket config` yourself.", ephemeral=True)
            return False
        return True

    @discord.ui.select(cls=discord.ui.ChannelSelect, channel_types=[discord.ChannelType.text],
                       placeholder="Bracket channel — where matchups & results post", row=0)
    async def bracket_channel_select(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect):
        set_config(self.guild_id, "bracket_channel", select.values[0].id)
        await interaction.response.edit_message(content=self._render(), view=self)

    @discord.ui.select(cls=discord.ui.ChannelSelect, channel_types=[discord.ChannelType.text],
                       placeholder="Best-of source — members forward renames here (optional)", row=1)
    async def source_channel_select(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect):
        new_id = select.values[0].id
        old_id = get_config(self.guild_id)["bracket_source_channel"]
        set_config(self.guild_id, "bracket_source_channel", new_id)
        await interaction.response.edit_message(content=self._render(), view=self)
        if new_id != old_id:
            await _post_source_instructions(interaction.guild, new_id)

    @discord.ui.button(label="No best-of", style=discord.ButtonStyle.secondary, emoji="🚫", row=2)
    async def clear_source_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        set_config(self.guild_id, "bracket_source_channel", None)
        await interaction.response.edit_message(content=self._render(), view=self)

    @discord.ui.button(label="Done", style=discord.ButtonStyle.success, emoji="✅", row=2)
    async def done_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        for c in self.children:
            c.disabled = True
        cfg = get_config(self.guild_id)
        bc  = f"<#{cfg['bracket_channel']}>" if cfg["bracket_channel"] else "*not set*"
        src = f"<#{cfg['bracket_source_channel']}>" if cfg["bracket_source_channel"] else "*all tracked renames*"
        await interaction.response.edit_message(
            content=(f"🏁 **Bracket setup saved.**\n• Matchups → {bc}\n• Seeded from → {src}\n\n"
                     "Start a bracket any time with `/bracket start`."),
            view=self)
        self.stop()


_VOTING_CHOICES = [(6, "6 hours"), (12, "12 hours"), (24, "24 hours (1 day)"),
                   (48, "48 hours (2 days)"), (72, "72 hours (3 days)"), (168, "168 hours (1 week)")]


class _BracketStartView(discord.ui.View):
    """Per-run bracket launcher — scope, size, voting window & pacing, pre-filled to last-used."""
    def __init__(self, author_id: int, guild_id: int):
        super().__init__(timeout=300)
        self.author_id = author_id
        self.guild_id  = guild_id
        cfg = get_config(guild_id)
        self.scope  = "year"
        self.size   = int(cfg["bracket_size"] or 8)
        self.voting = int(cfg["bracket_voting_hours"] or 24)
        self.pacing = cfg["bracket_pacing"] or "round"

        scope_opts = [discord.SelectOption(
            label=f"This year ({datetime.now().year})", value="year", default=True)]
        for s in get_seasons(guild_id)[:24]:
            scope_opts.append(discord.SelectOption(label=f'Season: {s["name"]}'[:100],
                                                   value=f'season:{s["name"]}'[:100]))
        self.scope_select.options = scope_opts

        self.size_select.options = [
            discord.SelectOption(label=f"{n} names", value=str(n), default=(n == self.size))
            for n in (4, 8, 16, 32)
        ]
        self.voting_select.options = [
            discord.SelectOption(label=lbl, value=str(v), default=(v == self.voting))
            for v, lbl in _VOTING_CHOICES
        ]
        self.pacing_select.options = [
            discord.SelectOption(label="Round — all matchups at once", value="round",
                                 default=(self.pacing == "round")),
            discord.SelectOption(label="Daily — one matchup per day", value="daily",
                                 default=(self.pacing == "daily")),
        ]

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "This panel isn't yours — run `/bracket start` yourself.", ephemeral=True)
            return False
        return True

    @discord.ui.select(placeholder="Scope — this year or a season",
                       options=[discord.SelectOption(label="This year", value="year")], row=0)
    async def scope_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        self.scope = select.values[0]
        await interaction.response.defer()

    @discord.ui.select(placeholder="Size — how many names",
                       options=[discord.SelectOption(label="8 names", value="8")], row=1)
    async def size_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        self.size = int(select.values[0])
        await interaction.response.defer()

    @discord.ui.select(placeholder="Voting window per matchup",
                       options=[discord.SelectOption(label="24 hours", value="24")], row=2)
    async def voting_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        self.voting = int(select.values[0])
        await interaction.response.defer()

    @discord.ui.select(placeholder="Pacing — all at once or one per day",
                       options=[discord.SelectOption(label="Round", value="round")], row=3)
    async def pacing_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        self.pacing = select.values[0]
        await interaction.response.defer()

    @discord.ui.button(label="Launch bracket", style=discord.ButtonStyle.success, emoji="🚀", row=4)
    async def launch_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        for c in self.children:
            c.disabled = True
        scope_label = "this year" if self.scope == "year" else self.scope.split(":", 1)[1]
        await interaction.response.edit_message(
            content=(f"🚀 **Launching** — {scope_label} · {self.size} names · {self.voting}h · {self.pacing}. "
                     "Watch the bracket channel."),
            view=self)
        self.stop()
        # Remember these as the new defaults (pre-fill for next time).
        set_config(self.guild_id, "bracket_size", self.size)
        set_config(self.guild_id, "bracket_voting_hours", self.voting)
        set_config(self.guild_id, "bracket_pacing", self.pacing)
        if self.scope.startswith("season:"):
            _, msg = await start_season_bracket(
                self.guild_id, client, self.scope.split(":", 1)[1],
                size=self.size, voting_hours=self.voting, pacing=self.pacing)
        else:
            _, msg = await start_bracket(
                self.guild_id, client, datetime.now().year,
                size=self.size, voting_hours=self.voting, pacing=self.pacing)
        await interaction.followup.send(msg, ephemeral=True)


@bracket_group.command(name="config", description="Set the bracket channel + optional best-of source channel")
@admin_only()
async def bracket_config(interaction: discord.Interaction):
    view = _BracketConfigView(interaction.user.id, interaction.guild_id)
    await interaction.response.send_message(view._render(), view=view, ephemeral=True)


@bracket_group.command(name="start", description="Start a bracket — pick scope, size, voting & pacing")
@admin_only()
async def bracket_start(interaction: discord.Interaction):
    view = _BracketStartView(interaction.user.id, interaction.guild_id)
    await interaction.response.send_message(
        "🏆 **Start a bracket** — settings are pre-filled to your last run; adjust any, then hit **🚀 Launch**.",
        view=view, ephemeral=True)


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


@bracket_group.command(name="history", description="Past bracket champions for this server")
async def bracket_history(interaction: discord.Interaction):
    rows = get_bracket_history(interaction.guild_id)
    if not rows:
        await interaction.response.send_message(
            "🏆 No completed brackets yet — run one with `/bracket start`.", ephemeral=True)
        return
    lines = ["🏆 **Hall of Champions**"]
    for r in rows:
        label = r["label"] or str(r["year"])
        date  = (r["created_at"] or "")[:10]
        champ = r["champion_quote"]
        if champ:
            who = f" — *{r['champion_user']}*" if r["champion_user"] else ""
            lines.append(f'• **{label}** ({date}): "{champ}"{who}')
        else:
            lines.append(f"• **{label}** ({date}): *champion not recorded*")
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


@bracket_group.command(name="cancel", description="Delete the active bracket")
@admin_only()
async def bracket_cancel(interaction: discord.Interaction):
    bracket = get_active_bracket(interaction.guild_id)
    if not bracket:
        await interaction.response.send_message("⚠️ No active bracket to cancel.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    cancel_bracket(bracket["id"])
    msg = "🗑️ Active bracket cancelled and removed."
    # A real bracket may have changed the server name mid-run; put it back.
    if bracket["year"] != 0:
        restored = await restore_pre_bracket_name(client, interaction.guild_id)
        if restored:
            msg += f"\nServer name restored to **{restored}**."
    await interaction.followup.send(msg, ephemeral=True)


client.tree.add_command(bracket_group)


# ── /season — named date ranges for brackets (guided panel) ───────────────────

def _season_panel_text(guild_id: int) -> str:
    seasons = get_seasons(guild_id)
    if seasons:
        body = "\n".join(f'• **{s["name"]}** — {s["start_at"][:10]} → {s["end_at"][:10]}' for s in seasons)
    else:
        body = "*No seasons defined yet.*"
    return (
        "📅 **Seasons** — named date windows you can seed a bracket from (a month, a holiday, etc.).\n\n"
        f"{body}\n\n"
        "Use **➕ Add season** to define one, or the dropdown to remove one. "
        "Then pick it as the scope in `/bracket start`."
    )


class _SeasonModal(discord.ui.Modal, title="Add a season"):
    start_in = discord.ui.TextInput(label="Start date (YYYY-MM-DD)", placeholder="2026-10-01", max_length=10)
    end_in   = discord.ui.TextInput(label="End date (YYYY-MM-DD)", placeholder="2026-10-31", max_length=10)
    name_in  = discord.ui.TextInput(label="Season name", placeholder="Halloween 2026", max_length=80)

    def __init__(self, view: "_SeasonView"):
        super().__init__()
        self._view = view

    async def on_submit(self, interaction: discord.Interaction):
        gid  = interaction.guild_id
        name = self.name_in.value.strip()
        cfg  = get_config(gid)
        try:
            tz = pytz.timezone(cfg["timezone"] or "US/Eastern")
        except pytz.exceptions.UnknownTimeZoneError:
            tz = pytz.timezone("US/Eastern")
        try:
            start_utc, end_utc = _parse_season_dates(self.start_in.value, self.end_in.value, tz)
        except ValueError:
            await interaction.response.send_message(
                "⚠️ Invalid dates. Use `YYYY-MM-DD` for both, and make sure the end is on/after the start.",
                ephemeral=True)
            return
        if add_season(gid, name, start_utc, end_utc):
            await interaction.response.edit_message(
                content=_season_panel_text(gid), view=_SeasonView(self._view.author_id, gid))
        else:
            await interaction.response.send_message(
                f'⚠️ A season named "{name}" already exists.', ephemeral=True)


class _SeasonView(discord.ui.View):
    """Manage seasons: list, remove via dropdown, add via a modal form."""
    def __init__(self, author_id: int, guild_id: int):
        super().__init__(timeout=300)
        self.author_id = author_id
        self.guild_id  = guild_id
        seasons = get_seasons(guild_id)
        if seasons:
            self.remove_select.options = [
                discord.SelectOption(label=s["name"][:100], value=s["name"][:100],
                                     description=f'{s["start_at"][:10]} → {s["end_at"][:10]}')
                for s in seasons[:25]
            ]
        else:
            self.remove_select.disabled = True
            self.remove_select.placeholder = "No seasons to remove yet"
            self.remove_select.options = [discord.SelectOption(label="(none)", value="__none__")]

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "This panel isn't yours — run `/season` yourself.", ephemeral=True)
            return False
        return True

    @discord.ui.select(placeholder="Remove a season…",
                       options=[discord.SelectOption(label="(none)", value="__none__")], row=0)
    async def remove_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        val = select.values[0]
        if val == "__none__":
            await interaction.response.defer()
            return
        remove_season(self.guild_id, val)
        await interaction.response.edit_message(
            content=_season_panel_text(self.guild_id), view=_SeasonView(self.author_id, self.guild_id))

    @discord.ui.button(label="Add season", style=discord.ButtonStyle.success, emoji="➕", row=1)
    async def add_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(_SeasonModal(self))


@client.tree.command(name="season", description="Manage bracket seasons (named date windows)")
@app_commands.guild_only()
@admin_only()
async def season_cmd(interaction: discord.Interaction):
    view = _SeasonView(interaction.user.id, interaction.guild_id)
    await interaction.response.send_message(
        _season_panel_text(interaction.guild_id), view=view, ephemeral=True)


# ── /daily group — admin-defined "X of the day" features ──────────────────────

daily_group = app_commands.Group(name="daily", description="Custom 'X of the day' features", guild_only=True)


_ACCESS_LABELS = {"admin": "admin", "everyone": "all", "roles": "roles"}


def _feature_cadence(f) -> str:
    """Compact cadence label for /daily list."""
    wd = (f["weekdays"] or "").strip()
    if wd:
        days = [_WEEKDAY_ABBR[int(x)] for x in wd.split(",") if x.strip().isdigit() and 0 <= int(x) <= 6]
        return ", ".join(days) if days else "daily"
    n = f["interval_days"] or 1
    return "daily" if n <= 1 else f"every {n}d"


def _feature_summary(f) -> str:
    src  = f"<#{f['source_channel']}>"
    dst  = f"<#{f['post_channel']}>"
    flag = "🟢" if f["enabled"] else "⚪"
    emo  = (f["emoji"] + " ") if f["emoji"] else ""
    cmd  = f"`/{f['command']}`" if f["command"] else "`(no command)`"
    acc  = _ACCESS_LABELS.get(f['run_access'] or 'admin', 'admin')
    return (f"{flag} {cmd} — {emo}**{f['name']}** · `{f['content_type']}` · "
            f"{src} → {dst} · {f['post_time']} ({_feature_cadence(f)}) · run: {acc}")


def _resolve_command_slug(guild_id: int, raw: Optional[str], current_name: Optional[str] = None,
                          required: bool = False):
    """
    Validate a requested command slug. Returns (slug|None, error|None).
    Empty/None raw → (None, error) if *required*, else (None, None).
    """
    if not raw or not raw.strip():
        return None, ("A command is required — pick a short one like `meme`." if required else None)
    slug = _normalize_command_slug(raw)
    if slug is None:
        return None, ("Command name must be 1–32 chars of lowercase letters/numbers/`-`/`_`, "
                      "and can't be a reserved name (help, song, daily, …).")
    existing = get_custom_feature_by_command(guild_id, slug)
    if existing and (current_name is None or existing["name"].lower() != current_name.lower()):
        return None, f"`/{slug}` is already used by **{existing['name']}**. Pick another."
    return slug, None


def _access_from_choice(access: str, role):
    """Map a /daily access|add choice to (run_access, run_roles, error)."""
    if access == "role":
        if role is None:
            return None, None, "Pick a **role** when access is `role`."
        return "roles", str(role.id), None
    if access == "everyone":
        return "everyone", None, None
    return "admin", None, None


def _create_daily_feature(guild_id: int, name: str, emoji: str | None, ctype: str,
                          source_id: int, dest_id: int, time: str,
                          command: str | None = None, run_access: str = "admin",
                          run_roles: str | None = None) -> tuple[bool, str]:
    """
    Validate inputs and create a custom feature. Returns (ok, message). Shared by
    `/daily add` and the guided `/daily setup` flow so both enforce the same rules.
    The caller syncs guild commands when *command* is set.
    """
    name = (name or "").strip()
    if not name:
        return False, "⚠️ Name can't be empty."
    if not _valid_hhmm(time):
        return False, "⚠️ Time must be `H:MM` or `HH:MM` (24-hour), e.g. `12:00`."
    if count_custom_features(guild_id) >= _MAX_CUSTOM_FEATURES:
        return False, f"⚠️ You've reached the limit of {_MAX_CUSTOM_FEATURES} daily features. Remove one first."
    if add_custom_feature(guild_id, name, (emoji or None), ctype, source_id, dest_id, time,
                          command=command, run_access=run_access, run_roles=run_roles):
        return True, (f"✅ **{name}** created ({_CUSTOM_TYPE_HELP[ctype]}): <#{source_id}> → <#{dest_id}> "
                      f"daily at `{time}`.\nMembers can run it with `/{command}`; dry-run it with "
                      f"`/preview {command}`. Set who can use it with `/daily access`.")
    return False, f'⚠️ A feature named "{name}" already exists. Remove it (`/daily remove`) or pick another name.'


# ── Guided /daily setup (channel pickers + type dropdown + a name/time form) ───

_TYPE_SELECT_OPTIONS = [
    discord.SelectOption(label="Media — memes, gifs, images, videos", value="media", emoji="🖼️"),
    discord.SelectOption(label="Link — any web link", value="link", emoji="🔗"),
    discord.SelectOption(label="Music — YouTube / Spotify / SoundCloud", value="music", emoji="🎵"),
    discord.SelectOption(label="Text — a line of text", value="text", emoji="💬"),
]


class _DailyNameModal(discord.ui.Modal, title="Name your daily feature"):
    """Final step of /daily setup — name, then a required command, time, optional emoji."""
    name_in    = discord.ui.TextInput(label="Name", placeholder="Meme of the Day", max_length=80)
    command_in = discord.ui.TextInput(label="Command (required, e.g. meme → /meme)",
                                      max_length=32, placeholder="meme")
    time_in    = discord.ui.TextInput(label="Time (24-hour H:MM, server timezone)", placeholder="12:00", max_length=5)
    emoji_in   = discord.ui.TextInput(label="Emoji (optional)", required=False, max_length=8, placeholder="🖼️")

    def __init__(self, view: "_DailySetupView"):
        super().__init__()
        self._view = view

    async def on_submit(self, interaction: discord.Interaction):
        slug, err = _resolve_command_slug(interaction.guild_id, self.command_in.value, required=True)
        if err:
            await interaction.response.send_message(f"⚠️ {err}", ephemeral=True)
            return
        ok, msg = _create_daily_feature(
            interaction.guild_id, self.name_in.value, self.emoji_in.value,
            self._view.ctype, self._view.source_id, self._view.dest_id, self.time_in.value,
            command=slug,
        )
        if ok:
            await sync_guild_feature_commands(interaction.guild_id)
        await interaction.response.send_message(msg, ephemeral=True)


class _DailySetupView(discord.ui.View):
    """Ephemeral panel: source picker, destination picker, type dropdown, create button."""
    def __init__(self, author_id: int):
        super().__init__(timeout=300)
        self.author_id = author_id
        self.source_id: Optional[int] = None
        self.dest_id:   Optional[int] = None
        self.ctype:     Optional[str] = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "This setup panel isn't yours — run `/daily setup` yourself.", ephemeral=True)
            return False
        return True

    @discord.ui.select(cls=discord.ui.ChannelSelect, channel_types=[discord.ChannelType.text],
                       placeholder="1) Source — the channel to pick from", row=0)
    async def source_select(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect):
        self.source_id = select.values[0].id
        await interaction.response.defer()

    @discord.ui.select(cls=discord.ui.ChannelSelect, channel_types=[discord.ChannelType.text],
                       placeholder="2) Destination — where to post it", row=1)
    async def dest_select(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect):
        self.dest_id = select.values[0].id
        await interaction.response.defer()

    @discord.ui.select(placeholder="3) Type — what to pick", options=_TYPE_SELECT_OPTIONS, row=2)
    async def type_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        self.ctype = select.values[0]
        await interaction.response.defer()

    @discord.ui.button(label="Name it & create", style=discord.ButtonStyle.success, emoji="✅", row=3)
    async def create_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        missing = [label for label, val in
                   (("source", self.source_id), ("destination", self.dest_id), ("type", self.ctype)) if not val]
        if missing:
            await interaction.response.send_message(
                f"⚠️ Pick **{', '.join(missing)}** above first, then hit create.", ephemeral=True)
            return
        await interaction.response.send_modal(_DailyNameModal(self))


@daily_group.command(name="setup", description="Guided, step-by-step setup for a custom daily feature")
@admin_only()
async def daily_setup(interaction: discord.Interaction):
    await interaction.response.send_message(
        "🛠️ **New daily feature** — pick a **source** channel, a **destination**, and a **type** below, "
        "then hit **Name it & create** to set the name, time, emoji, and (optionally) a slash command "
        "like `/meme`. Set who can use that command afterward with `/daily access`.",
        view=_DailySetupView(interaction.user.id), ephemeral=True)


@daily_group.command(name="add", description="Create a custom 'X of the day' in one command")
@app_commands.describe(
    name="Display name, e.g. 'Meme of the Day'",
    command="Its slash command, e.g. 'meme' → members run /meme (required)",
    type="What to pick — media (memes/gifs/images), link, music, or text",
    source="Channel to pick from",
    destination="Channel to post into",
    time="Daily post time, 24-hour H:MM (server timezone)",
    emoji="Optional emoji shown before the name",
    access="Who may run the command (default admin)",
    role="Role allowed to run it (when access is 'role')",
)
@admin_only()
async def daily_add(interaction: discord.Interaction, name: str, command: str,
                    type: Literal["media", "link", "music", "text"],
                    source: discord.TextChannel, destination: discord.TextChannel,
                    time: str, emoji: Optional[str] = None,
                    access: Literal["admin", "everyone", "role"] = "admin",
                    role: Optional[discord.Role] = None):
    slug, cerr = _resolve_command_slug(interaction.guild_id, command, required=True)
    if cerr:
        await interaction.response.send_message(f"⚠️ {cerr}", ephemeral=True)
        return
    run_access, run_roles, aerr = _access_from_choice(access, role)
    if aerr:
        await interaction.response.send_message(f"⚠️ {aerr}", ephemeral=True)
        return
    ok, msg = _create_daily_feature(interaction.guild_id, name, emoji, type,
                                    source.id, destination.id, time,
                                    command=slug, run_access=run_access, run_roles=run_roles)
    if ok:
        await sync_guild_feature_commands(interaction.guild_id)
    await interaction.response.send_message(msg, ephemeral=True)


async def _feature_slug_autocomplete(interaction: discord.Interaction, current: str):
    """Suggest this guild's feature command slugs."""
    cur = (current or "").lower()
    slugs = [f["command"] for f in get_custom_features(interaction.guild_id) if f["command"]]
    return [app_commands.Choice(name=s, value=s) for s in slugs if cur in s.lower()][:25]


@daily_group.command(name="edit", description="Change a feature's channels, time, type, name, or emoji")
@app_commands.describe(
    command="The feature's command, e.g. meme",
    name="New display name",
    type="New content type",
    source="New channel to pick from",
    destination="New channel to post into",
    time="New daily time, 24-hour H:MM",
    emoji="New emoji prefix",
)
@app_commands.autocomplete(command=_feature_slug_autocomplete)
@admin_only()
async def daily_edit(interaction: discord.Interaction, command: str,
                     name: Optional[str] = None,
                     type: Optional[Literal["media", "link", "music", "text"]] = None,
                     source: Optional[discord.TextChannel] = None,
                     destination: Optional[discord.TextChannel] = None,
                     time: Optional[str] = None,
                     emoji: Optional[str] = None):
    feat = get_custom_feature_by_command(interaction.guild_id, command.strip().lower())
    if not feat:
        await interaction.response.send_message(
            f'⚠️ No feature with command `/{command}`. See `/daily list`.', ephemeral=True)
        return
    if time is not None and not _valid_hhmm(time):
        await interaction.response.send_message(
            "⚠️ Time must be `H:MM` or `HH:MM` (24-hour), e.g. `12:00`.", ephemeral=True)
        return
    fields = {}
    if name is not None:        fields["name"] = name.strip()
    if type is not None:        fields["content_type"] = type
    if source is not None:      fields["source_channel"] = source.id
    if destination is not None: fields["post_channel"] = destination.id
    if time is not None:        fields["post_time"] = time
    if emoji is not None:       fields["emoji"] = emoji.strip()
    if not fields:
        await interaction.response.send_message(
            "⚠️ Nothing to change — set at least one field (e.g. `destination`).", ephemeral=True)
        return
    if not update_custom_feature(feat["id"], **fields):
        await interaction.response.send_message(
            f'⚠️ Another feature is already named "{fields.get("name")}". Pick a different name.', ephemeral=True)
        return
    # The /command's description embeds the display name, so refresh it on a rename.
    if "name" in fields:
        await sync_guild_feature_commands(interaction.guild_id)
    updated = get_custom_feature_by_command(interaction.guild_id, feat["command"])
    await interaction.response.send_message(
        f"✅ Updated `/{updated['command']}`.\n{_feature_summary(updated)}", ephemeral=True)


@daily_group.command(name="schedule", description="Set a feature's cadence (every N days or weekdays) & time")
@app_commands.describe(command="The feature's command slug")
@app_commands.autocomplete(command=_feature_slug_autocomplete)
@admin_only()
async def daily_schedule(interaction: discord.Interaction, command: str):
    feat = get_custom_feature_by_command(interaction.guild_id, command.strip().lower())
    if not feat:
        await interaction.response.send_message(
            f"⚠️ No feature with command `/{command}`. See `/daily list`.", ephemeral=True)
        return
    view = _ScheduleView(interaction.user.id, interaction.guild_id, feature=feat)
    await interaction.response.send_message(view._render(), view=view, ephemeral=True)


@daily_group.command(name="list", description="List this server's custom daily features")
@admin_only()
async def daily_list(interaction: discord.Interaction):
    feats = get_custom_features(interaction.guild_id)
    if not feats:
        await interaction.response.send_message(
            "No custom daily features yet. Create one with `/daily add` or `/daily setup`.", ephemeral=True)
        return
    lines = ["**Daily features** (run on demand with each `/command`):"] + [_feature_summary(f) for f in feats]
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


@daily_group.command(name="remove", description="Delete a custom daily feature (by its command)")
@app_commands.describe(command="The feature's command, e.g. meme")
@app_commands.autocomplete(command=_feature_slug_autocomplete)
@admin_only()
async def daily_remove(interaction: discord.Interaction, command: str):
    feat = get_custom_feature_by_command(interaction.guild_id, command.strip().lower())
    if not feat:
        await interaction.response.send_message(
            f'⚠️ No feature with command `/{command}`. See `/daily list`.', ephemeral=True)
        return
    remove_custom_feature(interaction.guild_id, feat["name"])
    await sync_guild_feature_commands(interaction.guild_id)
    await interaction.response.send_message(
        f"🗑️ **{feat['name']}** (`/{feat['command']}`) removed.", ephemeral=True)


@daily_group.command(name="access", description="Set who can run a feature's command")
@app_commands.describe(command="The feature's command, e.g. meme", access="Who may run it",
                       role="Role (when access is 'role')")
@app_commands.autocomplete(command=_feature_slug_autocomplete)
@admin_only()
async def daily_access(interaction: discord.Interaction, command: str,
                       access: Literal["admin", "everyone", "role"],
                       role: Optional[discord.Role] = None):
    feat = get_custom_feature_by_command(interaction.guild_id, command.strip().lower())
    if not feat:
        await interaction.response.send_message(
            f'⚠️ No feature with command `/{command}`. See `/daily list`.', ephemeral=True)
        return
    run_access, run_roles, aerr = _access_from_choice(access, role)
    if aerr:
        await interaction.response.send_message(f"⚠️ {aerr}", ephemeral=True)
        return
    set_custom_feature_access(interaction.guild_id, feat["name"], run_access, run_roles)
    who = {"admin": "admins only", "everyone": "anyone",
           "roles": f"{role.mention} and admins"}[run_access]
    await interaction.response.send_message(
        f"✅ `/{feat['command']}` can now be run by {who}.", ephemeral=True)


@daily_group.command(name="toggle", description="Pause or resume a custom daily feature")
@app_commands.describe(command="The feature's command, e.g. meme", enabled="Turn it on or off")
@app_commands.autocomplete(command=_feature_slug_autocomplete)
@admin_only()
async def daily_toggle(interaction: discord.Interaction, command: str, enabled: bool):
    feat = get_custom_feature_by_command(interaction.guild_id, command.strip().lower())
    if not feat:
        await interaction.response.send_message(
            f'⚠️ No feature with command `/{command}`. See `/daily list`.', ephemeral=True)
        return
    set_custom_feature_enabled(interaction.guild_id, feat["name"], enabled)
    await interaction.response.send_message(
        f"✅ **{feat['name']}** (`/{feat['command']}`) {'enabled' if enabled else 'paused'}.", ephemeral=True)


client.tree.add_command(daily_group)


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


class _ResetConfirmView(discord.ui.View):
    """Two-step confirmation for the destructive /admin reset."""
    def __init__(self, author_id: int, guild_id: int):
        super().__init__(timeout=60)
        self.author_id = author_id
        self.guild_id  = guild_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "This confirmation isn't yours.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Yes, wipe EVERYTHING", style=discord.ButtonStyle.danger, emoji="⚠️")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        reset_guild(self.guild_id)
        await sync_guild_feature_commands(self.guild_id)  # remove the wiped features' /commands
        for c in self.children:
            c.disabled = True
        await interaction.response.edit_message(
            content=("🗑️ **Done.** Every bit of this server's data was wiped — it's back to a fresh "
                     "install. Run `/setup` to reconfigure."),
            view=self)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        for c in self.children:
            c.disabled = True
        await interaction.response.edit_message(content="✅ Cancelled — nothing was changed.", view=self)
        self.stop()


@admin_group.command(name="reset", description="⚠️ Wipe ALL of this server's bot data (irreversible)")
@manager_only()
async def admin_reset(interaction: discord.Interaction):
    view = _ResetConfirmView(interaction.user.id, interaction.guild_id)
    await interaction.response.send_message(
        "⚠️ **DANGER — full reset**\n"
        "This **permanently deletes everything** this bot stores for this server:\n"
        "• all `/config` settings, schedule & channels\n"
        "• every custom `/daily` feature (and its slash command)\n"
        "• all brackets, seasons & bracket history\n"
        "• all tracked rename posts, forward nominations & pick history\n"
        "• the bot-admin roster\n\n"
        "**This cannot be undone.** The server returns to a fresh state (like a new install).\n"
        "*(Mainly a testing/cleanup tool — most servers never need this.)*",
        view=view, ephemeral=True)


client.tree.add_command(admin_group)


# ── Top-level commands ────────────────────────────────────────────────────────

@client.tree.command(name="help", description="Show the command reference")
@app_commands.guild_only()
async def help_cmd(interaction: discord.Interaction):
    is_admin, is_manager = _perms(interaction)
    await interaction.response.send_message(embed=build_help_embed(is_admin, is_manager), ephemeral=True)


class _CoreSetupView(discord.ui.View):
    """Guided first-run setup: the core channels + timezone, saved as you pick."""
    def __init__(self, author_id: int, guild_id: int):
        super().__init__(timeout=300)
        self.author_id = author_id
        self.guild_id  = guild_id
        cur_tz = get_config(guild_id)["timezone"] or "US/Eastern"
        self.tz_select.options = [
            discord.SelectOption(label=z, value=z, default=(z == cur_tz)) for z in _COMMON_TIMEZONES
        ]

    def _render(self) -> str:
        cfg = get_config(self.guild_id)
        def m(cid): return f"<#{cid}>" if cid else "*not set*"
        return (
            f"{_SETUP_INTRO}\n\n"
            f"• **Quote channel:** {m(cfg['quote_channel'])}\n"
            f"• **Icon channel:** {m(cfg['icon_channel'])}\n"
            f"• **Post channel:** {m(cfg['post_channel'])}  *(turns on bracket tracking)*\n"
            f"• **Timezone:** {cfg['timezone'] or 'US/Eastern'}"
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "This setup panel isn't yours — run `/setup` yourself.", ephemeral=True)
            return False
        return True

    @discord.ui.select(cls=discord.ui.ChannelSelect, channel_types=[discord.ChannelType.text],
                       placeholder="Quote channel — where members post quotes", row=0)
    async def quote_select(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect):
        set_config(self.guild_id, "quote_channel", select.values[0].id)
        await interaction.response.edit_message(content=self._render(), view=self)

    @discord.ui.select(cls=discord.ui.ChannelSelect, channel_types=[discord.ChannelType.text],
                       placeholder="Icon channel — where members post icon images", row=1)
    async def icon_select(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect):
        set_config(self.guild_id, "icon_channel", select.values[0].id)
        await interaction.response.edit_message(content=self._render(), view=self)

    @discord.ui.select(cls=discord.ui.ChannelSelect, channel_types=[discord.ChannelType.text],
                       placeholder="Post channel — where daily rename cards post (tracked for brackets)", row=2)
    async def post_select(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect):
        set_config(self.guild_id, "post_channel", select.values[0].id)
        await interaction.response.edit_message(content=self._render(), view=self)

    @discord.ui.select(placeholder="Timezone",
                       options=[discord.SelectOption(label="US/Eastern", value="US/Eastern")], row=3)
    async def tz_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        set_config(self.guild_id, "timezone", select.values[0])
        await interaction.response.edit_message(content=self._render(), view=self)

    @discord.ui.button(label="Done", style=discord.ButtonStyle.success, emoji="✅", row=4)
    async def done_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        for c in self.children:
            c.disabled = True
        await interaction.response.edit_message(
            content=(self._render() + "\n\n✅ **Saved.** Next: `/bracket config` · `/daily setup` · "
                     "`/config schedule` (time & frequency). Run `/showconfig` to check for warnings, "
                     "or `/help` for everything."),
            view=self)
        self.stop()


@client.tree.command(name="setup", description="Guided setup — core channels & timezone")
@app_commands.guild_only()
@admin_only()
async def setup_cmd(interaction: discord.Interaction):
    view = _CoreSetupView(interaction.user.id, interaction.guild_id)
    await interaction.response.send_message(view._render(), view=view, ephemeral=True)


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
async def contributors_cmd(interaction: discord.Interaction, category: Literal["quote", "icon"]):
    await interaction.response.defer(ephemeral=True)
    result = await build_contributors(interaction.guild_id, client, category)
    await interaction.followup.send(result, ephemeral=True)


async def _preview_autocomplete(interaction: discord.Interaction, current: str):
    """Suggest 'rename' plus this guild's feature command slugs."""
    cur = (current or "").lower()
    opts = ["rename"] + [f["command"] for f in get_custom_features(interaction.guild_id) if f["command"]]
    return [app_commands.Choice(name=o, value=o) for o in opts if cur in o.lower()][:25]


@client.tree.command(name="preview", description="Dry-run the daily rename or a feature, posted here only")
@app_commands.describe(what="'rename', or a feature's command like 'meme'")
@app_commands.autocomplete(what=_preview_autocomplete)
@app_commands.guild_only()
@admin_only()
async def preview_cmd(interaction: discord.Interaction, what: str):
    target = what.strip().lower()
    if target == "rename":
        await interaction.response.defer(ephemeral=True)
        await process_rename(interaction.guild_id, client, override_post_channel=interaction.channel, preview=True)
        await interaction.followup.send("✅ Rename preview posted above.", ephemeral=True)
        return
    feat = get_custom_feature_by_command(interaction.guild_id, target)
    if not feat:
        await interaction.response.send_message(
            f"⚠️ Nothing to preview for `{what}`. Use `rename` or a feature's command (see `/daily list`).",
            ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    ok, detail = await process_custom_daily(interaction.guild_id, client, feat,
                                            override_post_channel=interaction.channel, preview=True)
    await interaction.followup.send("✅ Preview posted above." if ok else f"⚠️ {detail}", ephemeral=True)


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


# (There is no built-in /song — "Song of the Day" is a normal feature with the
#  slug `song`, so /song is its auto-generated per-guild command.)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if not TOKEN:
        log.critical("DISCORD_TOKEN environment variable not set — exiting.")
    else:
        client.run(TOKEN)
