import asyncio
import logging
import random
import re
from datetime import datetime, timedelta

import aiohttp
import discord
import pytz

from db_utils import get_config
from image_utils import generate_card, truncate_to_100_chars

log = logging.getLogger(__name__)

_AIOHTTP_TIMEOUT = aiohttp.ClientTimeout(total=15)

_EST = pytz.timezone("US/Eastern")

_MUSIC_PATTERN = re.compile(
    r"https?://(?:www\.)?(?:youtube\.com|youtu\.be|soundcloud\.com|spotify\.com)/\S+",
    re.IGNORECASE,
)


def is_music_link(line: str) -> bool:
    return bool(_MUSIC_PATTERN.search(line))


# ── History helpers (two-stage fair sampling) ────────────────────────────────
#
# Problem: a user with 500 posts would dominate random.choice() over a user
# with 5 posts.
#
# Solution — two stages:
#   1. Stream all messages.  For each user, run an independent reservoir
#      (Algorithm R, k=1) so every user ends up with one randomly chosen
#      item from their own submissions.
#   2. Pick one user uniformly from the contributor pool.
#
# Result: every contributor has an equal 1/U probability of being selected
# (U = number of unique contributors), regardless of how many items they
# posted.  Memory is O(U) — one stored item per unique user — rather than
# O(total messages).
#
# User identity is keyed on Discord user ID (stable across display-name
# changes) but we store the display name for the card/post.

async def get_random_quote(channel) -> tuple[str | None, str | None]:
    if channel is None:
        return None, None

    # user_id -> (chosen_line, display_name, submission_count)
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
            prev_line, name, count = pool.get(uid, (None, msg.author.display_name, 0))
            count += 1
            if random.randint(1, count) == 1:
                pool[uid] = (stripped, msg.author.display_name, count)
            else:
                pool[uid] = (prev_line, name, count)

    if not pool:
        log.info("[quote] scanned %d messages → 0 contributors", scanned)
        return None, None

    chosen_uid = random.choice(list(pool.keys()))
    chosen_line, chosen_name, chosen_count = pool[chosen_uid]

    counts = {name: c for _, name, c in pool.values()}
    log.info(
        "[quote] scanned %d messages → %d contributors %s → picked %r from %s (%d submissions)",
        scanned, len(pool), dict(counts), chosen_line, chosen_name, chosen_count,
    )
    return chosen_line, chosen_name


async def get_random_icon(channel) -> tuple[str | None, str | None]:
    if channel is None:
        return None, None

    # user_id -> (chosen_url, display_name, submission_count)
    pool: dict[int, tuple[str, str, int]] = {}
    scanned = 0

    async for msg in channel.history(limit=None, oldest_first=False):
        scanned += 1
        if msg.author.bot:
            continue
        uid = msg.author.id
        for attachment in msg.attachments:
            if not (attachment.content_type and attachment.content_type.startswith("image")):
                continue
            prev_url, name, count = pool.get(uid, (None, msg.author.display_name, 0))
            count += 1
            if random.randint(1, count) == 1:
                pool[uid] = (attachment.url, msg.author.display_name, count)
            else:
                pool[uid] = (prev_url, name, count)

    if not pool:
        log.info("[icon] scanned %d messages → 0 contributors", scanned)
        return None, None

    chosen_uid = random.choice(list(pool.keys()))
    chosen_url, chosen_name, chosen_count = pool[chosen_uid]

    counts = {name: c for _, name, c in pool.values()}
    log.info(
        "[icon] scanned %d messages → %d contributors %s → picked image from %s (%d submissions)",
        scanned, len(pool), dict(counts), chosen_name, chosen_count,
    )
    return chosen_url, chosen_name


async def get_random_song(channel) -> tuple[str | None, str | None]:
    if channel is None:
        return None, None

    # user_id -> (chosen_link, display_name, submission_count)
    pool: dict[int, tuple[str, str, int]] = {}
    scanned = 0

    async for msg in channel.history(limit=None, oldest_first=False):
        scanned += 1
        if msg.author.bot:
            continue
        uid = msg.author.id
        for line in msg.content.strip().splitlines():
            stripped = line.strip()
            if not stripped or not is_music_link(stripped):
                continue
            prev_link, name, count = pool.get(uid, (None, msg.author.display_name, 0))
            count += 1
            if random.randint(1, count) == 1:
                pool[uid] = (stripped, msg.author.display_name, count)
            else:
                pool[uid] = (prev_link, name, count)

    if not pool:
        log.info("[song] scanned %d messages → 0 contributors", scanned)
        return None, None

    chosen_uid = random.choice(list(pool.keys()))
    chosen_link, chosen_name, chosen_count = pool[chosen_uid]

    counts = {name: c for _, name, c in pool.values()}
    log.info(
        "[song] scanned %d messages → %d contributors %s → picked %r from %s (%d submissions)",
        scanned, len(pool), dict(counts), chosen_link, chosen_name, chosen_count,
    )
    return chosen_link, chosen_name


