import bestof


def test_split_spaced_and_unspaced():
    assert bestof.split_emoji("🔁 ⏰") == ["🔁", "⏰"]
    assert bestof.split_emoji("🔁⏰") == ["🔁", "⏰"]


def test_split_keeps_multi_codepoint_emoji_whole():
    assert bestof.split_emoji("👍🏽") == ["👍🏽"]                  # skin tone
    assert bestof.split_emoji("🇺🇸🇬🇧") == ["🇺🇸", "🇬🇧"]          # flags pair up
    assert bestof.split_emoji("1️⃣") == ["1️⃣"]                    # keycap
    assert bestof.split_emoji("👨‍👩‍👧") == ["👨‍👩‍👧"]                  # zero-width-joiner family


def test_split_custom_emoji():
    assert bestof.split_emoji("<:yoba:123>🔁 <a:spin:456>") == ["<:yoba:123>", "🔁", "<a:spin:456>"]


def _usable(eid):
    return eid == 123


def test_parse_accepts_emoji_and_dedupes():
    assert bestof.parse_repeat("🔁 ⏰", _usable) == (["🔁", "⏰"], None)
    assert bestof.parse_repeat("🔁 🔁 ⏰", _usable) == (["🔁", "⏰"], None)
    assert bestof.parse_repeat("<:yoba:123>", _usable) == (["<:yoba:123>"], None)


def test_parse_rejects_bad_input():
    for text in ("alarm", ":alarm_clock:", "", "   ", "é"):
        chosen, err = bestof.parse_repeat(text, _usable)
        assert chosen == [] and err, text
    assert "alarm" in bestof.parse_repeat("🔁 alarm", _usable)[1]       # names the bad word


def test_parse_rejects_unusable_custom_and_too_many():
    assert bestof.parse_repeat("<:nope:999>", _usable)[1]               # bot can't use it
    assert bestof.parse_repeat("🔁 ⏰ 👍 🎉", _usable)[1]                # over the limit of 3


def test_repeat_reactions_default_and_stored():
    assert bestof.repeat_reactions({"repeat_reactions": None}) == ["🔁"]
    assert bestof.repeat_reactions({"repeat_reactions": "🔁 ⏰"}) == ["🔁", "⏰"]


def test_display_renders_custom_as_name():
    assert bestof.display(["🔁", "<:yoba:123>"]) == "🔁 :yoba:"


def test_instructions_name_the_configured_repeat():
    text = bestof.instructions({"repeat_reactions": "⏰"})
    assert text.startswith(bestof.INSTRUCTIONS_HEADER)
    assert "⏰" in text and "🔁" not in text
