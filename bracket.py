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
    get_config, set_config, cancel_bracket,
    get_rename_posts_in_range,
    create_bracket, get_active_bracket,
    create_bracket_entry, get_bracket_entry,
    create_bracket_matchup, update_matchup_posted,
    get_active_round_matchups, set_matchup_winner,
    get_round_winners_ordered, advance_bracket_round, complete_bracket,
    get_season,
)

log = logging.getLogger(__name__)

_POLL_QUESTION_LIMIT = 300
_POLL_ANSWER_LIMIT   = 55  # Discord hard limit for poll answer text

# Discord allows max 10 embeds and 10 files per message; 4 matchups = 8 of each.
_MATCHUPS_PER_CARD_MESSAGE = 4

# Test-bracket card images, generated in memory by start_test_bracket.
# Module-level so the bytes survive across rounds (round 2+ is posted from a
# fresh get_config() in force_bracket_advance / check_bracket_advancement).
# bracket_id -> {entry_id -> card PNG bytes}
_test_card_cache: dict[int, dict[int, bytes]] = {}


def _bracket_pacing(cfg) -> str:
    """Return 'daily' or 'round' (the default) for a config row/dict."""
    try:
        val = cfg["bracket_pacing"]
    except (IndexError, KeyError):
        val = None
    return "daily" if val == "daily" else "round"


def _guild_tz(cfg) -> pytz.BaseTzInfo:
    try:
        return pytz.timezone(cfg["timezone"] or "US/Eastern")
    except pytz.exceptions.UnknownTimeZoneError:
        return pytz.timezone("US/Eastern")


def _rename_posts_for_bracket(guild_id: int, bracket, cfg, tz: pytz.BaseTzInfo) -> list:
    """
    Rename posts within a bracket's seeding window, used to re-fetch card images
    in later rounds.  Uses the range persisted on the bracket (range_start/end);
    falls back to the calendar year for legacy brackets that predate those
    columns.  Empty for test brackets (year==0, no range).
    """
    start = _bracket_col(bracket, "range_start")
    end   = _bracket_col(bracket, "range_end")
    if start and end:
        return list(get_rename_posts_in_range(guild_id, start, end, cfg["voting_enabled_at"]))
    year = bracket["year"]
    if year <= 0:
        return []
    year_start, year_end = _year_utc_range(year, tz)
    return list(get_rename_posts_in_range(guild_id, year_start, year_end, cfg["voting_enabled_at"]))


def _bracket_col(bracket, col: str):
    """Safely read a possibly-absent bracket column (legacy rows / old Row objects)."""
    try:
        return bracket[col]
    except (IndexError, KeyError):
        return None


