# Discord Quote & Song Bot - Multi-Server, SQLite, Per-Server Feature Toggles
# All scheduled times are in EST (US/Eastern). Configure accordingly.
# Features: per-server config, persistent with SQLite, admin commands for setup

import discord
import random
import asyncio
import datetime
import pytz
import aiohttp
import re
from io import BytesIO
from PIL import Image, ImageDraw, ImageFont, ImageFilter
import logging
import os
import sqlite3
import string

TOKEN = os.getenv('DISCORD_TOKEN')
QUOTE_TIME = os.getenv('QUOTE_TIME', '4:00')  # 4:00 AM EST
SONG_TIME = os.getenv('SONG_TIME', '10:00')   # 10:00 AM EST

# ================ SQLITE CONFIG ================
DB_FILE = 'server_config.db'

CREATE_TABLE = '''CREATE TABLE IF NOT EXISTS server_config (
    guild_id INTEGER PRIMARY KEY,
    quote_channel INTEGER,
    icon_channel INTEGER,
    post_channel INTEGER,
    music_channel INTEGER,
    song_post_channel INTEGER,
    enable_daily_quote INTEGER DEFAULT 1,
    enable_daily_song INTEGER DEFAULT 1
)'''

def db_conn():
    return sqlite3.connect(DB_FILE)

def get_config(guild_id):
    with db_conn() as conn:
        row = conn.execute('SELECT * FROM server_config WHERE guild_id=?', (guild_id,)).fetchone()
        if not row:
            # Insert default config if missing
            conn.execute('INSERT INTO server_config (guild_id) VALUES (?)', (guild_id,))
            conn.commit()
            row = conn.execute('SELECT * FROM server_config WHERE guild_id=?', (guild_id,)).fetchone()
        return row

def set_config(guild_id, field, value):
    with db_conn() as conn:
        conn.execute(f'UPDATE server_config SET {field}=? WHERE guild_id=?', (value, guild_id))
        conn.commit()

def show_config(guild_id):
    cfg = get_config(guild_id)
    fields = ['Guild ID', 'Quote Channel', 'Icon Channel', 'Post Channel', 'Music Channel', 'Song Post Channel', 'Quote Feature Enabled', 'Song Feature Enabled']
    return '\n'.join([f"{fields[i]}: {cfg[i] if cfg[i] is not None else 'Not Set'}" for i in range(len(fields))])

# ================ BOT INITIALIZATION ================
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.messages = True
client = discord.Client(intents=intents)

logging.basicConfig(level=logging.INFO)
logging.getLogger('discord.gateway').setLevel(logging.WARNING)

# ================ FONT UTILS ================
def load_fonts(size):
    fonts = []
    paths = [
        "DejaVuSans-Bold.ttf",
        "NotoSansCJK-Bold.ttc"
    ]
    for path in paths:
        try:
            font = ImageFont.truetype(path, size)
            fonts.append(font)
        except Exception as e:
            print(f"❌ Failed to load font '{path}': {e}")
            fonts.append(None)
    return fonts

font_names = ["DejaVuSans", "NotoSansCJK"]

def is_pure_ascii(text):
    return all(c in string.ascii_letters + string.digits + string.punctuation + " " for c in text)

def choose_font(text, fonts, names):
    if is_pure_ascii(text) and fonts[0] is not None:
        return fonts[0]  # DejaVuSans
    for font, name in zip(fonts, names):
        if font and can_render_all(text, font, name):
            return font
    return fonts[0]

def can_render_all(text, font, name):
    try:
        for char in text:
            if char == ' ':
                continue
            if not font.getmask(char).getbbox():
                print(f"❌ Font '{name}' cannot render '{char}' (U+{ord(char):04X})")
                return False
        print(f"✅ Font '{name}' supports full string: \"{text}\"")
        return True
    except Exception as e:
        print(f"❌ Exception checking '{name}': {e}")
        return False

