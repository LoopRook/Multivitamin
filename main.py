import os
import discord
import logging

from db_utils import init_db, set_config, show_config, get_config
from bot_features import (
    process_rename,
    process_daily_song,
    schedule_rename,
    schedule_daily_song
)

TOKEN = os.getenv('DISCORD_TOKEN')
QUOTE_TIME = os.getenv('QUOTE_TIME', '4:00')
SONG_TIME = os.getenv('SONG_TIME', '10:00')

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.messages = True
client = discord.Client(intents=intents)

logging.basicConfig(level=logging.INFO)
logging.getLogger('discord.gateway').setLevel(logging.WARNING)

@client.event
async def on_ready():
    print(f"✅ Logged in as {client.user}")
    init_db()
    client.loop.create_task(schedule_rename(client, QUOTE_TIME))
    client.loop.create_task(schedule_daily_song(client, SONG_TIME))

@client.event
async def on_message(message):
    if message.author.bot or not message.guild:
        return
    # Ignore quoted/reply messages
    if getattr(message, "reference", None) is not None:
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
        await message.channel.send(f"```
{cfg_txt}
```")
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
            # Post image in the channel where command was used
            await process_rename(gid, client, override_post_channel=message.channel)
        else:
            await message.channel.send('⚠️ Daily Quote feature is disabled for this server.')
    elif content.startswith('!song'):
        cfg = get_config(gid)
        if cfg[7]:
            await process_daily_song(gid, client)
        else:
            await message.channel.send('⚠️ Daily Song feature is disabled for this server.')

if __name__ == "__main__":
    if not TOKEN:
        print("❌ DISCORD_TOKEN environment variable not set!")
    else:
        client.run(TOKEN)