def _bracket_label(bracket) -> str:
    """Display label for a bracket: explicit label, else the year, else 'Bracket'."""
    label = _bracket_col(bracket, "label")
    if label:
        return label
    year = bracket["year"]
    return "TEST" if year == 0 else str(year)


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
    Uses answer.vote_count directly — poll.results is not available in discord.py 2.7.1.
    Answers are ordered by insertion order (A=index 0, B=index 1).
    """
    try:
        channel = client.get_channel(channel_id)
        if not channel:
            return 0, 0
        msg = await channel.fetch_message(message_id)
        if not msg.poll:
            return 0, 0
        answers = msg.poll.answers
        if len(answers) < 2:
            return 0, 0
        return answers[0].vote_count, answers[1].vote_count
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


async def _get_card_bytes(
    entry,
    bracket_id: int,
    client: discord.Client,
    rename_posts: list,
) -> bytes | None:
    """
    Card image bytes for a bracket entry.
    Test brackets serve from _test_card_cache (populated by start_test_bracket).
    Real brackets find the most recent matching rename post and download the
    attachment fresh (CDN URLs expire, so the stored snapshot is never used).
    """
    import aiohttp

    cached = _test_card_cache.get(bracket_id, {})
    if entry["id"] in cached:
        return cached[entry["id"]]

    _TIMEOUT = aiohttp.ClientTimeout(total=15)
    matches = [p for p in rename_posts if p["quote"] == entry["quote"]]
    matches.sort(key=lambda p: p["posted_at"], reverse=True)
    for post in matches:
        url = await _get_fresh_image_url(client, post["channel_id"], post["message_id"])
        if not url:
            continue
        try:
            async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
                async with session.get(url) as resp:
                    resp.raise_for_status()
                    return await resp.read()
        except Exception as e:
            log.warning("Could not download card image: %s", e)
    return None


def _build_matchup_embeds(match_num: int, entry_a, entry_b, bytes_a, bytes_b):
    """
    Build the (embeds, files) for one matchup's two cards, labeled
    "Match N — Option A/B".  Images are attached via the attachment:// scheme
    with per-matchup filenames so several matchups can share one message.
    """
    from io import BytesIO

    embeds, files = [], []
    for side, entry, card, color in (
        ("A", entry_a, bytes_a, discord.Color.blue()),
        ("B", entry_b, bytes_b, discord.Color.red()),
    ):
        embed = discord.Embed(
            title=f"Match {match_num + 1} — Option {side}  ·  #{entry['seed']} seed",
            description=(
                f'**"{entry["quote"]}"**\n'
                f'*submitted by {entry["quote_user"] or "Unknown"}*\n'
                f'{entry["season_reactions"]} reactions this season'
            ),
            color=color,
        )
        if card:
            filename = f"card_m{match_num + 1}{side.lower()}.png"
            files.append(discord.File(BytesIO(card), filename=filename))
            embed.set_image(url=f"attachment://{filename}")
        embeds.append(embed)
    return embeds, files


async def _post_daily_matchup(
    bracket_id: int,
    matchup_id: int,
    entry_a_id: int,
    entry_b_id: int,
    match_num: int,
    total_matches: int,
    channel: discord.TextChannel,
    cfg,
    tz: pytz.BaseTzInfo,
    client: discord.Client,
    rename_posts: list,
    round_label: str,
) -> None:
    """
    Post a single matchup (its two cards, then its poll) for daily pacing and
    record the poll message.  Computes a fresh voting window from now.
    """
    voting_hours = cfg["bracket_voting_hours"] or 24
    ends_at_dt   = datetime.now(tz) + timedelta(hours=voting_hours)
    ends_at_utc  = ends_at_dt.astimezone(pytz.utc).isoformat()
    ends_str     = ends_at_dt.strftime("%b %d at %I:%M %p %Z")

    entry_a = get_bracket_entry(entry_a_id)
    entry_b = get_bracket_entry(entry_b_id)
    bytes_a, bytes_b = await asyncio.gather(
        _get_card_bytes(entry_a, bracket_id, client, rename_posts),
        _get_card_bytes(entry_b, bracket_id, client, rename_posts),
    )
    embeds, files = _build_matchup_embeds(match_num, entry_a, entry_b, bytes_a, bytes_b)

    header = (
        f"─────────────────────────\n"
        f"🏆 **{round_label}** — Match {match_num + 1} of {total_matches}\n"
        f"Voting closes **{ends_str}** — poll below!"
    )
    try:
        await channel.send(content=header, embeds=embeds, files=files)
    except discord.HTTPException as e:
        log.error("Failed to post daily matchup cards: %s", e)
    await asyncio.sleep(1.0)

    poll_msg = await _post_matchup(
        channel, matchup_id, entry_a, entry_b,
        round_label, match_num, total_matches, voting_hours,
    )
    if poll_msg:
        update_matchup_posted(matchup_id, poll_msg.id, channel.id, ends_at_utc)


async def _post_matchup(
    channel: discord.TextChannel,
    matchup_id: int,
    entry_a, entry_b,
    round_label: str,
    match_num: int,
    total_matches: int,
    voting_hours: int,
) -> discord.Message | None:
    """
    Post the Discord native poll for one matchup.  The matchup cards are shown
    in the round's combined card message (see _post_round), so this is poll-only.
    Returns the poll message (its ID is recorded for vote tallying).
    """
    answer_a = f"A: {entry_a['quote']}"[:_POLL_ANSWER_LIMIT]
    answer_b = f"B: {entry_b['quote']}"[:_POLL_ANSWER_LIMIT]
    question = f"{round_label} — Match {match_num + 1} of {total_matches}: Which name wins?"[:_POLL_QUESTION_LIMIT]

    poll = discord.Poll(question=question, duration=timedelta(hours=voting_hours), multiple=False)
    poll.add_answer(text=answer_a)
    poll.add_answer(text=answer_b)

    try:
        return await channel.send(poll=poll)
    except discord.HTTPException as e:
        log.error("Failed to post poll for matchup %s: %s", matchup_id, e)
        return None


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
    """
    Post a round's matchups.

    'round' pacing (default): one combined card message (all matchup cards as
    embeds, batched to respect Discord's 10-embed/10-file limits) followed by
    one poll message per matchup.

    'daily' pacing: only the first matchup is posted now (cards + poll); the
    remaining matchup rows already exist in the DB and are posted one at a time
    by the advancement scheduler as each poll closes.

    Poll message IDs and end times are recorded for the advancement scheduler.
    """
    voting_hours = cfg["bracket_voting_hours"] or 24
    actual_size  = 2 ** math.ceil(math.log2(max(len(matchups) * 2, 2)))
    total_rounds = int(math.log2(actual_size))
    label        = _round_label(round_num, total_rounds)

    # ── Daily pacing: post only the first matchup ─────────────────────────────
    if _bracket_pacing(cfg) == "daily":
        first_mid, first_a, first_b = matchups[0]
        await _post_daily_matchup(
            bracket_id, first_mid, first_a, first_b, 0, len(matchups),
            channel, cfg, tz, client, rename_posts, label,
        )
        if len(matchups) > 1:
            await channel.send(
                f"🗓️ **Daily pacing** — Match 1 of {len(matchups)} is up now. "
                f"The next matchup posts automatically when this poll closes."
            )
        return

    # ── Round pacing: combined cards + all polls at once ──────────────────────
    ends_at_dt   = datetime.now(tz) + timedelta(hours=voting_hours)
    ends_at_utc  = ends_at_dt.astimezone(pytz.utc).isoformat()
    ends_str     = ends_at_dt.strftime("%b %d at %I:%M %p %Z")

    # Collect entries and card bytes for every matchup before posting anything.
    prepared = []  # (match_num, matchup_id, entry_a, entry_b, bytes_a, bytes_b)
    for match_num, (matchup_id, a_id, b_id) in enumerate(matchups):
        entry_a = get_bracket_entry(a_id)
        entry_b = get_bracket_entry(b_id)
        bytes_a, bytes_b = await asyncio.gather(
            _get_card_bytes(entry_a, bracket_id, client, rename_posts),
            _get_card_bytes(entry_b, bracket_id, client, rename_posts),
        )
        prepared.append((match_num, matchup_id, entry_a, entry_b, bytes_a, bytes_b))

    # Combined card message(s): every matchup's cards as labeled embeds, with
    # the images attached to the same message via the attachment:// scheme.
    for batch_start in range(0, len(prepared), _MATCHUPS_PER_CARD_MESSAGE):
        batch  = prepared[batch_start:batch_start + _MATCHUPS_PER_CARD_MESSAGE]
        embeds = []
        files  = []
        for match_num, _mid, entry_a, entry_b, bytes_a, bytes_b in batch:
            m_embeds, m_files = _build_matchup_embeds(match_num, entry_a, entry_b, bytes_a, bytes_b)
            embeds.extend(m_embeds)
            files.extend(m_files)

        if batch_start == 0:
            header = (
                f"─────────────────────────\n"
                f"🏆 **{label}** — {len(matchups)} matchup(s)\n"
                f"Voting closes **{ends_str}** — polls below!"
            )
        else:
            header = f"🏆 **{label}** *(continued)*"

        try:
            await channel.send(content=header, embeds=embeds, files=files)
        except discord.HTTPException as e:
            log.error("Failed to post round card message: %s", e)
        await asyncio.sleep(1.5)

    # One poll per matchup, in match order.
    for match_num, matchup_id, entry_a, entry_b, _ba, _bb in prepared:
        poll_msg = await _post_matchup(
            channel, matchup_id, entry_a, entry_b,
            label, match_num, len(matchups), voting_hours,
        )
        if poll_msg:
            update_matchup_posted(matchup_id, poll_msg.id, channel.id, ends_at_utc)
        await asyncio.sleep(1.5)


async def start_test_bracket(guild_id: int, client: discord.Client) -> tuple[bool, str]:
    """
    Seed a bracket from the quote channel using reservoir sampling (one quote
    per contributor) with random reaction scores.  Runs the full real bracket
    flow — polls, embeds, advancement — so every part of the system can be
    verified without a year of real voting history.
    Year stored as 0 to distinguish test brackets from real ones.
    """
    cfg = get_config(guild_id)

    if get_active_bracket(guild_id):
        return False, "⚠️ There's already an active bracket. Run `!cancelbracket` first if it's a test."

    bracket_channel = client.get_channel(cfg["bracket_channel"])
    if not bracket_channel:
        return False, "⚠️ No bracket channel configured. Run `!setbracketchannel` first."

    quote_channel = client.get_channel(cfg["quote_channel"])
    if not quote_channel:
        return False, "⚠️ No quote channel configured."

    bracket_size = cfg["bracket_size"]  or 8
    voting_hours = cfg["bracket_voting_hours"] or 24

    await bracket_channel.send("⏳ Scanning quote channel to build test bracket...")

    # One quote per contributor via reservoir sampling
    pool: dict[int, tuple[str, str, int]] = {}
    async for msg in quote_channel.history(limit=None, oldest_first=False):
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

    if len(pool) < 2:
        return False, f"⚠️ Only {len(pool)} contributor(s) in quote channel — need at least 2."

    # Random scores simulate a real season of voting
    scored = sorted(
        [(random.randint(1, 50), quote, name) for _, (quote, name, _) in pool.items()],
        reverse=True,
    )

    actual_size = bracket_size
    while actual_size > len(scored) and actual_size > 2:
        actual_size //= 2

    if actual_size != bracket_size:
        await bracket_channel.send(
            f"ℹ️ Only {len(scored)} contributors — shrinking bracket to {actual_size} (from {bracket_size})."
        )

    nominees = scored[:actual_size]
    total_rounds = int(math.log2(actual_size))

    bracket_id = create_bracket(guild_id, 0, actual_size, voting_hours, label="TEST")
    entry_ids  = []
    for seed, (score, quote, user) in enumerate(nominees, start=1):
        eid = create_bracket_entry(bracket_id, seed, quote, user, score)
        entry_ids.append(eid)

    lines = [
        f"🧪 **TEST BRACKET — {actual_size} names** *(random scores, not a real bracket)*",
        f"{total_rounds} round(s) · {voting_hours}h per matchup · use `!forcebracketadvance` to skip the wait\n",
    ]
    for seed, (score, quote, user) in enumerate(nominees, start=1):
        lines.append(f'  **#{seed}** "{quote}" — *{user}* · {score} reactions (random)')
    await bracket_channel.send("\n".join(lines))

    # Generate card images for each nominee upfront and store the bytes in
    # _test_card_cache so _get_card_bytes can serve them in every round —
    # including round 2+, which is posted from a fresh get_config().
    await bracket_channel.send("⏳ Generating test cards...")
    card_bytes_map: dict[int, bytes] = {}

    icon_channel = client.get_channel(cfg["icon_channel"])

    async def _make_test_card(entry_id: int, quote: str, user: str) -> None:
        from bot_features import get_random_icon
        from image_utils import generate_card
        import aiohttp
        _TIMEOUT = aiohttp.ClientTimeout(total=15)

        icon_url, icon_user, _ = await get_random_icon(icon_channel)
        if not icon_url:
            log.warning("Test card: no icon available for entry %s", entry_id)
            return
        try:
            async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
                async with session.get(icon_url) as resp:
                    resp.raise_for_status()
                    icon_bytes = await resp.read()
        except Exception as e:
            log.warning("Test card: failed to download icon: %s", e)
            return
        card = await generate_card(quote, user, icon_user or "Unknown", icon_bytes)
        if card:
            card_bytes_map[entry_id] = card.read()

    for eid, (score, quote, user) in zip(entry_ids, nominees):
        await _make_test_card(eid, quote, user)

    _test_card_cache[bracket_id] = card_bytes_map

    pairs = _first_round_pairs(entry_ids)
    matchup_rows = []
    for match_num, (a_id, b_id) in enumerate(pairs):
        mid = create_bracket_matchup(bracket_id, 1, match_num, a_id, b_id)
        matchup_rows.append((mid, a_id, b_id))

    tz = _guild_tz(cfg)
    await _post_round(bracket_id, 1, matchup_rows, bracket_channel, cfg, tz, client, [])

    return True, (
        "✅ Test bracket started!\n"
        "Vote in the polls, then use `!forcebracketadvance` to skip the wait.\n"
        "Run `!cancelbracket` when done to clean up."
    )


async def force_bracket_advance(guild_id: int, client: discord.Client) -> tuple[bool, str]:
    """
    Immediately end all active polls in the current round and advance.
    Uses Message.end_poll() so real vote counts are preserved.
    Falls back to a coin flip for matchups with no votes.
    Works on both real and test brackets.
    """
    bracket = get_active_bracket(guild_id)
    if not bracket:
        return False, "⚠️ No active bracket."

    cfg             = get_config(guild_id)
    bracket_channel = client.get_channel(cfg["bracket_channel"])
    if not bracket_channel:
        return False, "⚠️ Bracket channel not found."

    round_num = bracket["current_round"]
    pacing    = _bracket_pacing(cfg)
    matchups  = get_active_round_matchups(bracket["id"], round_num)

    # Daily pacing posts one matchup at a time, so only the matchup currently
    # showing a poll (message_id set) can be resolved from votes.  Round pacing
    # resolves every active matchup.
    if pacing == "daily":
        pending = [m for m in matchups if m["status"] == "active" and m["message_id"]]
    else:
        pending = [m for m in matchups if m["status"] == "active"]

    if not pending:
        return False, "⚠️ No active matchups in the current round."

    await bracket_channel.send(f"⚡ Force-advancing round {round_num}...")

    for m in pending:
        entry_a = get_bracket_entry(m["entry_a_id"])
        entry_b = get_bracket_entry(m["entry_b_id"])
        votes_a = votes_b = 0

        try:
            ch = client.get_channel(m["channel_id"])
            if ch:
                msg = await ch.fetch_message(m["message_id"])
                if msg.poll:
                    try:
                        # end_poll() ends immediately and returns updated message
                        msg = await msg.end_poll()
                    except discord.HTTPException:
                        # Already ended — re-fetch
                        msg = await ch.fetch_message(m["message_id"])
                    # Use answer.vote_count — poll.results not available in 2.7.1
                    if msg.poll and len(msg.poll.answers) >= 2:
                        votes_a = msg.poll.answers[0].vote_count
                        votes_b = msg.poll.answers[1].vote_count
                        log.info(
                            "[%s] Poll %s results: A=%d B=%d",
                            guild_id, m["message_id"], votes_a, votes_b,
                        )
        except Exception as e:
            log.warning("[%s] Could not end poll for matchup %s: %s", guild_id, m["id"], e)

        if votes_a > votes_b:
            winner_id = m["entry_a_id"]
            await bracket_channel.send(
                f'✅ **"{entry_a["quote"]}"** wins ({votes_a}–{votes_b}) and advances!'
            )
        elif votes_b > votes_a:
            winner_id = m["entry_b_id"]
            await bracket_channel.send(
                f'✅ **"{entry_b["quote"]}"** wins ({votes_b}–{votes_a}) and advances!'
            )
        else:
            result    = await _dramatic_coin_flip(bracket_channel, entry_a["quote"], entry_b["quote"])
            winner_id = m["entry_a_id"] if result == "a" else m["entry_b_id"]

        set_matchup_winner(m["id"], winner_id)

    tz = _guild_tz(cfg)

    # Daily pacing: if matchups in this round remain unposted, post the next one
    # instead of advancing — force-advance just skips the current poll's wait.
    if pacing == "daily":
        fresh    = get_active_round_matchups(bracket["id"], round_num)
        unposted = [m for m in fresh if not m["message_id"]]
        if unposted:
            rename_posts = _rename_posts_for_bracket(guild_id, bracket, cfg, tz)
            total_rounds = int(math.log2(bracket["size"]))
            label        = _round_label(round_num, total_rounds)
            nxt          = unposted[0]
            await _post_daily_matchup(
                bracket["id"], nxt["id"], nxt["entry_a_id"], nxt["entry_b_id"],
                nxt["match_num"], len(fresh),
                bracket_channel, cfg, tz, client, rename_posts, label,
            )
            return True, f"✅ Resolved matchup — posted Match {nxt['match_num'] + 1} of {len(fresh)}."

    winners = get_round_winners_ordered(bracket["id"], round_num)

    if len(winners) == 1:
        champion = get_bracket_entry(winners[0])
        complete_bracket(bracket["id"])
        _test_card_cache.pop(bracket["id"], None)
        await bracket_channel.send(
            f"\n🎊🏆🎊 **CHAMPION** 🎊🏆🎊\n\n"
            f'**"{champion["quote"]}"**\n'
            f'*submitted by {champion["quote_user"] or "Unknown"} · '
            f'{champion["season_reactions"]} reactions this season*\n\n'
            f"Congratulations! 🎉"
        )
        return True, "🏆 Bracket complete!"

    new_round    = advance_bracket_round(bracket["id"])
    pairs        = [(winners[i], winners[i + 1]) for i in range(0, len(winners), 2)]
    matchup_rows = []
    for match_num, (a_id, b_id) in enumerate(pairs):
        mid = create_bracket_matchup(bracket["id"], new_round, match_num, a_id, b_id)
        matchup_rows.append((mid, a_id, b_id))

    bracket_size = bracket["size"]
    total_rounds = int(math.log2(bracket_size))
    label        = _round_label(new_round, total_rounds)
    await bracket_channel.send(f"\n⚔️ **{label} begins!**")

    rename_posts = _rename_posts_for_bracket(guild_id, bracket, cfg, tz)
    await _post_round(bracket["id"], new_round, matchup_rows, bracket_channel, cfg, tz, client, rename_posts)
    return True, f"✅ Advanced to round {new_round}."


# ── Core public functions ─────────────────────────────────────────────────────

async def _start_range_bracket(
    guild_id: int, client: discord.Client,
    range_start_utc: str, range_end_utc: str,
    label: str, year_value: int, scope_desc: str,
) -> tuple[bool, str]:
    """
    Shared seeding + launch for real brackets over an arbitrary date window.
    *label* is the display name ('2026', 'Halloween 2026'); *year_value* is stored
    in the bracket's year column (must be > 0 so it isn't treated as a test bracket);
    *scope_desc* appears in the "no posts found" and tally messages.
    """
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

    tz    = _guild_tz(cfg)
    posts = get_rename_posts_in_range(guild_id, range_start_utc, range_end_utc, cfg["voting_enabled_at"])

    if not posts:
        return False, (
            f"⚠️ No rename posts found for {scope_desc}. "
            f"Renames are tracked automatically once a post channel is set (`!setpostchannel`)."
        )

    await bracket_channel.send(f"⏳ Tallying reactions from {len(posts)} rename posts for {scope_desc}...")

    scored: list[tuple[int, str, str | None]] = []
    for post in posts:
        count = await _get_total_reactions(client, post["channel_id"], post["message_id"])
        scored.append((count, post["quote"], post["quote_user"]))

    # Deduplicate — keep highest score per unique quote
    best: dict[str, tuple[int, str | None]] = {}
    for count, quote, user in scored:
        if quote not in best or count > best[quote][0]:
            best[quote] = (count, user)

    ranked = sorted(best.items(), key=lambda x: x[1][0], reverse=True)

    if len(ranked) < 2:
        return False, f"⚠️ Only {len(ranked)} unique rename(s) found for {scope_desc} — need at least 2."

    actual_size = bracket_size
    while actual_size > len(ranked) and actual_size > 2:
        actual_size //= 2

    if actual_size != bracket_size:
        await bracket_channel.send(
            f"ℹ️ Only {len(ranked)} unique names available — bracket shrunk to {actual_size} (from {bracket_size})."
        )

    nominees = ranked[:actual_size]

    bracket_id = create_bracket(
        guild_id, year_value, actual_size, voting_hours,
        label=label, range_start=range_start_utc, range_end=range_end_utc,
    )
    entry_ids  = []
    for seed, (quote, (score, user)) in enumerate(nominees, start=1):
        eid = create_bracket_entry(bracket_id, seed, quote, user, score)
        entry_ids.append(eid)

    total_rounds = int(math.log2(actual_size))
    lines = [
        f"🏆 **{label} Server Name Championship — {actual_size}-name bracket!**",
        f"Seeded by total reactions · {total_rounds} round(s) · {voting_hours}h per matchup\n",
    ]
    for seed, (quote, (score, user)) in enumerate(nominees, start=1):
        lines.append(f'  **#{seed}** "{quote}" — *{user or "Unknown"}* · {score} reactions')
    await bracket_channel.send("\n".join(lines))

    pairs = _first_round_pairs(entry_ids)
    matchup_rows = []
    for match_num, (a_id, b_id) in enumerate(pairs):
        mid = create_bracket_matchup(bracket_id, 1, match_num, a_id, b_id)
        matchup_rows.append((mid, a_id, b_id))

    await _post_round(bracket_id, 1, matchup_rows, bracket_channel, cfg, tz, client, list(posts))

    return True, f"✅ {label} bracket started! {actual_size} nominees, {len(pairs)} first-round matchup(s)."


async def start_bracket(guild_id: int, client: discord.Client, year: int) -> tuple[bool, str]:
    """Seed and launch a real bracket for a calendar *year*, seeded by reaction counts."""
    cfg = get_config(guild_id)
    tz  = _guild_tz(cfg)
    year_start, year_end = _year_utc_range(year, tz)
    return await _start_range_bracket(
        guild_id, client, year_start, year_end,
        label=str(year), year_value=year, scope_desc=str(year),
    )


async def start_season_bracket(guild_id: int, client: discord.Client, season_name: str) -> tuple[bool, str]:
    """Seed and launch a bracket from a named season's date window."""
    season = get_season(guild_id, season_name)
    if not season:
        return False, (
            f'⚠️ No season named "{season_name}". '
            f"Create one with `!addseason <start> <end> <name>` or see `!listseasons`."
        )
    # Display the season's real name (preserves original casing) and derive the
    # stored year from the window start so it's never mistaken for a test bracket.
    name = season["name"]
    try:
        year_value = datetime.fromisoformat(season["start_at"]).year
    except ValueError:
        year_value = datetime.now(pytz.utc).year
    return await _start_range_bracket(
        guild_id, client, season["start_at"], season["end_at"],
        label=name, year_value=max(year_value, 1), scope_desc=f'season "{name}"',
    )


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

        # Only retry on 0-0 if the poll isn't finalized yet.
        # Once finalized, 0-0 is a genuine tie and should go to coin flip.
        if votes_a == 0 and votes_b == 0:
            try:
                ch = client.get_channel(m["channel_id"])
                if ch:
                    msg = await ch.fetch_message(m["message_id"])
                    if msg.poll and not msg.poll.is_finalized:
                        log.info("[%s] Poll not yet finalized for matchup %s — retrying.", guild_id, m["id"])
                        all_complete = False
                        continue
            except Exception:
                pass
            log.info("[%s] Poll 0-0 and finalized for matchup %s — coin flip.", guild_id, m["id"])

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

    tz = _guild_tz(cfg)

    # Daily pacing: post the next unposted matchup now that this poll has
    # closed, rather than waiting for the whole round.  Only fall through to
    # advancing once every matchup in the round has been posted and completed.
    if _bracket_pacing(cfg) == "daily":
        fresh    = get_active_round_matchups(bracket["id"], round_num)
        unposted = [m for m in fresh if not m["message_id"]]
        if unposted:
            rename_posts = _rename_posts_for_bracket(guild_id, bracket, cfg, tz)
            total_rounds = int(math.log2(bracket["size"]))
            label        = _round_label(round_num, total_rounds)
            nxt          = unposted[0]
            await _post_daily_matchup(
                bracket["id"], nxt["id"], nxt["entry_a_id"], nxt["entry_b_id"],
                nxt["match_num"], len(fresh),
                bracket_channel, cfg, tz, client, rename_posts, label,
            )
            log.info("[%s] Daily pacing: posted Match %d.", guild_id, nxt["match_num"] + 1)
            return

    if not all_complete:
        return

    winners = get_round_winners_ordered(bracket["id"], round_num)

    if len(winners) == 1:
        champion = get_bracket_entry(winners[0])
        complete_bracket(bracket["id"])
        _test_card_cache.pop(bracket["id"], None)
        await bracket_channel.send(
            f"\n🎊🏆🎊 **{_bracket_label(bracket)} SERVER NAME CHAMPION** 🎊🏆🎊\n\n"
            f'**"{champion["quote"]}"**\n'
            f'*submitted by {champion["quote_user"] or "Unknown"} · '
            f'{champion["season_reactions"]} reactions this season*\n\n'
            f"Congratulations! 🎉"
        )
        log.info("[%s] Bracket complete. Champion: %s", guild_id, champion["quote"])
        return

    new_round    = advance_bracket_round(bracket["id"])
    pairs        = [(winners[i], winners[i + 1]) for i in range(0, len(winners), 2)]
    matchup_rows = []
    for match_num, (a_id, b_id) in enumerate(pairs):
        mid = create_bracket_matchup(bracket["id"], new_round, match_num, a_id, b_id)
        matchup_rows.append((mid, a_id, b_id))

    bracket_size = bracket["size"]
    total_rounds = int(math.log2(bracket_size))
    label        = _round_label(new_round, total_rounds)
    await bracket_channel.send(f"\n⚔️ **{label} begins!**")

    rename_posts = _rename_posts_for_bracket(guild_id, bracket, cfg, tz)
    await _post_round(bracket["id"], new_round, matchup_rows, bracket_channel, cfg, tz, client, rename_posts)
    log.info("[%s] Advanced to round %d.", guild_id, new_round)


def get_bracket_status_text(guild_id: int) -> str:
    bracket = get_active_bracket(guild_id)
    if not bracket:
        return "No active bracket."
    cfg          = get_config(guild_id)
    pacing       = _bracket_pacing(cfg)
    total_rounds = int(math.log2(bracket["size"]))
    label        = _round_label(bracket["current_round"], total_rounds)
    matchups     = get_active_round_matchups(bracket["id"], bracket["current_round"])
    done         = sum(1 for m in matchups if m["status"] == "complete")
    lines = [
        f"**{_bracket_label(bracket)} Bracket** — {label}",
        f"Size: {bracket['size']} · Round {bracket['current_round']}/{total_rounds} · "
        f"{done}/{len(matchups)} matchups complete",
        f"Pacing: **{pacing}**" + (" (all matchups at once)" if pacing == "round" else " (one matchup per day)"),
    ]
    if pacing == "daily":
        active = next((m for m in matchups if m["status"] == "active" and m["message_id"]), None)
        if active:
            lines.append(f"Currently voting: Match {active['match_num'] + 1} of {len(matchups)}")
        else:
            lines.append("No matchup currently open for voting.")
    return "\n".join(lines)
