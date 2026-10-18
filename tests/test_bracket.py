import asyncio
from datetime import datetime, timedelta

import discord
import pytz

import bracket
import db_utils
from _fakes import FakeGuild, ForbiddenGuild, FakeClient, Channel, ref, reaction, forward_msg

run = asyncio.run
FWD = discord.MessageReferenceType.forward


def test_effective_cfg_snapshot_and_legacy_fallback():
    cfg = {"bracket_voting_hours": 24, "bracket_pacing": "round", "timezone": "UTC"}
    eff = bracket._effective_cfg(cfg, {"voting_hours": 48, "pacing": "daily"})
    assert eff["bracket_voting_hours"] == 48 and eff["bracket_pacing"] == "daily"
    legacy = bracket._effective_cfg(cfg, {"voting_hours": None, "pacing": None})
    assert legacy["bracket_voting_hours"] == 24 and legacy["bracket_pacing"] == "round"


def test_rename_guild_idempotent_and_missing():
    g = FakeGuild("Old")
    assert run(bracket._rename_guild(FakeClient(g), 1, "New")) is True and g.edits == ["New"]
    assert run(bracket._rename_guild(FakeClient(g), 1, "New")) is True and g.edits == ["New"]  # no re-edit
    assert run(bracket._rename_guild(FakeClient(None), 1, "X")) is False


def test_rename_guild_forbidden_warns_once(gid):
    db_utils.set_config(gid, "bracket_channel", 4242)
    ch = Channel()
    cl = FakeClient(ForbiddenGuild("S"), ch)
    bracket._rename_forbidden_warned.discard(gid)
    assert run(bracket._rename_guild(cl, gid, "New")) is False
    run(bracket._rename_guild(cl, gid, "New2"))
    assert sum("Manage Server" in (m or "") for m in ch.sent) == 1  # warned exactly once


def test_drive_server_name_real_vs_test():
    g = FakeGuild("Old")
    assert run(bracket._drive_server_name(FakeClient(g), 1, {"year": 2026}, "Winner", Channel())) is True
    gt, ch = FakeGuild("Real"), Channel()
    assert run(bracket._drive_server_name(FakeClient(gt), 1, {"year": 0}, "TestWinner", ch)) is False
    assert gt.edits == [] and any("would rename" in (m or "") for m in ch.sent)


def test_rotate_round_pacing_slice_and_guards(monkeypatch):
    now = datetime.now(pytz.utc)
    ends = (now + timedelta(hours=1.5)).isoformat()  # 4h window, start now-2.5h -> slot 1h -> idx 2
    mus = [{"match_num": 0, "entry_a_id": 10, "entry_b_id": 11, "ends_at": ends},
           {"match_num": 1, "entry_a_id": 12, "entry_b_id": 13, "ends_at": ends}]
    monkeypatch.setattr(bracket, "get_active_round_matchups", lambda b, r: mus)
    monkeypatch.setattr(bracket, "get_bracket_entry", lambda e: {"quote": f"E{e}"})

    g = FakeGuild("S")
    run(bracket._rotate_round_pacing_name(FakeClient(g), 1, {"id": 1, "current_round": 2}, {"bracket_voting_hours": 4}))
    assert g.name == "E12"

    g1 = FakeGuild("S1")  # round 1 = nominees, must not rotate
    run(bracket._rotate_round_pacing_name(FakeClient(g1), 1, {"id": 1, "current_round": 1}, {"bracket_voting_hours": 4}))
    assert g1.edits == []

    big = [{"match_num": j, "entry_a_id": 20 + 2 * j, "entry_b_id": 21 + 2 * j, "ends_at": ends} for j in range(4)]
    monkeypatch.setattr(bracket, "get_active_round_matchups", lambda b, r: big)
    g2 = FakeGuild("S2")  # 8 combatants / 1h window -> slices < 10 min -> skip
    run(bracket._rotate_round_pacing_name(FakeClient(g2), 1, {"id": 1, "current_round": 2}, {"bracket_voting_hours": 1}))
    assert g2.edits == []


def test_scored_from_forwards_excludes_bot_and_dedups():
    posts = [{"message_id": 1, "quote": "A", "quote_user": "u"},
             {"message_id": 2, "quote": "A", "quote_user": "u"}]  # two tracked copies, same quote
    msgs = [
        forward_msg(ref(1, FWD), [reaction(1, me=True), reaction(3)]),  # bot ℹ️ + 3 humans -> 3
        forward_msg(ref(2, FWD), [reaction(5)]),                        # 5 humans -> 5
        forward_msg(ref(99, FWD), [reaction(9)]),                       # untracked -> ignored
        forward_msg(None, [reaction(4)]),                               # not a forward -> ignored
    ]
    scored = run(bracket._scored_from_forwards(FakeClient(channel=Channel(msgs)), 7, posts))
    # The whole post row rides along so the entry keeps its quote/icon credits.
    assert sorted((c, p["quote"], p["quote_user"]) for c, p in scored) == [(3, "A", "u"), (5, "A", "u")]


def test_best_of_counts_distinct_people_not_emoji():
    # Best-of seeding is one-person-one-point: stacking emoji must not inflate.
    posts = [{"message_id": 1, "quote": "Stacked", "quote_user": "u"},
             {"message_id": 2, "quote": "Popular", "quote_user": "u"}]
    msgs = [
        # user 5 alone piled on four different emoji -> still worth 1
        forward_msg(ref(1, FWD), [reaction(1, user_ids=[5]), reaction(1, user_ids=[5]),
                                  reaction(1, user_ids=[5]), reaction(1, user_ids=[5])]),
        # three different people, some overlapping emoji -> worth 3
        forward_msg(ref(2, FWD), [reaction(2, user_ids=[7, 8]), reaction(2, user_ids=[8, 9])]),
    ]
    scored = run(bracket._scored_from_forwards(FakeClient(channel=Channel(msgs)), 7, posts))
    by_quote = {p["quote"]: c for c, p in scored}
    assert by_quote == {"Stacked": 1, "Popular": 3}


