"""
bracket.py — Yearly rename bracket championship logic.

Flow:
  1. Admin enables voting  (!enablefeature voting)
     → sets voting_enabled_at, bot starts adding vote reactions to rename posts

  2. Admin starts bracket  (!startbracket [year])
     → fetches rename_posts for that year, tallies Discord reactions,
       seeds top-N by score, posts round 1 matchups

  3. Scheduler checks every 60 s  (check_bracket_advancement)
     → when all matchups in current round have expired:
       - tallies each matchup (coin flip on ties)
       - if final: announces champion
       - otherwise: posts next round

Seeding:  standard tournament pairs (1vN, 2v(N-1), ...)
          winner of match i pairs with winner of match i+1 in subsequent rounds.
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


# ── Helpers ───────────────────────────────────────────────────────────────────

def _guild_tz(cfg) -> pytz.BaseTzInfo:
    try:
        return pytz.timezone(cfg["timezone"] or "US/Eastern")
    except pytz.exceptions.UnknownTimeZoneError:
        return pytz.timezone("US/Eastern")


def _year_utc_range(year: int, tz: pytz.BaseTzInfo) -> tuple[str, str]:
    """ISO UTC timestamps for Jan 1 00:00 → Dec 31 23:59:59 in tz."""
    start = tz.localize(datetime(year, 1, 1, 0, 0, 0)).astimezone(pytz.utc).isoformat()
    end   = tz.localize(datetime(year, 12, 31, 23, 59, 59)).astimezone(pytz.utc).isoformat()
    return start, end


def _round_label(round_num: int, total_rounds: int) -> str:
    remaining = total_rounds - round_num + 1
    if remaining == 1:
        return "🏆 The Final"
    elif remaining == 2:
        return "🔥 Semifinals"
    elif remaining == 3:
        return "⚔️ Quarterfinals"
    else:
        return f"Round of {2 ** remaining}"


def _first_round_pairs(entry_ids: list[int]) -> list[tuple[int, int]]:
    """
    Standard tournament seeding: top seed vs bottom seed, working inward.
    [1,2,3,4,5,6,7,8] → [(1,8), (2,7), (3,6), (4,5)]
    """
    top    = entry_ids[:len(entry_ids) // 2]
    bottom = list(reversed(entry_ids[len(entry_ids) // 2:]))
    return list(zip(top, bottom))


async def validate_emoji(message: discord.Message, emoji_str: str) -> bool:
    """
    Try to react with emoji_str on message.
    Returns True if the bot can use the emoji, False otherwise.
    Custom server emojis require the bot to be in the emoji's home server.
    """
    try:
        await message.add_reaction(emoji_str)
        await message.remove_reaction(emoji_str, message.guild.me)
        return True
    except (discord.HTTPException, discord.InvalidArgument):
        return False


async def _get_reaction_count(client: discord.Client, channel_id: int, message_id: int, emoji: str) -> int:
    """Fetch a Discord message and count reactions for emoji. Subtracts the bot's own reaction."""
    try:
        channel = client.get_channel(channel_id)
        if not channel:
            return 0
        msg = await channel.fetch_message(message_id)
        for reaction in msg.reactions:
            if str(reaction.emoji) == emoji:
                return max(0, reaction.count - 1)  # bot added its own reaction
        return 0
    except Exception as e:
        log.warning("Could not fetch reaction count (msg %s): %s", message_id, e)
        return 0


