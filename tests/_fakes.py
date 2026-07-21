"""Lightweight fakes so async Discord code can be exercised without a gateway."""
import discord


class FakeGuild:
    def __init__(self, name="Server"):
        self.name = name
        self.edits = []

    async def edit(self, name=None, **kw):
        self.edits.append(name)
        self.name = name


class ForbiddenGuild(FakeGuild):
    async def edit(self, name=None, **kw):
        raise discord.Forbidden(type("R", (), {"status": 403, "reason": "Forbidden"})(), "no perms")


class FakeMessage:
    def __init__(self):
        self.pinned = False

    async def pin(self, **kw):
        self.pinned = True


class Channel:
    def __init__(self, messages=None):
        self.messages = messages or []
        self.sent = []

    async def send(self, content=None, **kw):
        self.sent.append(content)
        return FakeMessage()

    def history(self, limit=None):
        async def gen():
            for m in self.messages:
                yield m
        return gen()


class FakeClient:
    def __init__(self, guild=None, channel=None):
        self._guild = guild
        self._channel = channel

    def get_guild(self, gid):
        return self._guild

    def get_channel(self, cid):
        return self._channel


# Helpers to build fake forward messages / reactions for scoring tests.
def ref(message_id, rtype):
    return type("Ref", (), {"message_id": message_id, "type": rtype})()


def _fake_user(uid, is_bot=False):
    return type("User", (), {"id": uid, "bot": is_bot})()


class _Reaction:
    """Fake reaction exposing both the raw count/me and an async users() list."""

    def __init__(self, count, me, people):
        self.count = count
        self.me = me
        self._people = people

    def users(self):
        async def gen():
            for u in self._people:
                yield u
        return gen()


def reaction(count, me=False, user_ids=None):
    """
    Fake reaction. By default `users()` yields *count* distinct humans (plus the
    bot when me=True), so a raw-count test and a unique-reactor test agree. Pass
    *user_ids* to control exactly who reacted, e.g. the same person across
    several emoji.
    """
    if user_ids is None:
        people = [_fake_user(i) for i in range(1, (count - 1 if me else count) + 1)]
        if me:
            people.append(_fake_user(0, is_bot=True))
    else:
        people = [_fake_user(u) for u in user_ids]
    return _Reaction(count, me, people)


def forward_msg(reference, reactions):
    return type("Msg", (), {"reference": reference, "reactions": reactions})()
