import asyncio

import discord

import db_utils
import bot_features
import main

run = asyncio.run
FWD = discord.MessageReferenceType.forward


def test_command_surface():
    bnames = {c.name for c in main.bracket_group.walk_commands()}
    assert bnames == {"start", "test", "forceadvance", "status", "cancel", "history"}  # no /bracket config
    top = {c.name for c in main.client.tree.get_commands()}
    assert {"setup", "season", "showconfig", "help", "rename", "mystats", "preview", "contributors"} <= top
    # removed commands must be gone
    assert "bracketchannel" not in {c.name for c in main.config_group.walk_commands()}


def test_all_views_instantiate(gid):
    # Instantiating a View enforces Discord's 5-action-row budget.
    main._BracketStartView(1, gid)
    main._SeasonView(1, gid)
    wiz = main._SetupWizardView(1, gid)     # stepped wizard
    for s in range(len(main._WIZARD_STEPS)):  # every step must build within the 5-row budget
        wiz.step = s
        wiz._rebuild()
    main._WizardTimeModal(wiz)
    main._ScheduleView(1, gid)
    # feature-targeted schedule view
    db_utils.add_custom_feature(gid, "Critter", None, "media", 1, 2, "9:00", command="critter")
    feat = db_utils.get_custom_feature_by_command(gid, "critter")
    main._ScheduleView(1, gid, feature=feat)
    main._ResetConfirmView(1, gid)
    main._CreateChannelsModal(gid)          # quote/icon/post/best-of name inputs
    main._BracketChannelModal(gid)          # bracket channel name input
    assert "schedule" in {c.name for c in main.daily_group.walk_commands()}
    assert "reset" in {c.name for c in main.admin_group.walk_commands()}


def test_help_embed_within_field_limits():
    e = main.build_help_embed(is_admin=True, is_manager=True)
    assert all(len(f.value) <= 1024 for f in e.fields)
    total = len(e.title) + len(e.description) + sum(len(f.name) + len(f.value) for f in e.fields)
    assert total <= 6000


def test_version_string():
    assert isinstance(main.__version__, str) and main.__version__


def test_normalize_channel_name():
    assert main._normalize_channel_name("  #Best Of ") == "best-of"
    assert main._normalize_channel_name("Renames") == "renames"
    assert main._normalize_channel_name("") == ""


def test_try_dm_delivery():
    class OkUser:
        bot = False
        async def send(self, text): pass

    class FailUser:
        bot = False
        async def send(self, text):
            raise discord.HTTPException(type("R", (), {"status": 403, "reason": "x"})(), "no dm")

    class BotUser:
        bot = True
        async def send(self, text): pass

    assert run(main.client._try_dm(OkUser(), "hi")) is True
    assert run(main.client._try_dm(FailUser(), "hi")) is False   # DMs off -> fall back
    assert run(main.client._try_dm(None, "hi")) is False
    assert run(main.client._try_dm(BotUser(), "hi")) is False


def _fake_forward(gid, chan_id, reference, mid):
    o = type("Msg", (), {})()
    o.guild = type("G", (), {"id": gid})()
    o.channel = type("C", (), {"id": chan_id})()
    o.reference = reference
    o.id = mid
    o.author = type("A", (), {"bot": False})()
    o.reacted = []

    async def add(emoji, _o=o):
        _o.reacted.append(emoji)
    o.add_reaction = add
    return o


def _ref(mid, rtype):
    return type("Ref", (), {"message_id": mid, "type": rtype})()


def test_forward_nomination_reactions(gid):
    db_utils.get_config(gid)
    db_utils.store_rename_post(gid, 1001, 50, "Alpha", "u", 1, None)
    db_utils.set_config(gid, "bracket_source_channel", 9000)

    m1 = _fake_forward(gid, 9000, _ref(1001, FWD), 1)
    run(main.client._handle_forward_nomination(m1))
    assert m1.reacted == ["ℹ️"]                 # first valid forward

    m2 = _fake_forward(gid, 9000, _ref(1001, FWD), 2)
    run(main.client._handle_forward_nomination(m2))
    assert m2.reacted == ["🔁"]                 # duplicate

    m3 = _fake_forward(gid, 9000, _ref(1234, FWD), 3)
    run(main.client._handle_forward_nomination(m3))
    assert m3.reacted == []                      # forward of untracked message

    m4 = _fake_forward(gid, 1234, _ref(1001, FWD), 4)
    run(main.client._handle_forward_nomination(m4))
    assert m4.reacted == []                      # wrong channel

    m5 = _fake_forward(gid, 9000, _ref(1001, discord.MessageReferenceType.default), 5)
    run(main.client._handle_forward_nomination(m5))
    assert m5.reacted == []                      # reply, not a forward


class _Perms:
    def __init__(self, **kw):
        for a in ("manage_guild", "attach_files", "embed_links", "add_reactions", "read_message_history"):
            setattr(self, a, kw.get(a, True))


class _HealthClient:
    def __init__(self, manage_guild):
        self._mg = manage_guild

    def get_channel(self, cid):
        return None

    def get_guild(self, gid):
        me = type("Me", (), {"guild_permissions": _Perms(manage_guild=self._mg)})()
        return type("G", (), {"me": me})()


def test_showconfig_health_warnings(gid):
    txt = run(bot_features.build_config(gid, _HealthClient(manage_guild=False)))
    assert "Warnings:" in txt
    assert "No Post Channel" in txt and "No Bracket Channel" in txt
    assert "Manage Server" in txt


def test_showconfig_health_clean(gid):
    db_utils.set_config(gid, "post_channel", 1)
    db_utils.set_config(gid, "bracket_channel", 2)
    txt = run(bot_features.build_config(gid, _HealthClient(manage_guild=True)))
    assert "no configuration warnings" in txt.lower()
