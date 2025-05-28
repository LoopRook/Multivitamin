import discord
import random
import aiohttp
import re
import asyncio

from db_utils import get_config, set_config, show_config
from image_utils import generate_card, truncate_to_100_chars

MUSIC_URL_PATTERNS = [
    r"(https?://)?(www\.)?(youtube\.com|youtu\.be|soundcloud\.com|spotify\.com)/[^\s]+"
]

def is_music_link(line):
    return any(re.search(pattern, line, re.IGNORECASE) for pattern in MUSIC_URL_PATTERNS)

async def get_random_quote(channel):
    all_lines = []
    async for message in channel.history(limit=None, oldest_first=False):
        if message.author.bot:
            continue
        # Only include lines that are NOT commands
        lines = message.content.strip().splitlines()
        for line in lines:
            if line.strip() and not line.strip().startswith('!'):
                all_lines.append((line, message.author.display_name))
    return random.choice(all_lines) if all_lines else (None, None)

async def get_random_icon(channel):
    all_images = []
    async for message in channel.history(limit=None, oldest_first=False):
        if message.author.bot:
            continue
        for attachment in message.attachments:
            if attachment.content_type and attachment.content_type.startswith("image"):
                all_images.append((attachment.url, message.author.display_name))
    return random.choice(all_images) if all_images else (None, None)

async def process_rename(guild_id, client, override_post_channel=None):
    cfg = get_config(guild_id)
    quote_channel = client.get_channel(cfg[1])
    icon_channel = client.get_channel(cfg[2])
    post_channel = client.get_channel(cfg[3])
    guild = client.get_guild(guild_id)

    quote, quote_user = await get_random_quote(quote_channel)
    image_url, icon_user = await get_random_icon(icon_channel)

    if not quote or not image_url:
        print(f"⚠️ No valid quote or image found for guild {guild_id}")
        return

    async with aiohttp.ClientSession() as session:
        async with session.get(image_url) as resp:
            icon_bytes = await resp.read()

    await guild.edit(name=truncate_to_100_chars(quote), icon=icon_bytes)
    print(f"📝 [{guild_id}] Server renamed to: \"{quote}\"")

    image_file = await generate_card(quote, quote_user or "Unknown", icon_user or "Unknown", icon_bytes)
    if image_file:
        image_file.seek(0)
        target_channel = override_post_channel or post_channel
        await target_channel.send(file=discord.File(fp=image_file, filename="update.png"))

async def get_random_song(channel):
    all_links = []
    async for message in channel.history(limit=None, oldest_first=False):
        if message.author.bot:
            continue
        lines = message.content.strip().splitlines()
        for line in lines:
            if line.strip() and is_music_link(line.strip()):
                all_links.append((line.strip(), message.author.display_name))
    return random.choice(all_links) if all_links else (None, None)

is_song_searching = {}

async def process_daily_song(guild_id, client):
    cfg = get_config(guild_id)
    music_channel = client.get_channel(cfg[4])
    post_channel = client.get_channel(cfg[5])
    if is_song_searching.get(guild_id, False):
        print(f"⚠️ Song search already in progress for guild {guild_id}. Skipping.")
        if post_channel:
            await post_channel.send("⚠️ Song search is already running. Please wait for it to finish.")
        return
    is_song_searching[guild_id] = True
    try:
        print(f"DEBUG: Entered process_daily_song() for guild {guild_id}")
        if not music_channel:
            print("❌ Music channel not found.")
            is_song_searching[guild_id] = False
            return
        if not post_channel:
            print("❌ Song post channel not found.")
            is_song_searching[guild_id] = False
            return
        song, user = await get_random_song(music_channel)
        print(f"DEBUG: Song={song}, User={user}")
        if not song:
            print("⚠️ No valid music link found in music channel.")
            await post_channel.send("⚠️ No valid music link found in music channel.")
            is_song_searching[guild_id] = False
            return
        await post_channel.send(f"🎵 **Song of the Day** (from {user}):\n{song}")
        print(f"🎵 Posted song of the day: {song}")
    except Exception as e:
        print(f"❌ Song post failed: {e}")
    finally:
        is_song_searching[guild_id] = False

def get_seconds_until_time(timestr):
    from datetime import datetime, timedelta
    import pytz
    hour, minute = [int(x) for x in timestr.strip().split(":")]
    tz = pytz.timezone('US/Eastern')
    now = datetime.now(tz)
    next_run = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if now >= next_run:
        next_run += timedelta(days=1)
    return (next_run - now).total_seconds()

async def schedule_rename(client, quote_time):
    await client.wait_until_ready()
    while not client.is_closed():
        wait_time = get_seconds_until_time(quote_time)
        print(f"⏰ Sleeping for {wait_time/3600:.2f} hours until Quote of the Day ({quote_time} EST)")
        await asyncio.sleep(wait_time)
        for guild in client.guilds:
            cfg = get_config(guild.id)
            if cfg[6]:  # enable_daily_quote
                await process_rename(guild.id, client)

async def schedule_daily_song(client, song_time):
    await client.wait_until_ready()
    while not client.is_closed():
        wait_time = get_seconds_until_time(song_time)
        print(f"⏰ Sleeping for {wait_time/3600:.2f} hours until Song of the Day ({song_time} EST)")
        await asyncio.sleep(wait_time)
        for guild in client.guilds:
            cfg = get_config(guild.id)
            if cfg[7]:  # enable_daily_song
                await process_daily_song(guild.id, client)
