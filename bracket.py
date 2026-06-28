"""
bracket.py — Yearly rename bracket championship.

Rename posts accumulate freeform reactions from users all year (any emoji counts).
At bracket time, total reaction count seeds the bracket.
Matchups use Discord native polls so users vote cleanly without specific emojis.
Each matchup post shows both rename cards as embeds so voters can see what they're choosing.
"""

import asyncio
import logging
import math
import random
from datetime import datetime, timedelta

import discord
import pytz

from db_utils import (
    get_config, set_config,
    get_rename_posts_for_year,
    create_bracket, get_active_bracket,
    create_bracket_entry, get_bracket_entry,
    create_bracket_matchup, update_matchup_posted,
    get_active_round_matchups, set_matchup_winner,
    get_round_winners_ordered, advance_bracket_round, complete_bracket,
)

log = logging.getLogger(__name__)

_POLL_QUESTION_LIMIT = 300
_POLL_ANSWER_LIMIT   = 55  # Discord hard limit for poll answer text


def _guild_tz(cfg) -> pytz.BaseTzInfo:
    try:
        return pytz.timezone(cfg["timezone"] or "US/Eastern")
    except pytz.exceptions.UnknownTimeZoneError:
        return pytz.timezone("US/Eastern")


def _year_utc_range(year: int, tz: pytz.BaseTzInfo) -> tuple[str, str]:
    start = tz.localize(datetime(year, 1, 1)).astimezone(pytz.utc).isoformat()
    end   = tz.localize(datetime(year, 12, 31, 23, 59, 59)).astimezone(pytz.utc).isoformat()
    return start, end


def _round_label(round_num: int, total_rounds: int) -> str:
    remaining = total_rounds - round_num + 1
    if remaining == 1:   return "🏆 The Final"
    if remaining == 2:   return "🔥 Semifinals"
    if remaining == 3:   return "⚔️ Quarterfinals"
    return f"Round of {2 ** remaining}"


