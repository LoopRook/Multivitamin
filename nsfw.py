"""
Age-restricted content: keep posts from NSFW channels inside NSFW channels.

The bot can't see what's in a quote, image, or link, so the channel's
age-restricted flag is the only signal it has. With the guard on (the default):
  - a custom feature won't repost from an age-restricted source channel into one
    that isn't (checked against wherever it's actually posting, so /meme and
    /preview count too);
  - renames never use an age-restricted quote or icon channel, because the server
    name and icon are visible to everyone, including on invites.

Per-guild config: nsfw_guard 1 = on (default), 0 = post anywhere. Only Manage
Server can turn it off; see /config nsfw.
"""
from __future__ import annotations


def guard_on(cfg) -> bool:
    """Whether the guard is on, defaulting to on for legacy rows."""
    value = cfg["nsfw_guard"] if "nsfw_guard" in cfg.keys() else None
    return value is None or bool(value)


def is_age_restricted(channel) -> bool:
    """The channel's NSFW flag (threads inherit their parent's). False if unknown."""
    check = getattr(channel, "is_nsfw", None)
    return bool(check()) if callable(check) else False


def feature_block_reason(cfg, source, dest, name: str) -> str | None:
    """Why feature *name* may not post from *source* into *dest*, or None if it may."""
    if not guard_on(cfg) or not is_age_restricted(source) or is_age_restricted(dest):
        return None
    return (f"**{name}** skipped: {source.mention} is age-restricted but {dest.mention} isn't. "
            "Post it in an age-restricted channel, or change this with `/config nsfw`.")


def rename_block_reason(cfg, quote_channel, icon_channel) -> str | None:
    """Why a rename may not draw from these channels, or None if it may."""
    if not guard_on(cfg):
        return None
    for channel in (quote_channel, icon_channel):
        if is_age_restricted(channel):
            return (f"Rename skipped: {channel.mention} is age-restricted, and the server name "
                    "and icon are visible to everyone. Pick another channel in `/setup`, "
                    "or change this with `/config nsfw`.")
    return None
