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
    cnames = {c.name for c in main.config_group.walk_commands()}
    # slimmed surface: channels live in /setup, time lives in the schedule panel
    assert cnames == {"feature", "timezone", "schedule", "credits", "blocklist", "bestof"}


def test_all_views_instantiate(gid):
    # Instantiating a View enforces Discord's 5-action-row budget.
    main._BracketStartView(1, gid)
    main._BracketTestView(1, gid)
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
    main._CreditsView(1, gid)
    main._CreateChannelsModal(gid)          # quote/icon/post/best-of name inputs
    main._BracketChannelModal(gid)          # bracket channel name input
    # setup panel is the only create path (/feature add removed)
    fnames = {c.name for c in main.feature_group.walk_commands()}
    assert fnames == {"setup", "list", "edit", "schedule", "toggle", "remove", "access"}
    assert "reset" in {c.name for c in main.admin_group.walk_commands()}


def test_pickable_channel_types_include_threads():
    # A forum post is a thread; every channel picker must offer thread types.
    types = set(main._PICKABLE_CHANNEL_TYPES)
    assert {discord.ChannelType.text, discord.ChannelType.news,
            discord.ChannelType.public_thread, discord.ChannelType.private_thread,
            discord.ChannelType.news_thread} <= types


def test_channel_pickers_offer_threads(gid):
    # Feature setup (source + destination) and bracket start ("where it posts")
    # all pick channels; none may be narrowed back to text-only.
    views = [main._DailySetupView(1), main._BracketStartView(1, gid)]
    selects = [c for v in views for c in v.children
               if isinstance(c, discord.ui.ChannelSelect)]
    assert len(selects) >= 3
    for sel in selects:
        assert discord.ChannelType.public_thread in set(sel.channel_types)


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

    # A later rename that reuses the same quote is a different card: counted,
    # not flagged as a repeat (the reported bug).
    db_utils.store_rename_post(gid, 2001, 50, "Alpha", "u", 1, None, rename_key=2001)
    m6 = _fake_forward(gid, 9000, _ref(2001, FWD), 6)
    run(main.client._handle_forward_nomination(m6))
    assert m6.reacted == ["ℹ️"]

    # Another copy of that same rename (e.g. the /rename channel copy) IS a repeat.
    db_utils.store_rename_post(gid, 2002, 60, "Alpha", "u", 1, None, rename_key=2001)
    m7 = _fake_forward(gid, 9000, _ref(2002, FWD), 7)
    run(main.client._handle_forward_nomination(m7))
    assert m7.reacted == ["🔁"]


def test_repeat_nomination_uses_configured_reactions(gid):
    db_utils.store_rename_post(gid, 3001, 50, "Beta", "u", 1, None, rename_key=3001)
    db_utils.set_config(gid, "bracket_source_channel", 9000)
    db_utils.set_config(gid, "repeat_reactions", "🔁 ⏰")
    run(main.client._handle_forward_nomination(_fake_forward(gid, 9000, _ref(3001, FWD), 1)))
    again = _fake_forward(gid, 9000, _ref(3001, FWD), 2)
    run(main.client._handle_forward_nomination(again))
    assert again.reacted == ["🔁", "⏰"]


def test_refresh_rewrites_only_the_bots_pinned_instructions(gid):
    import bestof

    class Msg:
        def __init__(self, author_id, content):
            self.author = type("A", (), {"id": author_id})()
            self.content, self.edited = content, None

        async def edit(self, content):
            self.edited = content

    ours = Msg(1, bestof.instructions({"repeat_reactions": None}))
    theirs = Msg(2, bestof.INSTRUCTIONS_HEADER + " (someone else's copy)")

    class Chan:
        def pins(self, limit=50):
            async def gen():
                for m in (theirs, ours):
                    yield m
            return gen()

    guild = type("G", (), {"id": gid, "me": type("Me", (), {"id": 1})(),
                           "get_channel": lambda self, cid: Chan() if cid == 9000 else None})()
    db_utils.set_config(gid, "bracket_source_channel", 9000)
    db_utils.set_config(gid, "repeat_reactions", "⏰")
    run(main._refresh_source_instructions(guild))
    assert theirs.edited is None
    assert "⏰" in ours.edited and "🔁" not in ours.edited


