"""
Content blocklist for the server-rename quotes.

Keeps the server name and rename cards from being set to hate speech. A bundled
default list (data/blocklist.txt, slurs/hate terms only, not broad profanity)
plus an optional per-guild list. Matching is whole-word and case-insensitive so
innocent substrings ("assassin", "Scunthorpe") aren't caught. It's a blunt
instrument by design: it catches the obvious, not every leetspeak evasion.
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
