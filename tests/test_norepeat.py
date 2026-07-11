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


def test_custom_feature_recent_text_skipped():
    chan = Chan([_msg(1, "meme A"), _msg(2, "meme B")])
    for _ in range(25):
        cand, _, uid = run(bf.get_random_content(chan, "text", recent_items={"meme A"}))
        assert (cand["content"], uid) == ("meme B", 2)


def test_custom_feature_link_query_is_identity():
    # Two YouTube links differing ONLY by query string are different items; the
    # window must not strip a link's query when comparing (unlike CDN images).
    used, fresh_link = "https://youtube.com/watch?v=aaa", "https://youtube.com/watch?v=bbb"
    chan = Chan([_msg(1, used), _msg(2, fresh_link)])
    for _ in range(25):
        cand, _, uid = run(bf.get_random_content(chan, "link", recent_items={used}))
        assert (cand["content"], uid) == (fresh_link, 2)


def test_custom_feature_attachment_signature_stripped():
    base = "https://cdn.discordapp.com/attachments/9/9/meme.png"
    att_msg = types.SimpleNamespace(
        author=types.SimpleNamespace(id=1, bot=False, display_name="user1"),
        content="", attachments=[types.SimpleNamespace(
            url=base + "?ex=zzz", content_type="image/png", filename="meme.png", size=100)])
    other = _msg(2, "https://imgur.com/fresh.gif")
    chan = Chan([att_msg, other])
    # recent set holds the SIGNED url exactly as it was logged at pick time
    for _ in range(25):
        cand, _, uid = run(bf.get_random_content(chan, "media", recent_items={base + "?ex=old"}))
        assert uid == 2


def test_custom_feature_all_recent_falls_back():
    chan = Chan([_msg(1, "only meme")])
    cand, _, uid = run(bf.get_random_content(chan, "text", recent_items={"only meme"}))
    assert (cand["content"], uid) == ("only meme", 1)