def test_repeat_nomination_falls_back_when_no_emoji_works(gid):
    # e.g. a configured custom emoji was deleted: still mark the repeat with 🔁
    db_utils.store_rename_post(gid, 4001, 50, "Gamma", "u", 1, None, rename_key=4001)
    db_utils.set_config(gid, "bracket_source_channel", 9000)
    db_utils.set_config(gid, "repeat_reactions", "<:gone:777>")
    run(main.client._handle_forward_nomination(_fake_forward(gid, 9000, _ref(4001, FWD), 1)))
    again = _fake_forward(gid, 9000, _ref(4001, FWD), 2)
    real_add = again.add_reaction

    async def add(emoji):
        if emoji.startswith("<:"):
            raise discord.HTTPException(type("R", (), {"status": 400, "reason": "x"})(), "Unknown Emoji")
        await real_add(emoji)
    again.add_reaction = add
    run(main.client._handle_forward_nomination(again))
    assert again.reacted == ["🔁"]


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
        return type("G", (), {"me": me, "name": "Test Server"})()


def test_showconfig_health_warnings(gid):
    txt = run(bot_features.build_config(gid, _HealthClient(manage_guild=False)))
    assert "Warnings:" in txt
    assert "No Post Channel" in txt
    assert "Manage Server" in txt
    # The bracket channel is picked per-run in /bracket start — not a setup gap.
    assert "No Bracket Channel" not in txt


def test_showconfig_health_clean(gid):
    db_utils.set_config(gid, "post_channel", 1)
    db_utils.set_config(gid, "bracket_channel", 2)
    txt = run(bot_features.build_config(gid, _HealthClient(manage_guild=True)))
    assert "no configuration warnings" in txt.lower()


def test_showconfig_lists_custom_features(gid):
    db_utils.add_custom_feature(gid, "Meme of the Day", "<:yoba:1446632582472143019>",
                                "media", 111, 222, "12:00", command="meme")
    txt = run(bot_features.build_config(gid, _HealthClient(manage_guild=True)))
    assert "Custom Features:" in txt
    assert "Meme of the Day" in txt
    assert "/meme" in txt
    # header is the server name, not a raw guild id label
    assert "Server:" in txt
    assert "Guild ID:" not in txt
    # custom-emoji shortcodes can't render in a code block, so they're stripped
    assert "<:yoba:" not in txt
    # legacy single-song plumbing must no longer appear in the config view
    for legacy in ("Song Feature", "Song Post Channel", "Song Time", "Music Channel"):
        assert legacy not in txt


def test_showconfig_shows_repeat_reaction(gid):
    txt = run(bot_features.build_config(gid, _HealthClient(manage_guild=True)))
    assert "Best-of Repeat:      🔁" in txt                       # default
    db_utils.set_config(gid, "repeat_reactions", "⏰ <:yoba:123>")
    txt = run(bot_features.build_config(gid, _HealthClient(manage_guild=True)))
    assert "Best-of Repeat:      ⏰ :yoba:" in txt                 # custom shown as :name:


def test_showconfig_no_features(gid):
    txt = run(bot_features.build_config(gid, _HealthClient(manage_guild=True)))
    assert "Custom Features:     None" in txt


def test_openrename_toggle_wired(gid):
    field, _ = main._FEATURE_MAP["openrename"]
    assert field == "rename_open"
    db_utils.get_config(gid)
    db_utils.set_config(gid, field, 0)                 # valid config field
    assert db_utils.get_config(gid)["rename_open"] == 0
    assert db_utils.get_config(gid)["credit_style"]    # migration neighbours intact


def test_feature_access_all_modes(gid):
    # /feature access crashed live on 'everyone'/'admin': a dict literal built
    # f"{role.mention}" eagerly even when no role was passed. Drive the real
    # callback through every mode.
    db_utils.add_custom_feature(gid, "Meme", None, "media", 1, 2, "9:00", command="meme")
    replies = []

    class _Resp:
        async def send_message(self, msg, ephemeral=False):
            replies.append(msg)

    def _inter():
        return type("I", (), {"guild_id": gid, "response": _Resp()})()

    cb = main.feature_access.callback
    run(cb(_inter(), "meme", "everyone", None))
    assert "anyone" in replies[-1]
    run(cb(_inter(), "meme", "admin", None))
    assert "admins only" in replies[-1]
    role = type("Role", (), {"id": 424242, "mention": "<@&424242>"})()
    run(cb(_inter(), "meme", "role", role))
    assert "<@&424242>" in replies[-1]
    assert db_utils.get_custom_feature_by_command(gid, "meme")["run_roles"] == "424242"
    run(cb(_inter(), "meme", "role", None))          # role mode w/o role -> friendly error
    assert replies[-1].startswith("⚠️")
