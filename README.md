# QOTD — the server-renaming Discord bot

Your members write quotes. Every day, the bot picks one, pairs it with a member-submitted image, and **renames the whole server after it** — with a generated card crediting who wrote it. That's the whole core, and it runs itself.

Optionally, the year's favorite names can fight it out in a **bracket championship**, and the winner becomes the server name.

It also lets admins spin up their own recurring posts — Meme of the Day, Critter of the Week, Song of the Day — each with its own on-demand slash command.

All slash commands, fully multi-server, no `Administrator` permission, per-server data.

---

## How it works — the loop

1. **Members drop quotes** in a quote channel — every line of every message is a candidate. Out-of-context gems work best:
   > *Bandits usually do not expect being robbed. You can use this to your advantage*
2. **Members drop images** in an icon channel (any image attachment).
3. **Every day** (or on whatever schedule you set), the bot picks a quote and an icon — *fairly*: one candidate per person, weighted so whoever got picked recently is less likely to be picked again — renames the server, sets the icon, and posts a **card** with the quote and credits.
4. **Members react** to the cards they love. Any emoji counts.

That's the complete daily loop — steps 1–4 repeat forever with zero upkeep. Then, **optionally, whenever you choose**: those accumulated reactions seed a single-elimination **bracket** voted with native Discord polls, the server gets live-renamed to the winners as rounds progress, and the champion holds the name. Reactions are tracked automatically from day one, so the option is always there — run one bracket a year, one a month, or never.

---

## Add it to your server

You need **Manage Server** on the target server:

```
https://discord.com/oauth2/authorize?client_id=1374255006433415248&scope=bot+applications.commands&permissions=562949953539312
```

The permission integer grants exactly: View Channels, Send Messages, Embed Links, Attach Files, Add Reactions, Read Message History, Send Polls, **Manage Server** (the rename itself), plus two optional conveniences — **View Audit Log** (DMs a setup guide to whoever invited it, instead of posting in #general) and **Manage Channels** (lets `/setup` create your channels for you). Missing the optional two just degrades gracefully.

> Slash commands can take a few minutes to appear after inviting — Discord propagates global commands gradually.

## Set it up (2 minutes)

Run **`/setup`** — a stepped wizard in one ephemeral message:

1. **Channels** — pick (or one-click create) the quote channel, icon channel, post channel (where daily cards land — this is what brackets track), and an optional **best-of** channel (more below).
2. **Timezone** — so "8:00" means *your* 8:00.
3. **Schedule** — daily, every N days, or specific weekdays ("every Sunday"), and the time.

That's it — the bot starts renaming on schedule. Re-run `/setup` any time to change a channel; each dropdown applies as picked. Check the state of everything with `/showconfig` (it includes a health check for missing permissions).

### Try it immediately

```
/preview          — dry-run a rename card in this channel (nothing is renamed)
/rename           — do a real rename right now
/bracket test     — full dry-run bracket with random scores (never renames the server)
```

---

## Everyday commands (members)

| Command | What it does |
|---|---|
| `/mystats` | Your submission counts and when you were last picked |
| `/contributors quote` (or `icon`) | Submission leaderboard |
| `/rename` | Trigger a rename now (admins can restrict this: `/config feature openrename off`) |

Renames are rate-limited by Discord (~2 per 10 minutes) — the bot tells you how long to wait rather than hanging.

---

## Brackets — the championship (optional)

Nothing here is required — the daily renames are a complete experience on their own. But reactions on the cards accumulate automatically, so whenever you're ready:

```
/bracket start
```

One panel, everything per-run: **where it posts** (pick or create a channel), **scope** (this year, last year, or a season), **size** (4–32), **voting window** (6h–1 week), **pacing**, and **source**. It shows the estimated total length before you launch — a 32-name daily-paced bracket at 24h/matchup is a month-long event; an 8-name round-paced one is a weekend.

- **Round pacing** — each round's matchups post at once and vote in parallel. The server name rotates through the previous round's winners, each holding it for an equal slice.
- **Daily pacing** — one matchup at a time; each winner immediately becomes the server name. Slow-burn.

Matchups are native Discord polls with both cards displayed. Ties get a dramatic coin flip. The champion gets an announcement with their winning card, the server name, and a place in `/bracket history`.

**Seasons**: define named windows with `/season` (e.g. "Halloween 2026" = Oct 1–31) and run brackets scoped to them — monthly brackets, holiday brackets, whatever.

### The best-of channel (optional, recommended)

Instead of seeding from *every* card's reactions, set a **best-of channel** in `/setup`: members **forward** their favorite rename cards into it (Discord's native Forward), and the bracket seeds from reactions on those forwards. The bot confirms each nomination with ℹ️ (or 🔁 for a duplicate) and pins instructions in the channel. Screenshots don't count — only real forwards, so nominations stay traceable to the original card. Each `/bracket start` chooses between best-of and all-renames seeding for that run.

---

## Custom "X of the day" features

Make the bot post *anything* on a schedule, sourced from a channel:

```
/feature setup
```

Pick a source channel, a destination, a type — `media` (images/gifs), `link`, `music` (YouTube/Spotify/SoundCloud), or `text` — then name it and give it a command slug. Example: a "Meme of the Day" sourced from #memes gets its own `/meme` command that anyone (or just admins, or a role — `/feature access`) can run on demand, plus a scheduled daily post. Same fair sampling as renames.

```
/feature list · edit · schedule · toggle · remove · access
```

Each feature has its own cadence (`/feature schedule meme` → every 3 days, or Fridays only).

---

## Configuration reference

| Command | What |
|---|---|
| `/setup` | The wizard — channels, timezone, schedule |
| `/config schedule` | Rename cadence + time (panel) |
| `/config credits` | How contributors are named — server nickname vs @username, and whether the champion gets pinged |
| `/config feature <quote\|cooldown\|openrename> <on/off>` | Feature toggles |
| `/config timezone <tz>` | Any IANA zone |
| `/season` | Manage named bracket windows |
| `/admin add/remove/list` | Grant bot-admin to non-managers |
| `/admin reset` | ⚠️ Wipe this server's bot data (two-step confirm) |
| `/showconfig` | Everything + health check |

Admin commands require **Manage Server** or bot-admin via `/admin add`. `/help` shows the full reference (admin sections only to admins).

---

## Self-hosting

Python 3.12, discord.py 2.7.1, SQLite. Runs as a single worker process (Railway, a VPS, anything).

```bash
pip install -r requirements.txt
DISCORD_TOKEN=your_token DB_FILE=/path/to/server_config.db python main.py
```

- **Developer Portal**: enable BOTH privileged intents — **Message Content** (channel scanning) and **Server Members** (live contributor names). The bot will not log in without them. Toggle **Public Bot** on if you want others to invite it.
- `DB_FILE` defaults to `/data/server_config.db` — put it on a persistent volume. Schema creates and migrates itself on boot.
- Verification (and a privacy policy) is required by Discord past ~100 servers.

Tests: `pip install -r requirements-dev.txt && python -m pytest tests/ -q`

## Data

Per-server only: channel/schedule config, a log of picks (for fair sampling and `/mystats`), tracked rename cards (quote text, submitter, message ids — this is what seeds brackets), and bracket history. No message content is stored beyond the quotes the bot itself posted on cards. `/admin reset` wipes a server's data completely.
