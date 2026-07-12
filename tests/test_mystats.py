"""build_mystats / build_contributors: quote + icon plus per-feature counts,
with the legacy single-song plumbing gone."""
import asyncio
import types

import bot_features as bf
import db_utils

run = asyncio.run


def _msg(uid, content="", attachments=()):
    author = types.SimpleNamespace(id=uid, bot=False, display_name=f"user{uid}")
    return types.SimpleNamespace(author=author, content=content, attachments=list(attachments))


def _att(url="http://x/a.png"):
    return types.SimpleNamespace(url=url, content_type="image/png", size=1, filename="a.png")


class Chan:
    def __init__(self, msgs):
        self._msgs = msgs

    def history(self, limit=None, oldest_first=False):
        async def gen():
            for m in self._msgs:
                yield m
        return gen()


class Client:
    def __init__(self, by_cid):
        self._by = by_cid

    def get_channel(self, cid):
        return self._by.get(cid)


def test_mystats_counts_custom_features(gid):
    # A media feature sourced from channel 111: user 7 posted two qualifying
    # messages, user 9 one. mystats should count 2 for user 7.
    db_utils.add_custom_feature(gid, "Meme of the Day", "M", "media", 111, 222, "12:00", command="meme")
    memes = Chan([_msg(7, attachments=[_att()]), _msg(7, "cap", [_att()]), _msg(9, attachments=[_att()])])
    txt = run(bf.build_mystats(gid, Client({111: memes}), 7, "user7"))
    assert "Meme of the Day submitted: **2**" in txt
    assert "Quotes submitted" in txt
    assert "Images submitted" in txt
    assert "Songs submitted" not in txt          # legacy hardcoded line is gone


def test_mystats_no_features_has_no_song_line(gid):
    txt = run(bf.build_mystats(gid, Client({}), 1, "user1"))
    assert "Quotes submitted" in txt
    assert "Images submitted" in txt
    assert "Songs submitted" not in txt


def test_contributors_quote_works_song_retired(gid):
    # /contributors is quote|icon only now. The real category resolves; the
    # retired 'song' category is treated as unconfigured, not a crash.
    quotes = Chan([_msg(1, "a good line"), _msg(1, "another")])
    client = Client({db_utils.get_config(gid)["quote_channel"] or 0: quotes})
    # point the quote channel at our fake
    db_utils.set_config(gid, "quote_channel", 77)
    txt = run(bf.build_contributors(gid, Client({77: quotes}), "quote"))
    assert "Quote contributors" in txt

    retired = run(bf.build_contributors(gid, Client({}), "song"))
    assert "not configured" in retired          # graceful, no KeyError
