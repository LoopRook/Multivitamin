import asyncio
import logging
import random
import re
from datetime import datetime, timedelta

import aiohttp
import discord
import pytz

from db_utils import (
    get_config, set_config, log_pick, get_user_last_picks,
    get_today_pick_counts, get_recent_pick_items, store_rename_post,
    get_active_bracket, get_custom_features, set_custom_feature_run_date,
    backup_database,
)
from image_utils import generate_card, truncate_to_100_chars
import credits
import moderation

log = logging.getLogger(__name__)

_AIOHTTP_TIMEOUT = aiohttp.ClientTimeout(total=15)

_MUSIC_PATTERN = re.compile(
    r"https?://(?:www\.)?(?:youtube\.com|youtu\.be|soundcloud\.com|spotify\.com)/\S+",
    re.IGNORECASE,
)

_URL_PATTERN = re.compile(r"https?://\S+", re.IGNORECASE)


def is_music_link(line: str) -> bool:
    return bool(_MUSIC_PATTERN.search(line))


def _guild_tz(cfg) -> pytz.BaseTzInfo:
    try:
        return pytz.timezone(cfg["timezone"] or "US/Eastern")
    except pytz.exceptions.UnknownTimeZoneError:
        return pytz.timezone("US/Eastern")


def _normalize_time(t: str) -> str:
    h, m = t.strip().split(":")
    return f"{int(h):02d}:{int(m):02d}"


_FIRST_RUN_GRACE_MIN = 60


def _fire_action(scheduled: str, cur_time: str, last_date: str | None, today: str) -> str:
    """
    Whether a scheduled job should run on this tick: 'fire', 'stamp' or 'skip'.

    The scheduled time is a *threshold*, not an instant. Matching the minute
    exactly (cur_time == scheduled) meant a slow tick, a restart or a paused
    container across that one minute skipped the whole day silently. The
    last-run date is what prevents a double fire, so firing late is safe.

    First run (no last date) is special-cased both ways: reaching the slot on
    time (within the grace window) fires normally — a weekly cadence must not
    swallow its very first slot — but a guild configured hours *after* today's
    slot gets 'stamp' instead of a surprise immediate post: record the date and
    start fresh at the next scheduled time.

    Times are zero-padded HH:MM, so string comparison is chronological.
    """
    if last_date == today:
        return "skip"           # already ran today
    if cur_time < scheduled:
        return "skip"           # not time yet
    if last_date:
        return "fire"
    sh, sm = scheduled.split(":")
    ch, cm = cur_time.split(":")
    minutes_late = (int(ch) * 60 + int(cm)) - (int(sh) * 60 + int(sm))
    return "fire" if minutes_late <= _FIRST_RUN_GRACE_MIN else "stamp"


# Discord allows ~2 guild-name edits per 10 minutes. discord.py doesn't error
# past that — it silently sleeps until the limit clears, which would hang an
# on-demand /rename for up to 10 minutes. We track our own budget so the
# command can refuse up front with a real wait time instead.
_RENAME_LIMIT = 2
_RENAME_WINDOW = timedelta(minutes=10)

# A quote/icon that was picked in the last N days is skipped during sampling, so
# contributors with only a few submissions don't repeat. Falls back gracefully
# when a server is so small that everything is recent.
_NO_REPEAT_DAYS = 30

# UTC date of the last on-volume DB backup (reset on restart; the scheduler
# re-backs-up at most once per UTC day).
_last_backup_date: str | None = None
_rename_times: dict[int, list[datetime]] = {}


def _record_guild_rename(guild_id: int) -> None:
    now = datetime.now(pytz.utc)
    kept = [t for t in _rename_times.get(guild_id, []) if now - t < _RENAME_WINDOW]
    kept.append(now)
    _rename_times[guild_id] = kept


