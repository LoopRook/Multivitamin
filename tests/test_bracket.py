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
    assert sorted(scored) == [(3, "A", "u"), (5, "A", "u")]


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