def _first_round_pairs(entry_ids: list[int]) -> list[tuple[int, int]]:
    """1 vs N, 2 vs N-1, ... standard tournament seeding."""
    top    = entry_ids[:len(entry_ids) // 2]
    bottom = list(reversed(entry_ids[len(entry_ids) // 2:]))
    return list(zip(top, bottom))


async def _get_total_reactions(client: discord.Client, channel_id: int, message_id: int) -> int:
    """Sum ALL reactions on a message — any emoji counts toward the rename's season score."""
    try:
        channel = client.get_channel(channel_id)
        if not channel:
            return 0
        msg = await channel.fetch_message(message_id)
        return sum(r.count for r in msg.reactions)
    except Exception as e:
        log.warning("Could not fetch reactions (msg %s): %s", message_id, e)
        return 0


async def _get_fresh_image_url(client: discord.Client, channel_id: int, message_id: int) -> str | None:
    """
    Fetch the current attachment URL from a stored rename post message.
    Discord CDN URLs include expiry tokens, so we always re-fetch rather than
    using the stored URL snapshot.  Falls back to None if the message is gone.
    """
    try:
        channel = client.get_channel(channel_id)
        if not channel:
            return None
        msg = await channel.fetch_message(message_id)
        return msg.attachments[0].url if msg.attachments else None
    except Exception:
        return None


async def _get_poll_votes(client: discord.Client, channel_id: int, message_id: int) -> tuple[int, int]:
    """
    Return (votes_a, votes_b) from a Discord poll message.
    poll.results is populated once the poll has ended (which it will be by the
    time our scheduler calls this, since ends_at matches the poll duration).
    """
    try:
        channel = client.get_channel(channel_id)
        if not channel:
            return 0, 0
        msg = await channel.fetch_message(message_id)
        if not msg.poll or not msg.poll.results:
            # Poll may not have fully resolved yet — return zeroes, scheduler
            # will retry next cycle.
            return 0, 0
        votes = {ac.id: ac.count for ac in msg.poll.results.answer_counts}
        return votes.get(1, 0), votes.get(2, 0)
    except Exception as e:
        log.warning("Could not fetch poll results (msg %s): %s", message_id, e)
        return 0, 0


async def _dramatic_coin_flip(channel: discord.TextChannel, name_a: str, name_b: str) -> str:
    """Post a multi-message dramatic coin flip. Returns 'a' or 'b'."""
    await channel.send("🪙 **It's a tie!** Flipping a coin...")
    await asyncio.sleep(2)
    await channel.send("*The coin spins through the air...*")
    await asyncio.sleep(2)
    await channel.send("*It wobbles on the edge...*")
    await asyncio.sleep(1.5)
    winner = random.choice(["a", "b"])
    side   = "Heads" if winner == "a" else "Tails"
    name   = name_a  if winner == "a" else name_b
    await channel.send(f'🎉 **{side}! "{name}" advances!**')
    return winner


async def _post_matchup(
    channel: discord.TextChannel,
    matchup_id: int,
    entry_a, entry_b,
    cfg,
    round_label: str,
    match_num: int,
    total_matches: int,
    ends_at_dt: datetime,
    client: discord.Client,
    rename_posts: list,  # all rename_posts rows for this guild/year, for image lookup
) -> discord.Message | None:
    """
    Post two embeds (one per rename card) then a Discord native poll.
    Returns the poll message (used for vote tallying).
    """
    voting_hours = cfg["bracket_voting_hours"] or 24
    ends_str     = ends_at_dt.strftime("%b %d at %I:%M %p %Z")

    # Find the most recent rename post for each quote to get its image
    async def find_image(quote: str) -> str | None:
        # Find matching rename_posts rows, most recent first
        matches = [p for p in rename_posts if p["quote"] == quote]
        matches.sort(key=lambda p: p["posted_at"], reverse=True)
        for post in matches:
            url = await _get_fresh_image_url(client, post["channel_id"], post["message_id"])
            if url:
                return url
        return None

    img_a, img_b = await asyncio.gather(
        find_image(entry_a["quote"]),
        find_image(entry_b["quote"]),
    )

    # Build embeds — one per option, shown side by side
    color_a = discord.Color.blue()
    color_b = discord.Color.red()

    embed_a = discord.Embed(
        title=f"Option A  ·  #{entry_a['seed']} seed",
        description=(
            f'**"{entry_a["quote"]}"**\n'
            f'*submitted by {entry_a["quote_user"] or "Unknown"}*\n'
            f'{entry_a["season_reactions"]} reactions this season'
        ),
        color=color_a,
    )
    if img_a:
        embed_a.set_image(url=img_a)

    embed_b = discord.Embed(
        title=f"Option B  ·  #{entry_b['seed']} seed",
        description=(
            f'**"{entry_b["quote"]}"**\n'
            f'*submitted by {entry_b["quote_user"] or "Unknown"}*\n'
            f'{entry_b["season_reactions"]} reactions this season'
        ),
        color=color_b,
    )
    if img_b:
        embed_b.set_image(url=img_b)

    header = (
        f"─────────────────────────\n"
        f"🏆 **{round_label}** — Match {match_num + 1} of {total_matches}\n"
        f"Voting closes **{ends_str}**"
    )

    try:
        await channel.send(content=header, embeds=[embed_a, embed_b])
    except discord.HTTPException as e:
        log.error("Failed to post matchup embeds: %s", e)
        return None

    # Discord poll — answers truncated to 55 chars
    answer_a = f"A: {entry_a['quote']}"[:_POLL_ANSWER_LIMIT]
    answer_b = f"B: {entry_b['quote']}"[:_POLL_ANSWER_LIMIT]
    question = f"{round_label} — Match {match_num + 1} of {total_matches}: Which name wins?"[:_POLL_QUESTION_LIMIT]

    poll = discord.Poll(question=question, duration=timedelta(hours=voting_hours), multiple=False)
    poll.add_answer(text=answer_a)
    poll.add_answer(text=answer_b)

    try:
        poll_msg = await channel.send(poll=poll)
        return poll_msg
    except discord.HTTPException as e:
        log.error("Failed to post poll: %s", e)
        return None


# ── Public API ────────────────────────────────────────────────────────────────

async def start_bracket(guild_id: int, client: discord.Client, year: int) -> tuple[bool, str]:
    """Seed and launch a bracket for *year*. Returns (success, admin_message)."""
    cfg = get_config(guild_id)

    if get_active_bracket(guild_id):
        return False, "⚠️ There's already an active bracket. It must finish before starting a new one."

    bracket_channel = client.get_channel(cfg["bracket_channel"])
    if not bracket_channel:
        return False, "⚠️ No bracket channel configured. Run `!setbracketchannel` in your desired channel first."

    bracket_size = cfg["bracket_size"]  or 8
    voting_hours = cfg["bracket_voting_hours"] or 24

    if not math.log2(bracket_size).is_integer():
        return False, f"⚠️ Bracket size must be a power of 2 (4, 8, 16, 32). Currently: {bracket_size}."

    tz                = _guild_tz(cfg)
    year_start, year_end = _year_utc_range(year, tz)
    posts             = get_rename_posts_for_year(guild_id, year_start, year_end, cfg["voting_enabled_at"])

    if not posts:
        return False, (
            f"⚠️ No rename posts found for {year}. "
            f"Make sure voting is enabled (`!enablefeature voting`) and renames have occurred."
        )

    await bracket_channel.send(f"⏳ Tallying reactions from {len(posts)} rename posts for {year}...")

    # Tally total reactions per post (any emoji counts)
    scored: list[tuple[int, str, str | None]] = []
    for post in posts:
        count = await _get_total_reactions(client, post["channel_id"], post["message_id"])
        scored.append((count, post["quote"], post["quote_user"]))

    # Deduplicate quotes — keep highest reaction count per unique quote
    best: dict[str, tuple[int, str | None]] = {}
    for count, quote, user in scored:
        if quote not in best or count > best[quote][0]:
            best[quote] = (count, user)

    ranked = sorted(best.items(), key=lambda x: x[1][0], reverse=True)

    if len(ranked) < 2:
        return False, f"⚠️ Only {len(ranked)} unique rename(s) found — need at least 2."

    # Auto-shrink to largest valid power-of-2 that fits available entries
    actual_size = bracket_size
    while actual_size > len(ranked) and actual_size > 2:
        actual_size //= 2

    if actual_size != bracket_size:
        await bracket_channel.send(
            f"ℹ️ Only {len(ranked)} unique names available — bracket shrunk to {actual_size} (from {bracket_size})."
        )

    nominees = ranked[:actual_size]

    bracket_id = create_bracket(guild_id, year, actual_size, voting_hours)
    entry_ids  = []
    for seed, (quote, (score, user)) in enumerate(nominees, start=1):
        eid = create_bracket_entry(bracket_id, seed, quote, user, score)
        entry_ids.append(eid)

    # Post seeding announcement
    total_rounds = int(math.log2(actual_size))
    lines = [
        f"🏆 **{year} Server Name Championship — {actual_size}-name bracket!**",
        f"Seeded by total reactions · {total_rounds} round{'s' if total_rounds > 1 else ''} · {voting_hours}h per matchup\n",
    ]
    for seed, (quote, (score, user)) in enumerate(nominees, start=1):
        lines.append(f"  **#{seed}** \"{quote}\" — *{user or 'Unknown'}* · {score} reactions")
    await bracket_channel.send("\n".join(lines))

    # Create round 1 matchups and post them
    pairs = _first_round_pairs(entry_ids)
    matchup_rows = []
    for match_num, (a_id, b_id) in enumerate(pairs):
        mid = create_bracket_matchup(bracket_id, 1, match_num, a_id, b_id)
        matchup_rows.append((mid, a_id, b_id))

    await _post_round(bracket_id, 1, matchup_rows, bracket_channel, cfg, tz, client, list(posts))

    return True, f"✅ Bracket started! {actual_size} nominees, {len(pairs)} first-round matchup{'s' if len(pairs) > 1 else ''}."


async def _post_round(
    bracket_id: int,
    round_num: int,
    matchups: list[tuple[int, int, int]],
    channel: discord.TextChannel,
    cfg,
    tz: pytz.BaseTzInfo,
    client: discord.Client,
    rename_posts: list,
) -> None:
    voting_hours = cfg["bracket_voting_hours"] or 24
    actual_size  = 2 ** math.ceil(math.log2(max(len(matchups) * 2, 2)))
    total_rounds = int(math.log2(actual_size))
    label        = _round_label(round_num, total_rounds)
    ends_at_dt   = datetime.now(tz) + timedelta(hours=voting_hours)
    ends_at_utc  = ends_at_dt.astimezone(pytz.utc).isoformat()

    for match_num, (matchup_id, a_id, b_id) in enumerate(matchups):
        entry_a = get_bracket_entry(a_id)
        entry_b = get_bracket_entry(b_id)
        poll_msg = await _post_matchup(
            channel, matchup_id, entry_a, entry_b,
            cfg, label, match_num, len(matchups), ends_at_dt,
            client, rename_posts,
        )
        if poll_msg:
            update_matchup_posted(matchup_id, poll_msg.id, channel.id, ends_at_utc)
        await asyncio.sleep(1.5)  # brief gap between matchup posts


async def check_bracket_advancement(guild_id: int, client: discord.Client) -> None:
    """Called by scheduler every 60 s. Tallies expired polls and advances the bracket."""
    bracket = get_active_bracket(guild_id)
    if not bracket:
        return

    cfg             = get_config(guild_id)
    bracket_channel = client.get_channel(cfg["bracket_channel"])
    if not bracket_channel:
        return

    now_utc   = datetime.now(pytz.utc).isoformat()
    round_num = bracket["current_round"]
    matchups  = get_active_round_matchups(bracket["id"], round_num)

    if not matchups:
        return

    # Only proceed if at least one active matchup has expired
    if not any(m["status"] == "active" and m["ends_at"] and m["ends_at"] <= now_utc for m in matchups):
        return

    all_complete = True
    for m in matchups:
        if m["status"] == "complete":
            continue
        if not m["ends_at"] or m["ends_at"] > now_utc:
            all_complete = False
            continue

        entry_a  = get_bracket_entry(m["entry_a_id"])
        entry_b  = get_bracket_entry(m["entry_b_id"])
        votes_a, votes_b = await _get_poll_votes(client, m["channel_id"], m["message_id"])

        # If poll results aren't available yet, skip and retry next cycle
        if votes_a == 0 and votes_b == 0:
            log.info("[%s] Poll results not yet available for matchup %s — will retry.", guild_id, m["id"])
            all_complete = False
            continue

        if votes_a > votes_b:
            winner_id = m["entry_a_id"]
            await bracket_channel.send(
                f'✅ **"{entry_a["quote"]}"** defeats **"{entry_b["quote"]}"** ({votes_a}–{votes_b}) and advances!'
            )
        elif votes_b > votes_a:
            winner_id = m["entry_b_id"]
            await bracket_channel.send(
                f'✅ **"{entry_b["quote"]}"** defeats **"{entry_a["quote"]}"** ({votes_b}–{votes_a}) and advances!'
            )
        else:
            result    = await _dramatic_coin_flip(bracket_channel, entry_a["quote"], entry_b["quote"])
            winner_id = m["entry_a_id"] if result == "a" else m["entry_b_id"]

        set_matchup_winner(m["id"], winner_id)

    if not all_complete:
        return

    winners = get_round_winners_ordered(bracket["id"], round_num)

    if len(winners) == 1:
        champion = get_bracket_entry(winners[0])
        complete_bracket(bracket["id"])
        await bracket_channel.send(
            f"\n🎊🏆🎊 **{bracket['year']} SERVER NAME CHAMPION** 🎊🏆🎊\n\n"
            f'**"{champion["quote"]}"**\n'
            f"*submitted by {champion['quote_user'] or 'Unknown'} · "
            f"{champion['season_reactions']} reactions in the regular season*\n\n"
            f"Congratulations! 🎉"
        )
        log.info("[%s] Bracket complete. Champion: %s", guild_id, champion["quote"])
        return

    new_round = advance_bracket_round(bracket["id"])
    pairs     = [(winners[i], winners[i + 1]) for i in range(0, len(winners), 2)]
    matchup_rows = []
    for match_num, (a_id, b_id) in enumerate(pairs):
        mid = create_bracket_matchup(bracket["id"], new_round, match_num, a_id, b_id)
        matchup_rows.append((mid, a_id, b_id))

    bracket_size = bracket["size"]
    total_rounds = int(math.log2(bracket_size))
    label        = _round_label(new_round, total_rounds)
    await bracket_channel.send(f"\n⚔️ **{label} begins!**")

    tz           = _guild_tz(cfg)
    # Re-fetch posts for image lookup in next round
    from db_utils import get_rename_posts_for_year
    from datetime import timezone as tz_mod
    year         = bracket["year"]
    year_start, year_end = _year_utc_range(year, tz)
    rename_posts = get_rename_posts_for_year(guild_id, year_start, year_end, cfg["voting_enabled_at"])
    await _post_round(bracket["id"], new_round, matchup_rows, bracket_channel, cfg, tz, client, list(rename_posts))
    log.info("[%s] Advanced to round %d.", guild_id, new_round)


def get_bracket_status_text(guild_id: int) -> str:
    bracket = get_active_bracket(guild_id)
    if not bracket:
        return "No active bracket."
    cfg          = get_config(guild_id)
    total_rounds = int(math.log2(bracket["size"]))
    label        = _round_label(bracket["current_round"], total_rounds)
    matchups     = get_active_round_matchups(bracket["id"], bracket["current_round"])
    done         = sum(1 for m in matchups if m["status"] == "complete")
    return (
        f"**{bracket['year']} Bracket** — {label}\n"
        f"Size: {bracket['size']} · Round {bracket['current_round']}/{total_rounds} · "
        f"{done}/{len(matchups)} matchups complete"
    )
