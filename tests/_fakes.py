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


def reaction(count, me=False):
    return type("Reaction", (), {"count": count, "me": me})()


def forward_msg(reference, reactions):
    return type("Msg", (), {"reference": reference, "reactions": reactions})()