def seconds_until_time_str(timestr):
    hour, minute = [int(x) for x in timestr.strip().split(":")]
    tz = pytz.timezone('US/Eastern')
    now = datetime.datetime.now(tz)
    next_run = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if now >= next_run:
        next_run += datetime.timedelta(days=1)
    return (next_run - now).total_seconds()

def truncate_to_100_chars(text):
    return text if len(text) <= 100 else text[:97].rsplit(' ', 1)[0] + '...'

# ================ BOT FEATURES ================
async def generate_card(server_name, quote_user, icon_user, icon_bytes):
    try:
        base = Image.new("RGBA", (800, 450), (0, 0, 0, 255))
        icon = Image.open(BytesIO(icon_bytes)).convert("RGBA").resize((400, 400))
        blurred_bg = icon.resize((800, 450)).filter(ImageFilter.GaussianBlur(12))
        base.paste(blurred_bg, (0, 0))

        mask = Image.new("L", (400, 400), 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, 400, 400), radius=40, fill=255)
        icon.putalpha(mask)
        base.paste(icon, (25, 25), icon)

        draw = ImageDraw.Draw(base)
        title_fonts = load_fonts(36)
        meta_fonts = load_fonts(24)
        words, line, lines = server_name.split(), "", []
        for word in words:
            test = f"{line} {word}".strip()
            font = choose_font(test, title_fonts, font_names) or title_fonts[0]
            if draw.textlength(test, font=font) < 300:
                line = test
            else:
                lines.append(line)
                line = word
        lines.append(line)

        y_text = 80
        for line in lines:
            font = choose_font(line, title_fonts, font_names) or title_fonts[0]
            draw.text((450, y_text), line, font=font, fill=(255, 255, 255))
            y_text += 40

        def render_meta(label, name, offset):
            font = choose_font(name, meta_fonts, font_names)
            if not font or not can_render_all(name, font, label):
                print(f"⚠️ Fallback: {label} '{name}' has unsupported glyphs, using 'Unknown'")
                name = "Unknown"
                font = meta_fonts[0]
            color = (200, 200, 200) if label == "Quote by" else (180, 180, 180)
            draw.text((450, offset), f"{label}: {name}", font=font, fill=color)

        render_meta("Quote by", quote_user, y_text + 10)
        render_meta("Icon by", icon_user, y_text + 50)

        buffer = BytesIO()
        base.save(buffer, format="PNG")
        buffer.seek(0)
        return buffer
    except Exception as e:
        print(f"❌ Image generation failed: {e}")
        return None

async def get_random_quote(channel):
    all_lines = []
    async for message in channel.history(limit=None, oldest_first=False):
        if message.author.bot:
            continue
        lines = message.content.strip().splitlines()
        all_lines.extend([(line, message.author.display_name) for line in lines if line.strip()])
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

async def process_rename(guild_id):
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
        await post_channel.send(file=discord.File(fp=image_file, filename="update.png"))

async def schedule_rename():
    await client.wait_until_ready()
    while not client.is_closed():
        wait_time = seconds_until_time_str(QUOTE_TIME)
        print(f"⏰ Sleeping for {wait_time/3600:.2f} hours until Quote of the Day ({QUOTE_TIME} EST)")
        await asyncio.sleep(wait_time)
        for guild in client.guilds:
            cfg = get_config(guild.id)
            if cfg[6]:  # enable_daily_quote
                await process_rename(guild.id)

MUSIC_URL_PATTERNS = [
    r"(https?://)?(www\.)?(youtube\.com|youtu\.be|soundcloud\.com|spotify\.com)/[^\s]+"
]

def is_music_link(line):
    return any(re.search(pattern, line, re.IGNORECASE) for pattern in MUSIC_URL_PATTERNS)

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

async def process_daily_song(guild_id):
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
        await post_channel.send(f"""🎵 **Song of the Day** (from {user}):\n{song}""")
        print(f"🎵 Posted song of the day: {song}")
    except Exception as e:
        print(f"❌ Song post failed: {e}")
    finally:
        is_song_searching[guild_id] = False