def rename_cooldown_remaining(guild_id: int) -> int:
    """Minutes until another server rename is allowed (0 = allowed now)."""
    now = datetime.now(pytz.utc)
    kept = [t for t in _rename_times.get(guild_id, []) if now - t < _RENAME_WINDOW]
    _rename_times[guild_id] = kept
    if len(kept) < _RENAME_LIMIT:
        return 0
    free_at = min(kept) + _RENAME_WINDOW
    seconds = (free_at - now).total_seconds()
    return max(1, int(seconds // 60) + (1 if seconds % 60 else 0))


def pack_lines(lines: list[str], limit: int = 1900) -> str:
    """
    Join *lines* into a single message that stays under Discord's 2000-char
    cap, dropping overflow lines with a "+N more" tail. For single-response
    replies (ephemeral interactions) where splitting into several messages
    isn't worth the ceremony — unbounded lists must not 400 the whole reply.
    """
    out, used, dropped = [], 0, 0
    for i, line in enumerate(lines):
        if used + len(line) + 1 > limit:
            dropped = len(lines) - i
            break
        out.append(line)
        used += len(line) + 1
    if dropped:
        out.append(f"*…and {dropped} more*")
    return "\n".join(out)


def _today_since_utc(tz: pytz.BaseTzInfo) -> str:
    """
    Return the UTC ISO timestamp for midnight-today in *tz*.
    Used to scope 'today's picks' correctly per guild timezone.
    """
    now_local  = datetime.now(tz)
    midnight   = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.astimezone(pytz.utc).isoformat()


def _weighted_choice(pool: dict[int, tuple], cooldown_counts: dict[int, int]) -> int:
    """
    Pick one user_id from *pool* using same-day cooldown weights.

    Weight formula: 1 / (1 + picks_today)
      0 picks today → weight 1.0  (full odds)
      1 pick today  → weight 0.5  (half odds)
      2 picks today → weight 0.33
      ...never zero, resets at midnight in guild timezone.

    Without cooldown (empty dict) all weights are 1.0, equivalent to random.choice.
    """
    uids    = list(pool.keys())
    weights = [1 / (1 + cooldown_counts.get(uid, 0)) for uid in uids]

    # Log any users whose weight was reduced
    reduced = {
        pool[uid][1]: f"{1/(1+cooldown_counts[uid]):.2f} ({cooldown_counts[uid]} picks today)"
        for uid in uids if cooldown_counts.get(uid, 0) > 0
    }
    if reduced:
        log.info("cooldown weights applied: %s", reduced)

    return random.choices(uids, weights=weights, k=1)[0]


# ── History helpers (two-stage fair sampling + optional cooldown) ────────────
#
# Stage 1 — per-user reservoir (Algorithm R, k=1): every user ends up with
#            one randomly chosen item from their own submissions.
# Stage 2 — weighted user selection:
#            weight = 1 / (1 + picks_today_for_category)
#            If cooldown is disabled, all weights are 1.0 (uniform).
#
# Returns (item, display_name, user_id).

def _reservoir_add(pool: dict, uid: int, name: str, item) -> None:
    """Algorithm R (k=1): after N items, each had a 1/N chance to be the kept one."""
    cur, _, count = pool.get(uid, (None, name, 0))
    count += 1
    pool[uid] = (item if random.randint(1, count) == 1 else cur, name, count)


def _strip_query(url: str) -> str:
    """Discord CDN URLs carry rotating signature params; the base path is the identity."""
    return url.split("?", 1)[0]


async def get_random_quote(
    channel,
    cooldown_counts: dict[int, int] | None = None,
    is_blocked=None,
    recent_items: set | None = None,
) -> tuple[str | None, str | None, int | None]:
    if channel is None:
        return None, None, None
    # Two reservoirs: `fresh` excludes items picked inside the no-repeat window,
    # `full` is everything. A contributor whose whole (small) pool was used
    # recently simply sits out until the window expires; if that would leave
    # nobody, fall back to `full` so tiny servers still get a rename.
    fresh: dict[int, tuple[str, str, int]] = {}
    full:  dict[int, tuple[str, str, int]] = {}
    scanned = 0
    async for msg in channel.history(limit=None, oldest_first=False):
        scanned += 1
        if msg.author.bot:
            continue
        uid = msg.author.id
        for line in msg.content.strip().splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("!"):
                continue
            if is_blocked and is_blocked(stripped):   # skip slurs/hate terms
                continue
            _reservoir_add(full, uid, msg.author.display_name, stripped)
            if not (recent_items and stripped in recent_items):
                _reservoir_add(fresh, uid, msg.author.display_name, stripped)
    pool = fresh or full
    if not pool:
        log.info("[quote] scanned %d messages → 0 contributors", scanned)
        return None, None, None
    chosen_uid = _weighted_choice(pool, cooldown_counts or {})
    item, name, count = pool[chosen_uid]
    log.info(
        "[quote] scanned %d messages → %d contributors (%d with fresh material) → picked %r from %s (%d submissions)",
        scanned, len(full), len(fresh), item, name, count,
    )
    return item, name, chosen_uid


async def get_random_icon(
    channel,
    cooldown_counts: dict[int, int] | None = None,
    recent_items: set | None = None,
) -> tuple[str | None, str | None, int | None]:
    if channel is None:
        return None, None, None
    fresh: dict[int, tuple[str, str, int]] = {}
    full:  dict[int, tuple[str, str, int]] = {}
    scanned = 0
    async for msg in channel.history(limit=None, oldest_first=False):
        scanned += 1
        if msg.author.bot:
            continue
        uid = msg.author.id
        for att in msg.attachments:
            if not (att.content_type and att.content_type.startswith("image")):
                continue
            _reservoir_add(full, uid, msg.author.display_name, att.url)
            if not (recent_items and _strip_query(att.url) in recent_items):
                _reservoir_add(fresh, uid, msg.author.display_name, att.url)
    pool = fresh or full
    if not pool:
        log.info("[icon] scanned %d messages → 0 contributors", scanned)
        return None, None, None
    chosen_uid = _weighted_choice(pool, cooldown_counts or {})
    url, name, count = pool[chosen_uid]
    log.info(
        "[icon] scanned %d messages → %d contributors (%d with fresh material) → picked image from %s (%d submissions)",
        scanned, len(full), len(fresh), name, count,
    )
    return url, name, chosen_uid


def _extract_candidate(msg, content_type: str) -> dict | None:
    """
    First qualifying candidate in a message for a custom-feature content type,
    or None. Returns a dict describing what to repost:
      {"kind": "attachment", "url", "filename", "size"}  (media uploads)
      {"kind": "text", "content"}                        (media links / link / music / text)
    Pure and side-effect free so it can be unit-tested on fake messages.
    """
    if content_type == "media":
        # Prefer an uploaded file (image/gif/video); fall back to a media link.
        for att in msg.attachments:
            return {"kind": "attachment", "url": att.url,
                    "filename": att.filename or "daily", "size": att.size or 0}
        for line in msg.content.splitlines():
            s = line.strip()
            if _URL_PATTERN.search(s):
                return {"kind": "text", "content": s}
        return None
    if content_type == "link":
        for line in msg.content.splitlines():
            s = line.strip()
            if _URL_PATTERN.search(s):
                return {"kind": "text", "content": s}
        return None
    if content_type == "music":
        for line in msg.content.splitlines():
            s = line.strip()
            if s and is_music_link(s):
                return {"kind": "text", "content": s}
        return None
    # text
    for line in msg.content.splitlines():
        s = line.strip()
        if s and not s.startswith("!"):
            return {"kind": "text", "content": s}
    return None


async def get_random_content(
    channel,
    content_type: str,
    cooldown_counts: dict[int, int] | None = None,
    recent_items: set | None = None,
) -> tuple[dict | None, str | None, int | None]:
    """
    Fair pick (one candidate per contributor, cooldown-weighted) for a custom
    daily feature. Returns (candidate_dict, display_name, user_id) — see
    _extract_candidate for the candidate shape. Scans all-time history.

    *recent_items* is the raw logged-item set for the no-repeat window.
    Attachment URLs are compared signature-stripped (CDN signatures rotate);
    content items (links/music/text) are compared verbatim, since a link's
    query string IS its identity (youtube.com/watch?v=...).
    """
    if channel is None:
        return None, None, None
    recent_raw = recent_items or set()
    recent_stripped = {_strip_query(i) for i in recent_raw}

    def _is_recent(cand: dict) -> bool:
        if cand["kind"] == "attachment":
            return _strip_query(cand["url"]) in recent_stripped
        return cand["content"] in recent_raw

    fresh: dict[int, tuple[dict, str, int]] = {}
    full:  dict[int, tuple[dict, str, int]] = {}
    scanned = 0
    async for msg in channel.history(limit=None, oldest_first=False):
        scanned += 1
        if msg.author.bot:
            continue
        cand = _extract_candidate(msg, content_type)
        if cand is None:
            continue
        uid = msg.author.id
        _reservoir_add(full, uid, msg.author.display_name, cand)
        if not _is_recent(cand):
            _reservoir_add(fresh, uid, msg.author.display_name, cand)
    pool = fresh or full
    if not pool:
        log.info("[custom:%s] scanned %d messages → 0 contributors", content_type, scanned)
        return None, None, None
    chosen_uid = _weighted_choice(pool, cooldown_counts or {})
    cand, name, count = pool[chosen_uid]
    log.info(
        "[custom:%s] scanned %d messages → %d contributors (%d with fresh material) → picked from %s (%d submissions)",
        content_type, scanned, len(full), len(fresh), name, count,
    )
    return cand, name, chosen_uid




async def get_contributor_quotes(channel) -> dict[int, tuple[str, str]]:
    """
    Scan *channel* and return {user_id: (quote, display_name)}.
    One randomly selected quote per contributor, using the same per-user
    reservoir sampling as get_random_quote.  Used by !testbracket.
    """
    if channel is None:
        return {}
    pool: dict[int, tuple[str, str, int]] = {}
    async for msg in channel.history(limit=None, oldest_first=False):
        if msg.author.bot:
            continue
        uid = msg.author.id
        for line in msg.content.strip().splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("!"):
                continue
            cur, name, count = pool.get(uid, (None, msg.author.display_name, 0))
            count += 1
            if random.randint(1, count) == 1:
                pool[uid] = (stripped, msg.author.display_name, count)
            else:
                pool[uid] = (cur, name, count)
    return {uid: (quote, name) for uid, (quote, name, _) in pool.items()}

# ── Contribution scanner (used by !mystats and !contributors) ────────────────

async def scan_contributions(channel, category: str) -> dict[int, tuple[str, int]]:
    if channel is None:
        return {}
    counts: dict[int, tuple[str, int]] = {}
    async for msg in channel.history(limit=None, oldest_first=False):
        if msg.author.bot:
            continue
        uid  = msg.author.id
        name = msg.author.display_name
        n = 0
        if category == "quote":
            for line in msg.content.strip().splitlines():
                s = line.strip()
                if s and not s.startswith("!"):
                    n += 1
        elif category == "icon":
            for att in msg.attachments:
                if att.content_type and att.content_type.startswith("image"):
                    n += 1
        elif category == "song":
            for line in msg.content.strip().splitlines():
                s = line.strip()
                if s and is_music_link(s):
                    n += 1
        if n:
            _, prev = counts.get(uid, (name, 0))
            counts[uid] = (name, prev + n)
    return counts


async def build_mystats(guild_id: int, client: discord.Client, user_id: int, display_name: str) -> str:
    cfg = get_config(guild_id)
    quote_ch = client.get_channel(cfg["quote_channel"])
    icon_ch  = client.get_channel(cfg["icon_channel"])
    music_ch = client.get_channel(cfg["music_channel"])

    q_counts, i_counts, s_counts = await asyncio.gather(
        scan_contributions(quote_ch, "quote"),
        scan_contributions(icon_ch,  "icon"),
        scan_contributions(music_ch, "song"),
    )

    q_count = q_counts.get(user_id, (None, 0))[1]
    i_count = i_counts.get(user_id, (None, 0))[1]
    s_count = s_counts.get(user_id, (None, 0))[1]

    last = get_user_last_picks(guild_id, user_id)

    def fmt(iso: str | None) -> str:
        return iso[:10] if iso else "Never"

    return (
        f"📊 **Stats for {display_name}**\n"
        f"Quotes submitted: **{q_count}** │ Last picked: {fmt(last.get('quote'))}\n"
        f"Images submitted: **{i_count}** │ Last picked: {fmt(last.get('icon'))}\n"
        f"Songs submitted:  **{s_count}** │ Last picked: {fmt(last.get('song'))}\n"
    )


async def build_contributors(guild_id: int, client: discord.Client, category: str) -> str:
    cfg = get_config(guild_id)
    ch_map = {
        "quote": client.get_channel(cfg["quote_channel"]),
        "icon":  client.get_channel(cfg["icon_channel"]),
        "song":  client.get_channel(cfg["music_channel"]),
    }
    channel = ch_map.get(category)
    if not channel:
        return f"⚠️ {category.title()} channel is not configured."

    counts = await scan_contributions(channel, category)
    if not counts:
        return f"No {category} contributions found."

    sorted_entries = sorted(counts.values(), key=lambda x: x[1], reverse=True)
    label = {"quote": "Quote", "icon": "Icon", "song": "Song"}[category]
    lines = [f"📊 **{label} contributors**"]
    for name, count in sorted_entries:
        lines.append(f"  {name}: **{count}** submission{'s' if count != 1 else ''}")
    return pack_lines(lines)


_WEEKDAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _cadence_desc(c) -> str:
    """Human description of the rename cadence for /showconfig."""
    wd = (c.get("quote_weekdays") or "").strip()
    if wd:
        days = [_WEEKDAY_NAMES[int(x)] for x in wd.split(",") if x.strip().isdigit() and 0 <= int(x) <= 6]
        return ("Weekly on " + ", ".join(days)) if days else "Daily"
    n = c.get("quote_interval_days") or 1
    return "Daily" if n <= 1 else f"Every {n} days"


def _blocklist_desc(c) -> str:
    """Human description of the slur blocklist for /showconfig."""
    if not (c["blocklist_enabled"] if "blocklist_enabled" in c.keys() else 1):
        return "Off"
    extra = len(moderation.parse_custom(c["blocklist_custom"] if "blocklist_custom" in c.keys() else None))
    return "On" + (f" (+{extra} custom)" if extra else "")


def rename_blocklist(cfg):
    """Return a per-guild is_blocked(text) callable, or None when the blocklist is off."""
    if not (cfg["blocklist_enabled"] if "blocklist_enabled" in cfg.keys() else 1):
        return None
    extra = moderation.parse_custom(cfg["blocklist_custom"] if "blocklist_custom" in cfg.keys() else None)
    return lambda text: moderation.is_blocked(text, extra)


async def build_config(guild_id: int, client: discord.Client) -> str:
    """
    Return a formatted config string with channel IDs resolved to #channel-name.
    Falls back to the raw ID if the channel can't be found (e.g. deleted channel).
    """
    from db_utils import show_config, get_seasons
    c = show_config(guild_id)

    def ch(cid) -> str:
        if not cid:
            return "Not Set"
        channel = client.get_channel(cid)
        return f"#{channel.name}" if channel else f"{cid} (not found)"

    # Bracket tracking is always on once a post channel is set.
    if c["post_channel"]:
        since = c["voting_enabled_at"]
        tracking = f"On (since {since[:10]})" if since else "On (awaiting first rename)"
    else:
        tracking = "Off (set a Post Channel to enable)"

    season_count = len(get_seasons(guild_id))

    lines = [
        f"Guild ID:            {c['guild_id']}",
        f"Quote Channel:       {ch(c['quote_channel'])}",
        f"Icon Channel:        {ch(c['icon_channel'])}",
        f"Post Channel:        {ch(c['post_channel'])}",
        f"Music Channel:       {ch(c['music_channel'])}",
        f"Song Post Channel:   {ch(c['song_post_channel'])}",
        f"Bracket Channel:     {ch(c['bracket_channel'])}",
        f"Bracket Source:      {ch(c.get('bracket_source_channel')) if c.get('bracket_source_channel') else 'All tracked renames'}",
        f"Quote Feature:       {'Enabled' if c['enable_daily_quote'] else 'Disabled'}",
        f"Song Feature:        {'Enabled' if c['enable_daily_song']  else 'Disabled'}",
        f"Cooldown:            {'Enabled' if c['enable_cooldown']    else 'Disabled'}",
        f"On-Demand /rename:   {'Everyone' if c.get('rename_open', 1) else 'Admins only'}",
        f"Bracket Tracking:    {tracking}",
        f"Bracket Size:        {c['bracket_size'] or 8}",
        f"Bracket Vote Hours:  {c['bracket_voting_hours'] or 24}",
        f"Bracket Pacing:      {c.get('bracket_pacing') or 'round'}",
        f"Seasons Defined:     {season_count}",
        f"Timezone:            {c['timezone'] or 'US/Eastern'}",
        f"Quote Time:          {c['quote_time'] or '4:00'}",
        f"Rename Cadence:      {_cadence_desc(c)}",
        f"Song Time:           {c['song_time'] or '10:00'}",
        f"Credit Names:        {'Server nickname' if credits.style_of(c) == 'nickname' else '@username'}",
        f"Credit Tagging:      {'On (mentions)' if credits.mentions_on(c) else 'Off (plain text)'}",
        f"Slur Blocklist:      {_blocklist_desc(c)}",
    ]

    # Health check — surface setup gaps and missing permissions.
    warnings = []
    if not c["post_channel"]:
        warnings.append("No Post Channel - renames aren't tracked for brackets (set one in /setup).")
    guild = client.get_guild(guild_id)
    if guild is not None and guild.me is not None:
        p = guild.me.guild_permissions
        if not p.manage_guild:
            warnings.append("Missing 'Manage Server' - daily and bracket renames will fail.")
        missing = [n for n, ok in (
            ("Attach Files", p.attach_files), ("Embed Links", p.embed_links),
            ("Add Reactions", p.add_reactions), ("Read Message History", p.read_message_history),
        ) if not ok]
        if missing:
            warnings.append("Missing perms: " + ", ".join(missing) + " - cards/polls may fail.")

    lines.append("")
    if warnings:
        lines.append("Warnings:")
        lines.extend(f"  ! {w}" for w in warnings)
    else:
        lines.append("Health:              OK - no configuration warnings.")
    return "\n".join(lines)


# ── Core feature logic ───────────────────────────────────────────────────────

async def process_rename(
    guild_id: int,
    client: discord.Client,
    override_post_channel=None,
    preview: bool = False,
) -> None:
    cfg   = get_config(guild_id)
    guild = client.get_guild(guild_id)
    if not guild:
        log.warning("[%s] Guild not found — skipping rename.", guild_id)
        return

    # While a bracket is running, the daily quote-rename is suspended — the
    # bracket drives the server name instead (each matchup winner under daily
    # pacing; the champion under round pacing). Renames resume once the bracket
    # completes. Previews are exempt.
    if not preview and get_active_bracket(guild_id):
        log.info("[%s] Active bracket — daily rename suspended (bracket controls the name).", guild_id)
        return

    # Fetch today's cooldown counts per category if cooldown is enabled.
    # preview runs bypass cooldown so admins see unweighted results.
    if cfg["enable_cooldown"] and not preview:
        tz       = _guild_tz(cfg)
        since    = _today_since_utc(tz)
        q_cd     = get_today_pick_counts(guild_id, "quote", since)
        i_cd     = get_today_pick_counts(guild_id, "icon",  since)
    else:
        q_cd = i_cd = {}

    quote_channel = client.get_channel(cfg["quote_channel"])
    icon_channel  = client.get_channel(cfg["icon_channel"])
    post_channel  = client.get_channel(cfg["post_channel"])

    # No-repeat window: exclude items already used recently, so contributors with
    # only a few submissions don't have the same quote/icon headline over and over.
    nr_since = (datetime.now(pytz.utc) - timedelta(days=_NO_REPEAT_DAYS)).isoformat()
    recent_q = get_recent_pick_items(guild_id, "quote", nr_since)
    recent_i = {_strip_query(u) for u in get_recent_pick_items(guild_id, "icon", nr_since)}

    (quote, quote_user, quote_uid), (image_url, icon_user, icon_uid) = await asyncio.gather(
        get_random_quote(quote_channel, cooldown_counts=q_cd, is_blocked=rename_blocklist(cfg),
                         recent_items=recent_q),
        get_random_icon( icon_channel,  cooldown_counts=i_cd, recent_items=recent_i),
    )

    if not quote or not image_url:
        log.warning("[%s] No valid quote or image found — skipping rename.", guild_id)
        return

    try:
        async with aiohttp.ClientSession(timeout=_AIOHTTP_TIMEOUT) as session:
            async with session.get(image_url) as resp:
                resp.raise_for_status()
                icon_bytes = await resp.read()
    except Exception as e:
        log.error("[%s] Failed to download icon image: %s", guild_id, e)
        return

    if not preview:
        renamed = False
        try:
            await guild.edit(name=truncate_to_100_chars(quote), icon=icon_bytes)
            renamed = True
            _record_guild_rename(guild_id)
            log.info('[%s] Server renamed to: "%s"', guild_id, quote)
        except discord.HTTPException as e:
            log.error("[%s] Failed to rename guild: %s", guild_id, e)
        # Only count picks for a rename that actually happened — a failed edit
        # must not burn the contributors' cooldown weighting or pad their stats.
        if renamed and quote_uid:
            log_pick(guild_id, quote_uid, quote_user or "Unknown", "quote", quote)
        if renamed and icon_uid:
            log_pick(guild_id, icon_uid, icon_user or "Unknown", "icon", image_url)

    # Names on the card follow the guild's credit style, resolved live from the
    # member (a stored snapshot goes stale when someone changes their nickname).
    # Cards are images, so they can never carry a mention.
    style = credits.style_of(cfg)
    quote_name = credits.resolve_name(client, guild_id, quote_uid, quote_user, style)
    icon_name  = credits.resolve_name(client, guild_id, icon_uid,  icon_user,  style)

    image_file = await generate_card(quote, quote_name, icon_name, icon_bytes)
    if not image_file:
        return

    if preview:
        if override_post_channel:
            image_file.seek(0)
            try:
                await override_post_channel.send(
                    f"🔍 **Preview.** Quote by {quote_name}, icon by {icon_name}:\n> {quote}",
                    file=discord.File(fp=image_file, filename="preview.png"),
                )
            except discord.HTTPException as e:
                log.error("[%s] Failed to send preview: %s", guild_id, e)
        return

    channels_to_post = []
    if override_post_channel:
        channels_to_post.append(override_post_channel)
    if post_channel and (
        not override_post_channel or post_channel.id != override_post_channel.id
    ):
        channels_to_post.append(post_channel)

    post_chan_id = cfg["post_channel"]

    for channel in channels_to_post:
        image_file.seek(0)
        try:
            sent = await channel.send(file=discord.File(fp=image_file, filename="update.png"))
        except discord.HTTPException as e:
            log.error("[%s] Failed to post card to channel %s: %s", guild_id, channel.id, e)
            continue

        # Bracket tracking: record EVERY copy of the card we post so its reactions
        # can seed a bracket later — and so a native Forward of any copy (e.g. the
        # channel a user ran /rename in, not just the post channel) can be matched
        # back to this rename via message_id. Tracking is on once a post channel is
        # configured; dedup-by-quote at bracket time keeps duplicate copies from
        # double-counting. voting_enabled_at is stamped once as a "tracking since"
        # floor so brackets never count pre-tracking history.
        if post_chan_id:
            # Grab the attachment URL as a cached snapshot.
            # (We always re-fetch fresh at bracket time since CDN URLs expire.)
            img_url = sent.attachments[0].url if sent.attachments else None
            store_rename_post(guild_id, sent.id, sent.channel.id, quote, quote_user, quote_uid,
                              img_url, icon_user=icon_user, icon_uid=icon_uid)
            if not cfg["voting_enabled_at"]:
                set_config(guild_id, "voting_enabled_at", datetime.now(pytz.utc).isoformat())


# In-progress guard, keyed by (guild_id, feature_id), so a slow scan can't
# overlap with itself. (Song-of-the-day is now just a custom feature.)
_custom_running: set[tuple[int, int]] = set()


def _feature_fail(guild_id: int, name: str, msg: str) -> tuple[bool, str]:
    log.warning("[%s] Custom feature '%s': %s", guild_id, name, msg)
    return False, msg


async def process_custom_daily(
    guild_id: int,
    client: discord.Client,
    feature,
    override_post_channel=None,
    preview: bool = False,
    on_demand: bool = False,
) -> tuple[bool, str]:
    """
    Run one admin-defined "X of the day" feature: fairly pick a matching item
    from its source channel and repost it to its post channel. Generalizes the
    old song-of-the-day flow. *feature* is a custom_features Row.

    Modes:
      scheduled (default) — post to the feature's channel, log the pick.
      preview=True        — post to override channel with a "🔍 Preview" tag, no log.
      on_demand=True      — post to override channel (no tag), no log; for /<command>.

    Returns (ok, detail): ok=True on a successful post; otherwise detail is a
    short, user-facing reason (permissions, no content, etc.) so callers can
    show the truth instead of a blanket "posted" message.
    """
    name    = feature["name"]
    key     = (guild_id, feature["id"])
    counts  = not preview and not on_demand   # only a scheduled/real run logs + guards
    if counts and key in _custom_running:
        return False, f"**{name}** is already running. Try again in a moment."
    if counts:
        _custom_running.add(key)
    try:
        cfg      = get_config(guild_id)
        ctype    = feature["content_type"]
        category = f"custom:{name.lower()}"

        if cfg["enable_cooldown"] and counts:
            tz    = _guild_tz(cfg)
            since = _today_since_utc(tz)
            cd    = get_today_pick_counts(guild_id, category, since)
        else:
            cd = {}

        source       = client.get_channel(feature["source_channel"])
        post_channel = override_post_channel or client.get_channel(feature["post_channel"])
        if not source:
            return _feature_fail(guild_id, name,
                f"I can't see the **source** channel for **{name}**. Was it deleted, "
                f"or am I missing **View Channel** there?")
        if not post_channel:
            return _feature_fail(guild_id, name,
                f"I can't see the **destination** channel for **{name}**. Was it deleted, "
                f"or am I missing **View Channel** there?")

        # Scan the source channel (needs View Channel + Read Message History).
        nr_since = (datetime.now(pytz.utc) - timedelta(days=_NO_REPEAT_DAYS)).isoformat()
        recent = get_recent_pick_items(guild_id, category, nr_since)
        try:
            cand, user, uid = await get_random_content(source, ctype, cooldown_counts=cd,
                                                       recent_items=recent)
        except discord.Forbidden:
            return _feature_fail(guild_id, name,
                f"I can't read {source.mention}. Give me **View Channel** and "
                f"**Read Message History** there.")
        if not cand:
            return _feature_fail(guild_id, name,
                f"No eligible **{ctype}** content found in {source.mention} yet.")

        prefix  = "🔍 **Preview:** " if preview else ""
        emoji   = (feature["emoji"] + " ") if feature["emoji"] else ""
        caption = f"{prefix}{emoji}**{name}** (from {user}):"

        try:
            if cand["kind"] == "attachment":
                logged_item = cand["url"]
                guild = client.get_guild(guild_id)
                limit = getattr(guild, "filesize_limit", 8 * 1024 * 1024) if guild else 8 * 1024 * 1024
                posted = False
                if cand["size"] and cand["size"] <= limit:
                    # Reupload the file so it embeds reliably and doesn't rely on the
                    # source message's (expiring) CDN URL. On any non-permission error,
                    # fall back to posting the URL (e.g. missing only Attach Files).
                    try:
                        async with aiohttp.ClientSession(timeout=_AIOHTTP_TIMEOUT) as session:
                            async with session.get(cand["url"]) as resp:
                                resp.raise_for_status()
                                data = await resp.read()
                        from io import BytesIO
                        await post_channel.send(content=caption, file=discord.File(BytesIO(data), filename=cand["filename"]))
                        posted = True
                    except discord.Forbidden:
                        raise
                    except Exception as e:
                        log.warning("[%s] Custom feature '%s': reupload failed (%s) — posting URL.", guild_id, name, e)
                if not posted:
                    await post_channel.send(f"{caption}\n{cand['url']}")
            else:
                logged_item = cand["content"]
                await post_channel.send(f"{caption}\n{cand['content']}")
        except discord.Forbidden:
            extra = " and **Attach Files**" if ctype == "media" else ""
            return _feature_fail(guild_id, name,
                f"I can't post in {post_channel.mention}. Give me **View Channel**, "
                f"**Send Messages**{extra} there.")

        if counts and uid:
            log_pick(guild_id, uid, user or "Unknown", category, logged_item)
        mode = " (preview)" if preview else (" (on-demand)" if on_demand else "")
        log.info("[%s] Posted custom feature '%s'%s.", guild_id, name, mode)
        return True, ""
    except Exception as e:
        log.error("[%s] Custom feature '%s' failed: %s", guild_id, name, e)
        return False, "Something went wrong running that feature. Check the bot logs."
    finally:
        if counts:
            _custom_running.discard(key)


# ── Scheduling (per-guild times and timezones) ───────────────────────────────

def _cadence_due(weekdays_csv, interval_days, last_date_str, now) -> bool:
    """
    Shared cadence gate (used by both the server rename and custom features). The
    caller already matched the time-of-day and the once-per-day guard.
      • Weekday mode — *weekdays_csv* is a comma list of weekday numbers
        (0=Mon .. 6=Sun). Due when today's weekday is in the set (e.g. "6" = every
        Sunday). Overrides the interval.
      • Interval mode (default) — due when at least *interval_days* have passed
        since *last_date_str* (1 = daily).
    """
    weekdays = (weekdays_csv or "").strip()
    if weekdays:
        allowed = {int(x) for x in weekdays.split(",") if x.strip().isdigit()}
        return now.weekday() in allowed
    interval = interval_days or 1
    if interval <= 1:
        return True
    if not last_date_str:
        return True
    try:
        last_date = datetime.strptime(last_date_str, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return True
    return (now.date() - last_date).days >= interval


def _rename_due(cfg, now) -> bool:
    """Server-rename cadence (weekday mode via quote_weekdays, else quote_interval_days)."""
    return _cadence_due(cfg["quote_weekdays"], cfg["quote_interval_days"], cfg["last_quote_date"], now)


def _feature_due(feat, now) -> bool:
    """Custom-feature cadence (weekday mode via weekdays, else interval_days)."""
    return _cadence_due(feat["weekdays"], feat["interval_days"], feat["last_run_date"], now)


async def scheduler_loop(client: discord.Client) -> None:
    await client.wait_until_ready()
    log.info("⏰ Scheduler loop started (checking every 60 s)")
    from bracket import check_bracket_advancement
    while not client.is_closed():
        await asyncio.sleep(60)
        now_utc = datetime.now(pytz.utc)

        # Once-a-day on-volume DB backup (guarded so a restart re-backs-up at most
        # once per UTC day). Global, not per-guild.
        global _last_backup_date
        today_utc = now_utc.strftime("%Y-%m-%d")
        if _last_backup_date != today_utc:
            _last_backup_date = today_utc
            try:
                backup_database()
            except Exception:
                log.exception("Daily DB backup failed — continuing.")

        for guild in client.guilds:
            # Isolate each guild: one guild's failure must not abort the cycle
            # for every other guild (critical for a multi-tenant public bot).
            try:
                cfg  = get_config(guild.id)
                tz   = _guild_tz(cfg)
                now  = now_utc.astimezone(tz)
                today    = now.strftime("%Y-%m-%d")
                cur_time = now.strftime("%H:%M")

                if cfg["enable_daily_quote"]:
                    scheduled = _normalize_time(cfg["quote_time"] or "04:00")
                    action = _fire_action(scheduled, cur_time, cfg["last_quote_date"], today)
                    if action != "skip" and _rename_due(cfg, now):
                        # Stamp before running: a job that keeps throwing must not
                        # retry every 60s for the rest of the day.
                        set_config(guild.id, "last_quote_date", today)
                        if action == "fire":
                            await process_rename(guild.id, client)

                # Admin-defined "X of the day" features (song-of-the-day is one).
                for feat in get_custom_features(guild.id):
                    if not feat["enabled"]:
                        continue
                    try:
                        scheduled = _normalize_time(feat["post_time"])
                    except Exception:
                        continue
                    action = _fire_action(scheduled, cur_time, feat["last_run_date"], today)
                    if action != "skip" and _feature_due(feat, now):
                        set_custom_feature_run_date(feat["id"], today)
                        if action == "fire":
                            await process_custom_daily(guild.id, client, feat)

                # Check for bracket matchups that need tallying/advancing
                await check_bracket_advancement(guild.id, client)
            except Exception:
                log.exception("[%s] Scheduler tick failed for guild — continuing.", guild.id)