async def _dramatic_coin_flip(channel: discord.TextChannel, name_a: str, name_b: str) -> str:
    """Post a dramatic coin-flip sequence and return 'a' or 'b'."""
    await channel.send("🪙 **It's a tie!** Flipping a coin...")
    await asyncio.sleep(2)
    await channel.send("*The coin spins through the air...*")
    await asyncio.sleep(2)
    await channel.send("*It wobbles on the edge...*")
    await asyncio.sleep(1.5)
    winner = random.choice(["a", "b"])
    side = "Heads" if winner == "a" else "Tails"
    name = name_a if winner == "a" else name_b
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
) -> discord.Message | None:
    """Post a single bracket matchup and return the sent message."""
    emoji_a   = cfg["bracket_emoji_a"] or "1️⃣"
    emoji_b   = cfg["bracket_emoji_b"] or "2️⃣"
    hours     = cfg["bracket_voting_hours"] or 24
    ends_str  = ends_at_dt.strftime("%b %d at %I:%M %p %Z")

    text = (
        f"─────────────────────────\n"
        f"🏆 **{round_label}** — Match {match_num + 1} of {total_matches}\n"
        f"Voting closes **{ends_str}** ({hours}h)\n\n"
        f"{emoji_a} **(#{entry_a['seed']} seed)** \"{entry_a['quote']}\"\n"
        f"*— submitted by {entry_a['quote_user'] or 'Unknown'}*"
        f"   ·   *{entry_a['season_reactions']} 👍 this season*\n\n"
        f"**VS**\n\n"
        f"{emoji_b} **(#{entry_b['seed']} seed)** \"{entry_b['quote']}\"\n"
        f"*— submitted by {entry_b['quote_user'] or 'Unknown'}*"
        f"   ·   *{entry_b['season_reactions']} 👍 this season*\n"
        f"─────────────────────────"
    )
    try:
        msg = await channel.send(text)
        await msg.add_reaction(emoji_a)
        await msg.add_reaction(emoji_b)
        return msg
    except discord.HTTPException as e:
        log.error("Failed to post matchup: %s", e)
        return None


# ── Core public functions ─────────────────────────────────────────────────────

async def start_bracket(guild_id: int, client: discord.Client, year: int) -> tuple[bool, str]:
    """
    Seed and launch a bracket for *year*.
    Returns (success, message_for_admin).
    """
    cfg = get_config(guild_id)

    if get_active_bracket(guild_id):
        return False, "⚠️ There's already an active bracket for this server. It must finish before starting a new one."

    bracket_channel = client.get_channel(cfg["bracket_channel"])
    if not bracket_channel:
        return False, "⚠️ No bracket channel configured. Use `!setbracketchannel` in your desired channel first."

    bracket_size  = cfg["bracket_size"]  or 8
    voting_hours  = cfg["bracket_voting_hours"] or 24
    vote_emoji    = cfg["vote_emoji"]    or "👍"

    if not math.log2(bracket_size).is_integer():
        return False, f"⚠️ Bracket size must be a power of 2 (4, 8, 16, 32). Currently set to {bracket_size}."

    # Fetch rename posts for the year
    tz = _guild_tz(cfg)
    year_start, year_end = _year_utc_range(year, tz)
    posts = get_rename_posts_for_year(guild_id, year_start, year_end, cfg["voting_enabled_at"])

    if not posts:
        return False, f"⚠️ No rename posts found for {year} (voting must be enabled and renames must have occurred)."

    # Tally reaction scores for each post
    await bracket_channel.send(f"⏳ Tallying votes from {len(posts)} rename posts for {year}...")

    scored: list[tuple[int, str, str | None]] = []  # (score, quote, quote_user)
    for post in posts:
        score = await _get_reaction_count(client, post["channel_id"], post["message_id"], vote_emoji)
        scored.append((score, post["quote"], post["quote_user"]))

    # Deduplicate quotes, keeping highest score per quote
    best: dict[str, tuple[int, str | None]] = {}
    for score, quote, user in scored:
        if quote not in best or score > best[quote][0]:
            best[quote] = (score, user)

    ranked = sorted(best.items(), key=lambda x: x[1][0], reverse=True)

    if len(ranked) < 2:
        return False, f"⚠️ Only {len(ranked)} unique rename(s) found — need at least 2 to run a bracket."

    # Auto-shrink bracket size if not enough entries
    actual_size = bracket_size
    while actual_size > len(ranked) and actual_size > 2:
        actual_size //= 2

    if actual_size != bracket_size:
        await bracket_channel.send(
            f"ℹ️ Only {len(ranked)} unique names available — shrinking bracket to {actual_size} (from {bracket_size})."
        )

    nominees = ranked[:actual_size]

    # Create bracket in DB
    bracket_id = create_bracket(guild_id, year, actual_size, voting_hours)
    entry_ids  = []
    for seed, (quote, (score, user)) in enumerate(nominees, start=1):
        eid = create_bracket_entry(bracket_id, seed, quote, user, score)
        entry_ids.append(eid)

    # Post bracket announcement
    total_rounds = int(math.log2(actual_size))
    lines = [f"🏆 **{year} Server Name Championship — {actual_size}-name bracket!**"]
    lines.append(f"Seeded by reaction score | {total_rounds} round{'s' if total_rounds > 1 else ''} | {voting_hours}h per matchup\n")
    for seed, (quote, (score, user)) in enumerate(nominees, start=1):
        lines.append(f"  **#{seed}** \"{quote}\" — *{user or 'Unknown'}* · {score} 👍")
    await bracket_channel.send("\n".join(lines))

    # Create and post round 1 matchups
    pairs      = _first_round_pairs(entry_ids)
    matchup_ids = []
    for match_num, (a_id, b_id) in enumerate(pairs):
        mid = create_bracket_matchup(bracket_id, 1, match_num, a_id, b_id)
        matchup_ids.append((mid, a_id, b_id))

    await _post_round(bracket_id, 1, matchup_ids, bracket_channel, cfg, tz)

    return True, f"✅ Bracket started! {actual_size} nominees, {len(pairs)} first-round matchup{'s' if len(pairs) > 1 else ''}."


