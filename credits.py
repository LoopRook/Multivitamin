"""
Contributor credits — one place that decides how a person is named.

Names are stored alongside every rename post and bracket entry as a snapshot,
but a snapshot goes stale the moment someone changes their nickname. Whenever a
user id is known we re-resolve the name live from the guild, so the card, the
matchup embeds, the champion announcement and /bracket history all agree.

Two knobs, both per-guild config:
  credit_style    'nickname' (server nick, falling back to their display name)
                  'username' (their Discord @username — stable across servers)
  credit_mentions 1 = render as a real <@id> mention in text posts
"""
from __future__ import annotations

import discord

NAME_STYLES = ("nickname", "username")
_UNKNOWN = "Unknown"


def style_of(cfg) -> str:
    """The guild's credit style, defaulting to 'nickname' for legacy rows."""
    style = (cfg["credit_style"] if "credit_style" in cfg.keys() else None) or "nickname"
    return style if style in NAME_STYLES else "nickname"


def mentions_on(cfg) -> bool:
    return bool(cfg["credit_mentions"] if "credit_mentions" in cfg.keys() else 0)


def resolve_name(
    client: discord.Client, guild_id: int, uid: int | None, stored: str | None, style: str,
) -> str:
    """
    Live name for *uid* in the guild's chosen style.

    Falls back to the *stored* snapshot when the member has left, isn't cached,
    or we never recorded a uid — a stale name beats no name at all.
    """
    if uid:
        guild = client.get_guild(guild_id)
        member = guild.get_member(uid) if guild else None
        if member is not None:
            return member.name if style == "username" else member.display_name
    return stored or _UNKNOWN


def credit(
    client: discord.Client, guild_id: int, uid: int | None, stored: str | None,
    cfg, *, mention: bool = True,
) -> str:
    """
    A contributor's credit for use in a text post or embed.

    Renders an @mention when the guild enabled mentions AND we know the uid AND
    the caller allows it (cards are images — they can never mention).
    """
    if mention and uid and mentions_on(cfg):
        return f"<@{uid}>"
    return resolve_name(client, guild_id, uid, stored, style_of(cfg))


def credit_line(
    client: discord.Client, guild_id: int, cfg, *,
    quote_user: str | None, quote_uid: int | None,
    icon_user: str | None = None, icon_uid: int | None = None,
    prefix: str = "submitted by", mention: bool = True,
) -> str:
    """
    "submitted by X · icon by Y" — the icon half is dropped when unknown, which
    is the case for every rename posted before icon credits were recorded.
    """
    who  = credit(client, guild_id, quote_uid, quote_user, cfg, mention=mention)
    line = f"{prefix} {who}" if prefix else who
    if icon_user or icon_uid:
        line += f" · icon by {credit(client, guild_id, icon_uid, icon_user, cfg, mention=mention)}"
    return line