def test_best_of_ignores_bot_reactions():
    # the bot's own info/duplicate confirmations must never score
    posts = [{"message_id": 1, "quote": "A", "quote_user": "u"}]
    msgs = [forward_msg(ref(1, FWD), [reaction(1, me=True), reaction(1, user_ids=[4])])]
    scored = run(bracket._scored_from_forwards(FakeClient(channel=Channel(msgs)), 7, posts))
    assert [c for c, _ in scored] == [1]


def test_crown_champion_credits_icon_poster(gid):
    ch = Channel()
    champ = {"id": 5, "quote": "Win", "quote_user": "a", "quote_uid": None,
             "icon_user": "iconguy", "icon_uid": None, "season_reactions": 3}
    run(bracket._crown_champion(FakeClient(FakeGuild("Old")), gid, ch,
                                {"id": 9, "year": 2026, "label": "2026"}, champ, []))
    body = "\n".join(m or "" for m in ch.sent)
    assert "submitted by a" in body and "icon by iconguy" in body


def test_crown_champion_renames_and_attaches_card():
    ch = Channel()
    champ = {"id": 5, "quote": "Win", "quote_user": "a", "season_reactions": 3}
    bracket._test_card_cache[9] = {5: b"PNGDATA"}
    g = FakeGuild("Old")
    run(bracket._crown_champion(FakeClient(g), 1, ch, {"id": 9, "year": 2026, "label": "2026"}, champ, []))
    assert g.edits == ["Win"]  # renamed to champion
    assert any("CHAMPION" in (m or "") for m in ch.sent)
    bracket._test_card_cache.pop(9, None)


def test_crown_champion_test_bracket_no_rename():
    ch = Channel()
    g = FakeGuild("Real")
    run(bracket._crown_champion(FakeClient(g), 1, ch, {"id": 8, "year": 0, "label": "TEST"},
                                {"id": 1, "quote": "T", "quote_user": "x", "season_reactions": 1}, []))
    assert g.edits == []  # test brackets never rename the real server


def test_send_lines_chunks_under_discord_cap():
    ch = Channel()
    lines = [f'**#{i}** "' + "q" * 120 + '" — name · 9 reactions' for i in range(32)]
    run(bracket._send_lines(ch, lines))
    assert len(ch.sent) > 1                              # split, not one oversized send
    assert all(len(m) <= 2000 for m in ch.sent)          # every chunk fits
    assert "".join(ch.sent).count("**#") == 32           # nothing dropped


# ---- card lookup: identity is the seeded post, not the quote text ----

class _FakeResp:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    async def read(self):
        return self._data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _FakeSession:
    def __init__(self, *a, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def get(self, url):
        return _FakeResp(b"IMG:" + url.encode())


def _same_quote_posts():
    return [
        {"quote": "Same", "channel_id": 5, "message_id": 100, "posted_at": "2026-01-01T00:00:00"},
        {"quote": "Same", "channel_id": 5, "message_id": 200, "posted_at": "2026-02-01T00:00:00"},
    ]


def _stub_card_network(monkeypatch, gone=()):
    import aiohttp

    async def fresh(client, channel_id, message_id):
        return None if message_id in gone else f"https://cdn/{message_id}.png"

    monkeypatch.setattr(bracket, "_get_fresh_image_url", fresh)
    monkeypatch.setattr(aiohttp, "ClientSession", _FakeSession)


def test_card_bytes_uses_seeded_post_not_newest_same_quote(monkeypatch):
    _stub_card_network(monkeypatch)
    entry = {"id": 1, "quote": "Same", "source_channel_id": 5, "source_message_id": 100}
    card = run(bracket._get_card_bytes(entry, 777, FakeClient(), _same_quote_posts()))
    assert card == b"IMG:https://cdn/100.png"  # the older, seeded post, not the newest


def test_card_bytes_legacy_entry_falls_back_to_newest_quote_match(monkeypatch):
    _stub_card_network(monkeypatch)
    entry = {"id": 2, "quote": "Same"}  # no source ids (pre-migration entry)
    card = run(bracket._get_card_bytes(entry, 777, FakeClient(), _same_quote_posts()))
    assert card == b"IMG:https://cdn/200.png"


def test_card_bytes_falls_back_when_source_message_is_gone(monkeypatch):
    _stub_card_network(monkeypatch, gone={100})
    entry = {"id": 3, "quote": "Same", "source_channel_id": 5, "source_message_id": 100}
    card = run(bracket._get_card_bytes(entry, 777, FakeClient(), _same_quote_posts()))
    assert card == b"IMG:https://cdn/200.png"


def test_create_bracket_entry_stores_source_ids():
    bid = db_utils.create_bracket(1, 2026, 2, 24)
    eid = db_utils.create_bracket_entry(bid, 1, "q", "u", 5,
                                        source_channel_id=5, source_message_id=100)
    row = db_utils.get_bracket_entry(eid)
    assert (row["source_channel_id"], row["source_message_id"]) == (5, 100)
    legacy = db_utils.get_bracket_entry(db_utils.create_bracket_entry(bid, 2, "q2", "u", 1))
    assert legacy["source_channel_id"] is None and legacy["source_message_id"] is None