async def _post_round(
    bracket_id: int,
    round_num: int,
    matchups: list[tuple[int, int, int]],  # (matchup_id, entry_a_id, entry_b_id)
    channel: discord.TextChannel,
    cfg,
    tz: pytz.BaseTzInfo,
) -> None:
    """Post all matchups for a round and record their message IDs."""
    voting_hours  = cfg["bracket_voting_hours"] or 24
    bracket_size  = cfg["bracket_size"] or 8
    # Recalculate based on actual entries in this bracket
    # Determine total rounds from bracket
    actual_size   = 2 ** math.ceil(math.log2(len(matchups) * 2))
    total_rounds  = int(math.log2(max(actual_size, 2)))
    label         = _round_label(round_num, total_rounds)
    ends_at_dt    = datetime.now(tz) + timedelta(hours=voting_hours)
    ends_at_utc   = ends_at_dt.astimezone(pytz.utc).isoformat()

    for match_num, (matchup_id, a_id, b_id) in enumerate(matchups):
        entry_a = get_bracket_entry(a_id)
        entry_b = get_bracket_entry(b_id)
        msg = await _post_matchup(
            channel, matchup_id, entry_a, entry_b,
            cfg, label, match_num, len(matchups), ends_at_dt,
        )
        if msg:
            update_matchup_posted(matchup_id, msg.id, channel.id, ends_at_utc)
        await asyncio.sleep(1)  # small gap between matchup posts


