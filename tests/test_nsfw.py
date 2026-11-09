"""Content from age-restricted (NSFW) channels stays in age-restricted channels
unless a Manage Server admin turns that off with /config nsfw."""
import asyncio
import types

import bot_features as bf
import db_utils
import main
import nsfw
from _fakes import FakeGuild

run = asyncio.run


class Chan:
    def __init__(self, cid, is_nsfw=False, texts=()):
        self.id = cid
        self.name = f"chan{cid}"
        self.mention = f"<#{cid}>"
        self._nsfw = is_nsfw
        self._texts = list(texts)
        self.sent = []

    def is_nsfw(self):
        return self._nsfw

    def history(self, limit=None, oldest_first=False):
        async def gen():
            for i, t in enumerate(self._texts):
                author = types.SimpleNamespace(id=i + 1, bot=False, display_name=f"user{i + 1}")
                yield types.SimpleNamespace(author=author, content=t, attachments=[])
        return gen()

    async def send(self, content=None, **kw):
        self.sent.append(content)


_ALL_PERMS = types.SimpleNamespace(manage_guild=True, attach_files=True, embed_links=True,
                                   add_reactions=True, read_message_history=True)


class Client:
    def __init__(self, *chans, guild=None):
        self._chans = {c.id: c for c in chans}
        self._guild = guild

    def get_channel(self, cid):
        return self._chans.get(cid)

    def get_guild(self, gid):
        return self._guild


def _feature(gid, src, dest):
    db_utils.add_custom_feature(gid, "Meme", None, "text", src, dest, "9:00", command="meme")
    return db_utils.get_custom_feature_by_command(gid, "meme")


# ── the rule itself ──────────────────────────────────────────────────────────

def test_guard_is_on_by_default(gid):
    assert nsfw.guard_on(db_utils.get_config(gid))


def test_age_restricted_reads_the_channel_flag():
    assert nsfw.is_age_restricted(Chan(1, is_nsfw=True))
    assert not nsfw.is_age_restricted(Chan(1))
    assert not nsfw.is_age_restricted(None)
    assert not nsfw.is_age_restricted(object())   # channel types without the flag


def test_feature_blocked_only_from_nsfw_into_sfw(gid):
    cfg = db_utils.get_config(gid)
    reason = nsfw.feature_block_reason(cfg, Chan(1, is_nsfw=True), Chan(2), "Meme")
    assert "<#1>" in reason and "<#2>" in reason and "/config nsfw" in reason
    assert nsfw.feature_block_reason(cfg, Chan(1, is_nsfw=True), Chan(2, is_nsfw=True), "Meme") is None
    assert nsfw.feature_block_reason(cfg, Chan(1), Chan(2), "Meme") is None
    assert nsfw.feature_block_reason(cfg, Chan(1), Chan(2, is_nsfw=True), "Meme") is None


def test_feature_allowed_anywhere_with_guard_off(gid):
    db_utils.set_config(gid, "nsfw_guard", 0)
    cfg = db_utils.get_config(gid)
    assert nsfw.feature_block_reason(cfg, Chan(1, is_nsfw=True), Chan(2), "Meme") is None


def test_rename_never_uses_nsfw_sources_with_guard_on(gid):
    # The server name and icon are shown everywhere, so no destination is age-restricted.
    cfg = db_utils.get_config(gid)
    assert "<#1>" in nsfw.rename_block_reason(cfg, Chan(1, is_nsfw=True), Chan(2))
    assert "<#2>" in nsfw.rename_block_reason(cfg, Chan(1), Chan(2, is_nsfw=True))
    assert nsfw.rename_block_reason(cfg, Chan(1), Chan(2)) is None
    db_utils.set_config(gid, "nsfw_guard", 0)
    assert nsfw.rename_block_reason(db_utils.get_config(gid), Chan(1, is_nsfw=True), Chan(2)) is None


# ── wired into the posting paths ─────────────────────────────────────────────

def test_feature_post_from_nsfw_into_sfw_is_skipped(gid):
    src, dest = Chan(11, is_nsfw=True, texts=["spicy"]), Chan(12)
    ok, detail = run(bf.process_custom_daily(gid, Client(src, dest), _feature(gid, 11, 12)))
    assert not ok
    assert "/config nsfw" in detail
    assert dest.sent == []


def test_feature_post_from_nsfw_into_nsfw_goes_through(gid):
    src, dest = Chan(11, is_nsfw=True, texts=["spicy"]), Chan(12, is_nsfw=True)
    ok, _ = run(bf.process_custom_daily(gid, Client(src, dest), _feature(gid, 11, 12)))
    assert ok
    assert "spicy" in dest.sent[0]