async def schedule_daily_song():
    await client.wait_until_ready()
    while not client.is_closed():
        wait_time = seconds_until_time_str(SONG_TIME)
        print(f"⏰ Sleeping for {wait_time/3600:.2f} hours until Song of the Day ({SONG_TIME} EST)")
        await asyncio.sleep(wait_time)
        for guild in client.guilds:
            cfg = get_config(guild.id)
            if cfg[7]:  # enable_daily_song
                await process_daily_song(guild.id)

# ================ COMMANDS ================
def admin_only():
    async def predicate(ctx):
        return ctx.author.guild_permissions.manage_guild
    return discord.app_commands.check(predicate)

@client.event
async def on_ready():
    print(f"✅ Logged in as {client.user}")
    # Setup DB
    with db_conn() as conn:
        conn.execute(CREATE_TABLE)
    # Start scheduled features if enabled
    client.loop.create_task(schedule_rename())
    client.loop.create_task(schedule_daily_song())

@client.event
async def on_message(message):
    if message.author.bot or not message.guild:
        return
    # ADMIN ONLY
    if not message.author.guild_permissions.manage_guild:
        return
    content = message.content.lower()
    gid = message.guild.id
    # Channel Setters
    if content.startswith('!setquotechannel'):
        set_config(gid, 'quote_channel', message.channel.id)
        await message.channel.send('✅ This channel set as Quote Channel.')
    elif content.startswith('!seticonchannel'):
        set_config(gid, 'icon_channel', message.channel.id)
        await message.channel.send('✅ This channel set as Icon Channel.')
    elif content.startswith('!setpostchannel'):
        set_config(gid, 'post_channel', message.channel.id)
        await message.channel.send('✅ This channel set as Post Channel.')
    elif content.startswith('!setmusicchannel'):
        set_config(gid, 'music_channel', message.channel.id)
        await message.channel.send('✅ This channel set as Music Channel.')
    elif content.startswith('!setsongpostchannel'):
        set_config(gid, 'song_post_channel', message.channel.id)
        await message.channel.send('✅ This channel set as Song Post Channel.')
    # Feature Toggles
    elif content.startswith('!enablefeature '):
        arg = content.split(' ', 1)[1].strip()
        if arg == 'quote':
            set_config(gid, 'enable_daily_quote', 1)
            await message.channel.send('✅ Daily Quote feature enabled.')
        elif arg == 'song':
            set_config(gid, 'enable_daily_song', 1)
            await message.channel.send('✅ Daily Song feature enabled.')
    elif content.startswith('!disablefeature '):
        arg = content.split(' ', 1)[1].strip()
        if arg == 'quote':
            set_config(gid, 'enable_daily_quote', 0)
            await message.channel.send('✅ Daily Quote feature disabled.')
        elif arg == 'song':
            set_config(gid, 'enable_daily_song', 0)
            await message.channel.send('✅ Daily Song feature disabled.')
    elif content.startswith('!showconfig'):
        cfg_txt = show_config(gid)
        await message.channel.send(f"```\n{cfg_txt}\n```")
    elif content.startswith('!setup'):
        setup_text = (
            "**Bot Setup Guide:**\n"
            "1. In each channel, use the appropriate setup command:\n"
            "   - !setquotechannel\n   - !seticonchannel\n   - !setpostchannel\n   - !setmusicchannel\n   - !setsongpostchannel\n"
            "2. Use !enablefeature [quote|song] or !disablefeature [quote|song] to toggle features.\n"
            "3. Use !showconfig to see your current config.\n"
            "4. All scheduled times are EST."
        )
        await message.channel.send(setup_text)
    # Manual trigger (only if features enabled)
    elif content.startswith('!rename'):
        cfg = get_config(gid)
        if cfg[6]:
            await process_rename(gid)
        else:
            await message.channel.send('⚠️ Daily Quote feature is disabled for this server.')
    elif content.startswith('!song'):
    cfg = get_config(gid)
    if cfg[7]:
        await process_daily_song(gid)
    else:
        await message.channel.send('⚠️ Daily Song feature is disabled for this server.')