async def check_bracket_advancement(guild_id: int, client: discord.Client) -> None:
    """
    Called by the scheduler every 60 s.
    Tallies any expired matchups and advances the bracket if the round is complete.
    """
    bracket = get_active_bracket(guild_id)
    if not bracket:
        return

    cfg             = get_config(guild_id)
    bracket_channel = client.get_channel(cfg["bracket_channel"])
    if not bracket_channel:
        return

    tz          = _guild_tz(cfg)
    now_utc     = datetime.now(pytz.utc).isoformat()
    round_num   = bracket["current_round"]
    matchups    = get_active_round_matchups(bracket["id"], round_num)

    if not matchups:
        return

    # Check if any matchups have expired and need tallying
    any_pending = False
    for m in matchups:
        if m["status"] == "active" and m["ends_at"] and m["ends_at"] <= now_utc:
            any_pending = True
            break

    if not any_pending:
        return

    # Tally all expired matchups in this round
    emoji_a = cfg["bracket_emoji_a"] or "1️⃣"
    emoji_b = cfg["bracket_emoji_b"] or "2️⃣"

    all_complete = True
    for m in matchups:
        if m["status"] == "complete":
            continue
        if not m["ends_at"] or m["ends_at"] > now_utc:
            all_complete = False
            continue

        # This matchup has expired — tally it
        entry_a = get_bracket_entry(m["entry_a_id"])
        entry_b = get_bracket_entry(m["entry_b_id"])

        votes_a = await _get_reaction_count(client, m["channel_id"], m["message_id"], emoji_a)
        votes_b = await _get_reaction_count(client, m["channel_id"], m["message_id"], emoji_b)

        if votes_a > votes_b:
            winner_id   = m["entry_a_id"]
            winner_name = entry_a["quote"]
            await bracket_channel.send(
                f'✅ **"{entry_a["quote"]}"** defeats **"{entry_b["quote"]}"** '
                f'({votes_a}–{votes_b}) and advances!'
            )
        elif votes_b > votes_a:
            winner_id   = m["entry_b_id"]
            winner_name = entry_b["quote"]
            await bracket_channel.send(
                f'✅ **"{entry_b["quote"]}"** defeats **"{entry_a["quote"]}"** '
                f'({votes_b}–{votes_a}) and advances!'
            )
        else:
            # Tie — dramatic coin flip
            result = await _dramatic_coin_flip(bracket_channel, entry_a["quote"], entry_b["quote"])
            winner_id   = m["entry_a_id"] if result == "a" else m["entry_b_id"]
            winner_name = entry_a["quote"] if result == "a" else entry_b["quote"]

        set_matchup_winner(m["id"], winner_id)

    if not all_complete:
        return

    # All matchups in the round are done — check if this was the final
    winners = get_round_winners_ordered(bracket["id"], round_num)

    if len(winners) == 1:
        # Champion!
        champion = get_bracket_entry(winners[0])
        complete_bracket(bracket["id"])
        await bracket_channel.send(
            f"\n🎊🏆🎊 **{bracket['year']} SERVER NAME CHAMPION** 🎊🏆🎊\n\n"
            f'**"{champion["quote"]}"**\n'
            f"*submitted by {champion['quote_user'] or 'Unknown'} · "
            f"{champion['season_reactions']} 👍 in the regular season*\n\n"
            f"Congratulations! 🎉"
        )
        log.info("[%s] Bracket complete. Champion: %s", guild_id, champion["quote"])
        return

    # Advance to next round
    new_round   = advance_bracket_round(bracket["id"])
    pairs       = [(winners[i], winners[i + 1]) for i in range(0, len(winners), 2)]
    matchup_ids = []
    for match_num, (a_id, b_id) in enumerate(pairs):
        mid = create_bracket_matchup(bracket["id"], new_round, match_num, a_id, b_id)
        matchup_ids.append((mid, a_id, b_id))

    bracket_size = bracket["size"]
    total_rounds = int(math.log2(bracket_size))
    label        = _round_label(new_round, total_rounds)
    await bracket_channel.send(f"\n⚔️ **{label} begins!** Matchups below:")

    tz = _guild_tz(cfg)
    await _post_round(bracket["id"], new_round, matchup_ids, bracket_channel, cfg, tz)
    log.info("[%s] Advanced to round %d.", guild_id, new_round)


def get_bracket_status_text(guild_id: int) -> str:
    bracket = get_active_bracket(guild_id)
    if not bracket:
        return "No active bracket."
    cfg   = get_config(guild_id)
    size  = bracket["size"]
    total = int(math.log2(size))
    label = _round_label(bracket["current_round"], total)
    matchups = get_active_round_matchups(bracket["id"], bracket["current_round"])
    done  = sum(1 for m in matchups if m["status"] == "complete")
    return (
        f"**{bracket['year']} Bracket** — {label}\n"
        f"Size: {size} | Round {bracket['current_round']}/{total} | "
        f"{done}/{len(matchups)} matchups complete"
    )