def test_feature_post_with_guard_off_goes_anywhere(gid):
    db_utils.set_config(gid, "nsfw_guard", 0)
    src, dest = Chan(11, is_nsfw=True, texts=["spicy"]), Chan(12)
    ok, _ = run(bf.process_custom_daily(gid, Client(src, dest), _feature(gid, 11, 12)))
    assert ok


def test_on_demand_checks_the_channel_it_posts_in(gid):
    # /meme and /preview post where they're run, not in the feature's destination.
    src, dest, here = Chan(11, is_nsfw=True, texts=["spicy"]), Chan(12, is_nsfw=True), Chan(13)
    ok, _ = run(bf.process_custom_daily(gid, Client(src, dest, here), _feature(gid, 11, 12),
                                        override_post_channel=here, on_demand=True))
    assert not ok and here.sent == []
    ok, _ = run(bf.process_custom_daily(gid, Client(src, dest, here), _feature(gid, 11, 12),
                                        override_post_channel=here, preview=True))
    assert not ok and here.sent == []


def test_rename_from_nsfw_channel_is_skipped(gid):
    db_utils.set_config(gid, "quote_channel", 21)
    db_utils.set_config(gid, "icon_channel", 22)
    guild = FakeGuild()
    client = Client(Chan(21, is_nsfw=True, texts=["a quote"]), Chan(22), guild=guild)
    reason = run(bf.process_rename(gid, client))
    assert "/config nsfw" in reason
    assert guild.edits == []
    preview_here = Chan(23)
    assert run(bf.process_rename(gid, client, override_post_channel=preview_here, preview=True))
    assert preview_here.sent == []


# ── /config nsfw ─────────────────────────────────────────────────────────────

def _inter(gid, replies, manage_guild):
    async def send_message(msg, ephemeral=False):
        replies.append(msg)
    user = types.SimpleNamespace(guild_permissions=types.SimpleNamespace(manage_guild=manage_guild))
    return types.SimpleNamespace(guild_id=gid, user=user,
                                 response=types.SimpleNamespace(send_message=send_message))


def test_config_nsfw_turning_off_needs_manage_server(gid):
    replies = []
    cb = main.config_nsfw.callback
    run(cb(_inter(gid, replies, manage_guild=False), False))     # bot-admin without Manage Server
    assert "Manage Server" in replies[-1]
    assert db_utils.get_config(gid)["nsfw_guard"] == 1
    run(cb(_inter(gid, replies, manage_guild=True), False))
    assert db_utils.get_config(gid)["nsfw_guard"] == 0
    assert "visible to everyone" in replies[-1]                  # renames warning
    run(cb(_inter(gid, replies, manage_guild=False), True))      # any admin may turn it back on
    assert db_utils.get_config(gid)["nsfw_guard"] == 1


def test_config_nsfw_without_a_value_shows_the_state(gid):
    replies = []
    run(main.config_nsfw.callback(_inter(gid, replies, manage_guild=False), None))
    assert "**on**" in replies[-1]
    assert "age-restricted" in replies[-1].split(":")[0].lower()   # names Discord's toggle up front
    assert db_utils.get_config(gid)["nsfw_guard"] == 1


# Discord's channel toggle is called "Age-Restricted Channel", so user-facing text
# leads with that term; "NSFW" stays in the command name and as a parenthetical.
def test_help_lists_config_nsfw():
    embed = main.build_help_embed(is_admin=True, is_manager=True)
    line = next(l for f in embed.fields for l in f.value.splitlines() if "/config nsfw" in l)
    assert "age-restricted (NSFW) channels" in line


def test_config_nsfw_description_matches_discords_toggle():
    assert "age-restricted" in main.config_nsfw.description.lower()


# ── /showconfig ──────────────────────────────────────────────────────────────

def _showconfig(gid, *chans):
    guild = types.SimpleNamespace(name="S", me=types.SimpleNamespace(guild_permissions=_ALL_PERMS))
    return run(bf.build_config(gid, Client(*chans, guild=guild)))


def test_showconfig_shows_the_setting(gid):
    assert "NSFW Content:        Kept in age-restricted channels" in _showconfig(gid)
    db_utils.set_config(gid, "nsfw_guard", 0)
    assert "NSFW Content:        Can post anywhere" in _showconfig(gid)


def test_showconfig_warns_about_blocked_sources(gid):
    db_utils.set_config(gid, "post_channel", 30)
    db_utils.set_config(gid, "quote_channel", 31)
    _feature(gid, 32, 33)
    chans = (Chan(30), Chan(31, is_nsfw=True), Chan(32, is_nsfw=True), Chan(33))
    txt = _showconfig(gid, *chans)
    assert "#chan31 is age-restricted" in txt          # rename source
    assert "Meme: #chan32 is age-restricted" in txt    # feature src -> sfw dest
    db_utils.set_config(gid, "nsfw_guard", 0)
    assert "age-restricted" not in _showconfig(gid, *chans)
