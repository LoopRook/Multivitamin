import credits
import db_utils


class _Member:
    def __init__(self, nick, username):
        self.display_name = nick
        self.name = username


class _Guild:
    def __init__(self, members):
        self._m = members

    def get_member(self, uid):
        return self._m.get(uid)


class _Client:
    def __init__(self, members=None):
        self._g = _Guild(members or {})

    def get_guild(self, gid):
        return self._g


NICK = {7: _Member("Nickname", "handle")}


def test_style_and_mentions_defaults(gid):
    cfg = db_utils.get_config(gid)
    assert credits.style_of(cfg) == "nickname"
    assert credits.mentions_on(cfg) is False


def test_style_of_rejects_garbage():
    assert credits.style_of({"credit_style": "bogus"}) == "nickname"
    assert credits.style_of({}) == "nickname"


def test_resolve_name_live_beats_stored():
    c = _Client(NICK)
    # The stored snapshot is stale; the live member wins, in the chosen style.
    assert credits.resolve_name(c, 1, 7, "OldNick", "nickname") == "Nickname"
    assert credits.resolve_name(c, 1, 7, "OldNick", "username") == "handle"


def test_resolve_name_falls_back_when_member_gone():
    c = _Client({})
    assert credits.resolve_name(c, 1, 7, "Departed", "nickname") == "Departed"
    assert credits.resolve_name(c, 1, None, "NoUid", "nickname") == "NoUid"
    assert credits.resolve_name(c, 1, None, None, "nickname") == "Unknown"


def test_credit_mentions_only_when_enabled_and_allowed():
    c = _Client(NICK)
    on, off = {"credit_mentions": 1}, {"credit_mentions": 0}
    assert credits.credit(c, 1, 7, "s", on) == "<@7>"
    assert credits.credit(c, 1, 7, "s", off) == "Nickname"
    # Cards are images — callers pass mention=False and always get a plain name.
    assert credits.credit(c, 1, 7, "s", on, mention=False) == "Nickname"
    # No uid means no mention is possible.
    assert credits.credit(c, 1, None, "s", on) == "s"


def test_credit_line_drops_unknown_icon():
    c = _Client(NICK)
    cfg = {"credit_mentions": 0}
    assert credits.credit_line(c, 1, cfg, quote_user="a", quote_uid=None) == "submitted by a"
    line = credits.credit_line(c, 1, cfg, quote_user="a", quote_uid=None, icon_user="b", icon_uid=None)
    assert line == "submitted by a · icon by b"
    bare = credits.credit_line(c, 1, cfg, quote_user="a", quote_uid=None, icon_user="b",
                               icon_uid=None, prefix="")
    assert bare == "a · icon by b"
