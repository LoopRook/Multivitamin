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
    get_today_pick_counts, store_rename_post, get_active_bracket,
    get_custom_features, set_custom_feature_run_date,
)
from image_utils import generate_card, truncate_to_100_chars

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

async def get_random_quote(
    channel,
    cooldown_counts: dict[int, int] | None = None,
) -> tuple[str | None, str | None, int | None]:
    if channel is None:
        return None, None, None
    pool: dict[int, tuple[str, str, int]] = {}
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
            cur, name, count = pool.get(uid, (None, msg.author.display_name, 0))
            count += 1
            if random.randint(1, count) == 1:
                pool[uid] = (stripped, msg.author.display_name, count)
            else:
                pool[uid] = (cur, name, count)
    if not pool:
        log.info("[quote] scanned %d messages → 0 contributors", scanned)
        return None, None, None
    chosen_uid = _weighted_choice(pool, cooldown_counts or {})
    item, name, count = pool[chosen_uid]
    log.info(
        "[quote] scanned %d messages → %d contributors → picked %r from %s (%d submissions)",
        scanned, len(pool), item, name, count,
    )
    return item, name, chosen_uid


async def get_random_icon(
    channel,
    cooldown_counts: dict[int, int] | None = None,
) -> tuple[str | None, str | None, int | None]:
    if channel is None:
        return None, None, None
    pool: dict[int, tuple[str, str, int]] = {}
    scanned = 0
    async for msg in channel.history(limit=None, oldest_first=False):
        scanned += 1
        if msg.author.bot:
            continue
        uid = msg.author.id
        for att in msg.attachments:
            if not (att.content_type and att.content_type.startswith("image")):
                continue
            cur, name, count = pool.get(uid, (None, msg.author.display_name, 0))
            count += 1
            if random.randint(1, count) == 1:
                pool[uid] = (att.url, msg.author.display_name, count)
            else:
                pool[uid] = (cur, name, count)
    if not pool:
        log.info("[icon] scanned %d messages → 0 contributors", scanned)
        return None, None, None
    chosen_uid = _weighted_choice(pool, cooldown_counts or {})
    url, name, count = pool[chosen_uid]
    log.info(
        "[icon] scanned %d messages → %d contributors → picked image from %s (%d submissions)",
        scanned, len(pool), name, count,
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
) -> tuple[dict | None, str | None, int | None]:
    """
    Fair pick (one candidate per contributor, cooldown-weighted) for a custom
    daily feature. Returns (candidate_dict, display_name, user_id) — see
    _extract_candidate for the candidate shape. Scans all-time history.
    """
    if channel is None:
        return None, None, None
    pool: dict[int, tuple[dict, str, int]] = {}
    scanned = 0
    async for msg in channel.history(limit=None, oldest_first=False):
        scanned += 1
        if msg.author.bot:
            continue
        cand = _extract_candidate(msg, content_type)
        if cand is None:
            continue
        uid = msg.author.id
        cur, name, count = pool.get(uid, (None, msg.author.display_name, 0))
        count += 1
        if random.randint(1, count) == 1:
            pool[uid] = (cand, msg.author.display_name, count)
        else:
            pool[uid] = (cur, name, count)
    if not pool:
        log.info("[custom:%s] scanned %d messages → 0 contributors", content_type, scanned)
        return None, None, None
    chosen_uid = _weighted_choice(pool, cooldown_counts or {})
    cand, name, count = pool[chosen_uid]
    log.info(
        "[custom:%s] scanned %d messages → %d contributors → picked from %s (%d submissions)",
        content_type, scanned, len(pool), name, count,
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
        lines.append(f"  {name} — **{count}** submission{'s' if count != 1 else ''}")
    return "\n".join(lines)


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
        f"Quote Feature:       {'Enabled' if c['enable_daily_quote'] else 'Disabled'}",
        f"Song Feature:        {'Enabled' if c['enable_daily_song']  else 'Disabled'}",
        f"Cooldown:            {'Enabled' if c['enable_cooldown']    else 'Disabled'}",
        f"Bracket Tracking:    {tracking}",
        f"Bracket Size:        {c['bracket_size'] or 8}",
        f"Bracket Vote Hours:  {c['bracket_voting_hours'] or 24}",
        f"Bracket Pacing:      {c.get('bracket_pacing') or 'round'}",
        f"Seasons Defined:     {season_count}",
        f"Timezone:            {c['timezone'] or 'US/Eastern'}",
        f"Quote Time:          {c['quote_time'] or '4:00'}",
        f"Song Time:           {c['song_time'] or '10:00'}",
    ]
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

    # While a bracket is running, the server name is frozen — the bracket winner
    # sets it. Renames resume once the bracket completes. Previews are exempt.
    if not preview and get_active_bracket(guild_id):
        log.info("[%s] Active bracket — skipping rename (server name frozen until winner).", guild_id)
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

    (quote, quote_user, quote_uid), (image_url, icon_user, icon_uid) = await asyncio.gather(
        get_random_quote(quote_channel, cooldown_counts=q_cd),
        get_random_icon( icon_channel,  cooldown_counts=i_cd),
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
        try:
            await guild.edit(name=truncate_to_100_chars(quote), icon=icon_bytes)
            log.info('[%s] Server renamed to: "%s"', guild_id, quote)
        except discord.HTTPException as e:
            log.error("[%s] Failed to rename guild: %s", guild_id, e)
        if quote_uid:
            log_pick(guild_id, quote_uid, quote_user or "Unknown", "quote", quote)
        if icon_uid:
            log_pick(guild_id, icon_uid, icon_user or "Unknown", "icon", image_url)

    image_file = await generate_card(
        quote,
        quote_user or "Unknown",
        icon_user  or "Unknown",
        icon_bytes,
    )
    if not image_file:
        return

    if preview:
        if override_post_channel:
            image_file.seek(0)
            try:
                await override_post_channel.send(
                    f"🔍 **Preview** — Quote by {quote_user}, icon by {icon_user}:\n> {quote}",
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

        # Bracket tracking: record the official daily post so its reactions can
        # seed a bracket later. Tracking is always on once a post channel is set;
        # we only track the configured post_channel message (never !rename run in
        # an arbitrary channel). voting_enabled_at is stamped once as a
        # "tracking since" floor so brackets never count pre-tracking history.
        is_official = post_chan_id and channel.id == post_chan_id
        if is_official:
            # Grab the attachment URL as a cached snapshot.
            # (We always re-fetch fresh at bracket time since CDN URLs expire.)
            img_url = sent.attachments[0].url if sent.attachments else None
            store_rename_post(guild_id, sent.id, sent.channel.id, quote, quote_user, quote_uid, img_url)
            if not cfg["voting_enabled_at"]:
                set_config(guild_id, "voting_enabled_at", datetime.now(pytz.utc).isoformat())


# In-progress guard, keyed by (guild_id, feature_id), so a slow scan can't
# overlap with itself. (Song-of-the-day is now just a custom feature.)
_custom_running: set[tuple[int, int]] = set()


async def process_custom_daily(
    guild_id: int,
    client: discord.Client,
    feature,
    override_post_channel=None,
    preview: bool = False,
) -> None:
    """
    Run one admin-defined "X of the day" feature: fairly pick a matching item
    from its source channel and repost it to its post channel. Generalizes the
    old song-of-the-day flow. *feature* is a custom_features Row.
    """
    key = (guild_id, feature["id"])
    if not preview and key in _custom_running:
        log.warning("[%s] Custom feature '%s' already running — skipping.", guild_id, feature["name"])
        return
    if not preview:
        _custom_running.add(key)
    try:
        cfg      = get_config(guild_id)
        name     = feature["name"]
        ctype    = feature["content_type"]
        category = f"custom:{name.lower()}"

        if cfg["enable_cooldown"] and not preview:
            tz    = _guild_tz(cfg)
            since = _today_since_utc(tz)
            cd    = get_today_pick_counts(guild_id, category, since)
        else:
            cd = {}

        source       = client.get_channel(feature["source_channel"])
        post_channel = override_post_channel or client.get_channel(feature["post_channel"])
        if not source:
            log.error("[%s] Custom feature '%s': source channel not found.", guild_id, name)
            return
        if not post_channel:
            log.error("[%s] Custom feature '%s': post channel not found.", guild_id, name)
            return

        cand, user, uid = await get_random_content(source, ctype, cooldown_counts=cd)
        if not cand:
            await post_channel.send(
                f"⚠️ No eligible **{ctype}** content found in {source.mention} for **{name}**."
            )
            return

        prefix  = "🔍 **Preview** — " if preview else ""
        emoji   = (feature["emoji"] + " ") if feature["emoji"] else ""
        caption = f"{prefix}{emoji}**{name}** (from {user}):"

        if cand["kind"] == "attachment":
            logged_item = cand["url"]
            guild = client.get_guild(guild_id)
            limit = getattr(guild, "filesize_limit", 8 * 1024 * 1024) if guild else 8 * 1024 * 1024
            posted = False
            if cand["size"] and cand["size"] <= limit:
                # Reupload the file so it embeds reliably and doesn't rely on the
                # source message's (expiring) CDN URL.
                try:
                    async with aiohttp.ClientSession(timeout=_AIOHTTP_TIMEOUT) as session:
                        async with session.get(cand["url"]) as resp:
                            resp.raise_for_status()
                            data = await resp.read()
                    from io import BytesIO
                    await post_channel.send(content=caption, file=discord.File(BytesIO(data), filename=cand["filename"]))
                    posted = True
                except Exception as e:
                    log.warning("[%s] Custom feature '%s': reupload failed (%s) — posting URL.", guild_id, name, e)
            if not posted:
                await post_channel.send(f"{caption}\n{cand['url']}")
        else:
            logged_item = cand["content"]
            await post_channel.send(f"{caption}\n{cand['content']}")

        if not preview and uid:
            log_pick(guild_id, uid, user or "Unknown", category, logged_item)
            log.info("[%s] Posted custom feature '%s'.", guild_id, name)
    except Exception as e:
        log.error("[%s] Custom feature '%s' failed: %s", guild_id, feature["name"], e)
    finally:
        if not preview:
            _custom_running.discard(key)


# ── Scheduling (per-guild times and timezones) ───────────────────────────────

async def scheduler_loop(client: discord.Client) -> None:
    await client.wait_until_ready()
    log.info("⏰ Scheduler loop started (checking every 60 s)")
    from bracket import check_bracket_advancement
    while not client.is_closed():
        await asyncio.sleep(60)
        now_utc = datetime.now(pytz.utc)
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
                    if cur_time == scheduled and cfg["last_quote_date"] != today:
                        set_config(guild.id, "last_quote_date", today)
                        await process_rename(guild.id, client)

                # Admin-defined "X of the day" features (song-of-the-day is one).
                for feat in get_custom_features(guild.id):
                    if not feat["enabled"]:
                        continue
                    try:
                        scheduled = _normalize_time(feat["post_time"])
                    except Exception:
                        continue
                    if cur_time == scheduled and feat["last_run_date"] != today:
                        set_custom_feature_run_date(feat["id"], today)
                        await process_custom_daily(guild.id, client, feat)

                # Check for bracket matchups that need tallying/advancing
                await check_bracket_advancement(guild.id, client)
            except Exception:
                log.exception("[%s] Scheduler tick failed for guild — continuing.", guild.id)
