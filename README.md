# QOTD Discord Bot

A multi-server Discord bot that:

- **Renames your server daily** using community-submitted quotes + icons (fair, weighted sampling so no one dominates).
- **Posts a daily song** picked from a music channel.
- **Runs bracket championships** — reactions on rename cards seed a tournament of your favourite names, voted with native Discord polls. Supports calendar-year or custom **seasons** (monthly, holidays, etc.). The winning name becomes the server name.

All commands are **slash (`/`) commands**. The bot is multi-server, uses no `Administrator` permission, and stores each server's data separately.

## Add it to your server

Invite link (replace `YOUR_APPLICATION_ID` with the Application ID from the Discord Developer Portal → General Information):

```
https://discord.com/oauth2/authorize?client_id=YOUR_APPLICATION_ID&scope=bot+applications.commands&permissions=562949953539168
```

Scopes: `bot` + `applications.commands`. The permission integer `562949953539168` grants exactly: View Channels, Send Messages, Embed Links, Attach Files, Add Reactions, Read Message History, Send Polls, and **Manage Server** (needed to rename the server). Prefer the Dev Portal's **OAuth2 → URL Generator** to (re)generate this by ticking those boxes.

> Slash commands can take a few minutes to appear after inviting (Discord propagates global commands gradually).

## Quick start (in your server)

1. `/setup` — full setup guide, any time.
2. `/config postchannel` — the channel where official rename cards post (this turns on bracket tracking).
3. `/config quotechannel`, `/config iconchannel`, `/config musicchannel`, `/config songpostchannel`, `/config bracketchannel`.
4. `/config timezone` and `/config scheduletime quote 8:00` etc.
5. `/help` lists every command (it only shows admin sections to admins).

Admin commands require the **Manage Server** permission, or bot-admin access granted via `/admin add`.

## Self-hosting

Runs as a single worker process. See [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) for Railway/VM setup, the required Developer Portal settings (enable the **Message Content** intent, toggle **Public Bot**), and the ~100-server verification threshold.

```bash
pip install -r requirements.txt
DISCORD_TOKEN=your_token DB_FILE=/path/to/server_config.db python main.py
```

Environment: `DISCORD_TOKEN` (required), `DB_FILE` (optional, defaults to `/data/server_config.db` — put it on a persistent volume).
