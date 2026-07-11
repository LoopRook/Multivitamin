import asyncio
import types

import bot_features as bf
import db_utils

run = asyncio.run


def _msg(uid, content="", attachments=()):
    author = types.SimpleNamespace(id=uid, bot=False, display_name=f"user{uid}")
    return types.SimpleNamespace(author=author, content=content, attachments=list(attachments))


def _att(url):
    return types.SimpleNamespace(url=url, content_type="image/png")


class Chan:
    def __init__(self, msgs):
        self._msgs = msgs

    def history(self, limit=None, oldest_first=False):
        async def gen():
            for m in self._msgs:
                yield m
        return gen()


def test_recent_quote_is_skipped():
    # user 1 has ONE quote which was used recently; user 2 must always win.
    chan = Chan([_msg(1, "old faithful"), _msg(2, "something new")])
    for _ in range(25):
        item, _, uid = run(bf.get_random_quote(chan, recent_items={"old faithful"}))
        assert (item, uid) == ("something new", 2)


def test_all_recent_falls_back_instead_of_failing():
    chan = Chan([_msg(1, "old faithful")])
    item, _, uid = run(bf.get_random_quote(chan, recent_items={"old faithful"}))
    assert (item, uid) == ("old faithful", 1)   # tiny server: repeat beats no rename


def test_user_with_fresh_material_stays_in():
    # user 1's pool is {used, fresh}: they stay eligible but only via the fresh item.
    chan = Chan([_msg(1, "used one\nfresh one")])
    for _ in range(25):
        item, _, _ = run(bf.get_random_quote(chan, recent_items={"used one"}))
        assert item == "fresh one"


def test_recent_icon_matches_despite_cdn_signature():
    # Stored URL and freshly scanned URL differ by rotating ?ex=... signature params.
    base = "https://cdn.discordapp.com/attachments/1/2/cat.png"
    chan = Chan([
        _msg(1, attachments=[_att(base + "?ex=aaa&is=bbb")]),
        _msg(2, attachments=[_att("https://cdn.discordapp.com/attachments/3/4/dog.png?ex=ccc")]),
    ])
    for _ in range(25):
        url, _, uid = run(bf.get_random_icon(chan, recent_items={base}))
        assert uid == 2 and "dog.png" in url


def test_get_recent_pick_items_windowing(gid):
    db_utils.log_pick(gid, 1, "u", "quote", "inside window")
    assert "inside window" in db_utils.get_recent_pick_items(gid, "quote", "2000-01-01")
    # a since-cutoff in the far future excludes everything
    assert db_utils.get_recent_pick_items(gid, "quote", "9999-01-01") == set()
    # other categories don't bleed in
    assert db_utils.get_recent_pick_items(gid, "icon", "2000-01-01") == set()
