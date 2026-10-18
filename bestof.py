"""
Best-of channel nomination reactions.

When a member forwards a rename card into the best-of channel, the bot reacts
ℹ️ for a counted nomination, and with the server's repeat reaction (default 🔁)
when that rename was already nominated. Admins choose the repeat reaction with
/config bestof: up to three emoji, unicode or custom ones from a server the bot
is in. Pure helpers, so main.py and bot_features.py can share them.
"""
import re

COUNTED = "ℹ️"
DEFAULT_REPEAT = ("🔁",)
MAX_REPEAT = 3
INSTRUCTIONS_HEADER = "📌 **Best-of nominations**"

_CUSTOM_RE = re.compile(r"<a?:(\w+):(\d+)>")

# Code point ranges that hold emoji (arrows and symbols through dingbats, the
# pictograph planes, and the handful of older symbols Discord treats as emoji).
_EMOJI_RANGES = (
    (0x00A9, 0x00A9), (0x00AE, 0x00AE), (0x203C, 0x203C), (0x2049, 0x2049),
    (0x20E3, 0x20E3), (0x2122, 0x2122), (0x2139, 0x2139), (0x2190, 0x2BFF),
    (0x3030, 0x3030), (0x303D, 0x303D), (0x3297, 0x3297), (0x3299, 0x3299),
    (0x1F000, 0x1FAFF),
)


def _attaches(cp: int) -> bool:
    """Marks that belong to the emoji before them: variation selectors, the
    keycap mark, skin tones and tag characters."""
    return (cp in (0xFE0E, 0xFE0F, 0x20E3)
            or 0x1F3FB <= cp <= 0x1F3FF
            or 0xE0020 <= cp <= 0xE007F)


def _is_regional(cp: int) -> bool:
    return 0x1F1E6 <= cp <= 0x1F1FF


def _split_unicode(text: str) -> list[str]:
    """Split a run of unicode emoji into single emoji, keeping skin tones,
    keycaps, flags and zero-width-joiner sequences whole."""
    clusters: list[str] = []
    after_zwj = False   # the previous character was a zero-width joiner
    flag_open = False   # a lone regional indicator waiting for its pair
    for ch in text:
        cp = ord(ch)
        if ch.isspace():
            clusters.append("")         # nothing joins across a space
            after_zwj = flag_open = False
            continue
        joins = _attaches(cp) or cp == 0x200D or after_zwj or (flag_open and _is_regional(cp))
        if clusters and clusters[-1] and joins:
            clusters[-1] += ch
            if _is_regional(cp):
                flag_open = False
        else:
            clusters.append(ch)
            flag_open = _is_regional(cp)
        after_zwj = cp == 0x200D
    return [c for c in clusters if c]


def split_emoji(text: str) -> list[str]:
    """Split admin input into single emoji: custom <:name:id> tokens and unicode
    emoji, with or without spaces between them."""
    out, pos = [], 0
    for m in _CUSTOM_RE.finditer(text):
        out += _split_unicode(text[pos:m.start()])
        out.append(m.group(0))
        pos = m.end()
    return out + _split_unicode(text[pos:])


def _looks_like_emoji(token: str) -> bool:
    return any(lo <= ord(ch) <= hi for ch in token for lo, hi in _EMOJI_RANGES)


def parse_repeat(text: str, can_use_custom) -> tuple[list[str], str | None]:
    """
    Validate admin input for the repeat reaction. Returns (emoji, None), or
    ([], reason) when something can't be used. *can_use_custom(emoji_id)* says
    whether the bot can react with that custom emoji.
    """
    # Report typed words whole ("alarm", ":alarm_clock:") rather than letter by letter.
    word = next((w for w in _CUSTOM_RE.sub(" ", text).split() if re.search(r"[A-Za-z]", w)), None)
    if word:
        return [], f"`{word}` isn't an emoji. Pick them from the emoji picker, e.g. `🔁 ⏰`."
    tokens = split_emoji(text)
    if not tokens:
        return [], "Give at least one emoji, e.g. `🔁 ⏰`."
    chosen: list[str] = []
    for tok in tokens:
        m = _CUSTOM_RE.fullmatch(tok)
        if m:
            if not can_use_custom(int(m.group(2))):
                return [], f"I can't use {tok}. Custom emoji have to come from a server I'm in."
        elif not _looks_like_emoji(tok):
            return [], f"`{tok}` isn't an emoji. Pick them from the emoji picker, e.g. `🔁 ⏰`."
        if tok not in chosen:
            chosen.append(tok)
    if len(chosen) > MAX_REPEAT:
        return [], f"That's {len(chosen)} emoji. The limit is {MAX_REPEAT}."
    return chosen, None


def repeat_reactions(cfg) -> list[str]:
    """The emoji to react with on a repeat nomination (default 🔁)."""
    stored = cfg["repeat_reactions"] if "repeat_reactions" in cfg.keys() else None
    return (stored or "").split() or list(DEFAULT_REPEAT)


def display(emoji: list[str]) -> str:
    """Readable inside a code block, where custom emoji can't render: <:name:id> -> :name:."""
    return " ".join(_CUSTOM_RE.sub(r":\1:", e) for e in emoji)


def instructions(cfg) -> str:
    """The pinned how-to for the best-of channel, naming this server's reactions."""
    return (
        f"{INSTRUCTIONS_HEADER}\n"
        "**Forward** your favorite rename cards into this channel to nominate them for the bracket "
        "(use Discord's **Forward** button; screenshots and re-uploads don't count).\n"
        f"• I'll react {COUNTED} when a forward is counted, or {' '.join(repeat_reactions(cfg))} "
        "if that rename was already forwarded here.\n"
        "• **React** to the forwards you like. The most-reacted renames get seeded into the bracket."
    )