# ── Core feature logic ───────────────────────────────────────────────────────

async def process_rename(guild_id: int, client: discord.Client, override_post_channel=None) -> None:
    cfg = get_config(guild_id)
    quote_channel = client.get_channel(cfg[1])
    icon_channel  = client.get_channel(cfg[2])
    post_channel  = client.get_channel(cfg[3])
    guild         = client.get_guild(guild_id)

    if not guild:
        log.warning("[%s] Guild not found — skipping rename.", guild_id)
        return

    (quote, quote_user), (image_url, icon_user) = await asyncio.gather(
        get_random_quote(quote_channel),
        get_random_icon(icon_channel),
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

    try:
        await guild.edit(name=truncate_to_100_chars(quote), icon=icon_bytes)
        log.info('[%s] Server renamed to: "%s"', guild_id, quote)
    except discord.HTTPException as e:
        log.error("[%s] Failed to rename guild: %s", guild_id, e)

    image_file = await generate_card(
        quote,
        quote_user or "Unknown",
        icon_user  or "Unknown",
        icon_bytes,
    )
    if not image_file:
        return

    channels_to_post = []
    if override_post_channel:
        channels_to_post.append(override_post_channel)
    if post_channel and (
        not override_post_channel or post_channel.id != override_post_channel.id
    ):
        channels_to_post.append(post_channel)

    for channel in channels_to_post:
        image_file.seek(0)
        try:
            await channel.send(file=discord.File(fp=image_file, filename="update.png"))
        except discord.HTTPException as e:
            log.error("[%s] Failed to post card to channel %s: %s", guild_id, channel.id, e)


_is_song_searching: dict[int, bool] = {}


async def process_daily_song(guild_id: int, client: discord.Client) -> None:
    if _is_song_searching.get(guild_id):
        log.warning("[%s] Song search already in progress — skipping.", guild_id)
        cfg = get_config(guild_id)
        post_channel = client.get_channel(cfg[5])
        if post_channel:
            await post_channel.send("⚠️ Song search is already running. Please wait for it to finish.")
        return

    _is_song_searching[guild_id] = True
    try:
        cfg = get_config(guild_id)
        music_channel = client.get_channel(cfg[4])
        post_channel  = client.get_channel(cfg[5])

        if not music_channel:
            log.error("[%s] Music channel not configured or not found.", guild_id)
            return
        if not post_channel:
            log.error("[%s] Song post channel not configured or not found.", guild_id)
            return

        song, user = await get_random_song(music_channel)
        if not song:
            log.warning("[%s] No valid music link found in music channel.", guild_id)
            await post_channel.send("⚠️ No valid music link found in music channel.")
            return

        await post_channel.send(f"🎵 **Song of the Day** (from {user}):\n{song}")
        log.info("[%s] Posted song of the day: %s", guild_id, song)
    except Exception as e:
        log.error("[%s] Song post failed: %s", guild_id, e)
    finally:
        _is_song_searching[guild_id] = False


# ── Scheduling ───────────────────────────────────────────────────────────────

def get_seconds_until_time(timestr: str) -> float:
    hour, minute = (int(x) for x in timestr.strip().split(":"))
    now = datetime.now(_EST)
    next_run = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if now >= next_run:
        next_run += timedelta(days=1)
    return (next_run - now).total_seconds()


async def schedule_rename(client: discord.Client, quote_time: str) -> None:
    await client.wait_until_ready()
    while not client.is_closed():
        wait = get_seconds_until_time(quote_time)
        log.info("⏰ Next Quote of the Day in %.2fh (%s EST)", wait / 3_600, quote_time)
        await asyncio.sleep(wait)
        for guild in client.guilds:
            cfg = get_config(guild.id)
            if cfg[6]:
                await process_rename(guild.id, client)


async def schedule_daily_song(client: discord.Client, song_time: str) -> None:
    await client.wait_until_ready()
    while not client.is_closed():
        wait = get_seconds_until_time(song_time)
        log.info("⏰ Next Song of the Day in %.2fh (%s EST)", wait / 3_600, song_time)
        await asyncio.sleep(wait)
        for guild in client.guilds:
            cfg = get_config(guild.id)
            if cfg[7]:
                await process_daily_song(guild.id, client)
