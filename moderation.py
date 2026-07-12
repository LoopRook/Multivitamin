"""
Content cleaning for the server-rename quotes.

Two jobs, both keeping the server name and rename cards sane:

1. A blocklist so a name can't become hate speech. A bundled default list
   (data/blocklist.txt, slurs/hate terms only, not broad profanity) plus an
   optional per-guild list. Matching is whole-word and case-insensitive so
   innocent substrings ("assassin", "Scunthorpe") aren't caught. It's a blunt
   instrument by design: it catches the obvious, not every leetspeak evasion.

2. Emoji stripping, so a quote that is a Discord custom emoji (raw <:name:id>)
   or unicode emoji doesn't get baked verbatim into the server name and card
   (neither renders; the card has no emoji font). A quote that is nothing but
   emoji cleans to "" and is skipped upstream.
"""
import logging
import os
import re

log = logging.getLogger(__name__)

_DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "blocklist.txt")


def _load_default() -> frozenset:
    words = set()
    try:
        with open(_DATA, encoding="utf-8") as f:
            for line in f:
                term = line.strip().lower()
                if term and not term.startswith("#"):
                    words.add(term)
    except OSError as e:
        log.warning("Could not load default blocklist: %s", e)
    return frozenset(words)


DEFAULT_BLOCKLIST = _load_default()


def _compile(words) -> "re.Pattern | None":
    """One whole-word, case-insensitive alternation over *words* (longest first)."""
    if not words:
        return None
    alts = "|".join(re.escape(w) for w in sorted(words, key=len, reverse=True))
    # (?<!\w)…(?!\w) is a word boundary that also fires next to punctuation.
    return re.compile(rf"(?<!\w)(?:{alts})(?!\w)", re.IGNORECASE)


_DEFAULT_RE = _compile(DEFAULT_BLOCKLIST)


# Discord custom emoji, static <:name:id> or animated <a:name:id>.
_CUSTOM_EMOJI_RE = re.compile(r"<a?:\w+:\d+>")

# Unicode emoji. Deliberately conservative: the pictographic planes people
# actually type, plus their joiners/modifiers, but NOT every symbol block, so
# ordinary punctuation and arrows in a quote survive.
_UNICODE_EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001FAFF"   # pictographs, emoticons, transport, supplemental, extended-A/B
    "\U00002600-\U000027BF"   # misc symbols + dingbats (hearts, checks, scissors, ...)
    "\U0001F1E6-\U0001F1FF"   # regional indicators (flag letters)
    "\U00002B00-\U00002BFF"   # stars, arrows-as-emoji, geometric emoji
    "\U0000FE00-\U0000FE0F"   # variation selectors (emoji-presentation flag)
    "\U0000200D"              # zero-width joiner (multi-part emoji)
    "\U000020E3"              # combining enclosing keycap
    "]+"
)


def strip_emoji(text: str | None) -> str:
    """Remove Discord custom-emoji shortcodes and unicode emoji from *text*,
    collapsing the whitespace they leave behind. Emoji-only text cleans to ""."""
    if not text:
        return ""
    text = _CUSTOM_EMOJI_RE.sub(" ", text)
    text = _UNICODE_EMOJI_RE.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


def strip_custom_emoji(text: str | None) -> str:
    """Remove only Discord custom-emoji shortcodes (<:name:id>), keeping unicode
    emoji. For display in a monospace/code block, where custom shortcodes can't
    render but plain unicode emoji still show as a glyph."""
    if not text:
        return ""
    return _CUSTOM_EMOJI_RE.sub("", text).strip()


def parse_custom(csv: str | None) -> set:
    """Parse a stored comma-separated per-guild blocklist into a lowercase set."""
    return {w.strip().lower() for w in (csv or "").split(",") if w.strip()}


def is_blocked(text: str | None, extra: set | None = None) -> bool:
    """True if *text* contains a blocked term (default list + *extra*), whole-word."""
    if not text:
        return False
    if _DEFAULT_RE and _DEFAULT_RE.search(text):
        return True
    if extra:
        extra_re = _compile(extra)
        if extra_re and extra_re.search(text):
            return True
    return False
