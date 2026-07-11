import moderation


def test_default_list_loaded():
    assert len(moderation.DEFAULT_BLOCKLIST) > 20


def test_blocks_slurs_case_insensitive():
    assert moderation.is_blocked("you are a nigger")
    assert moderation.is_blocked("NIGGER")
    assert moderation.is_blocked("what a retard")


def test_whole_word_only_no_scunthorpe():
    # innocent words that merely CONTAIN a blocked substring must pass
    assert not moderation.is_blocked("the raccoon ate it")        # 'coon'
    assert not moderation.is_blocked("a conspicuous choice")      # 'spic'
    assert not moderation.is_blocked("the assassin struck")       # no false hit
    assert not moderation.is_blocked("the printer is jammed")


def test_custom_extra_words():
    assert moderation.is_blocked("ban frobnicate now", {"frobnicate"})
    assert not moderation.is_blocked("ban frobnicate now")        # only with the extra set


def test_parse_custom_roundtrip():
    assert moderation.parse_custom(" Foo, bar ,, BAZ ") == {"foo", "bar", "baz"}
    assert moderation.parse_custom(None) == set()
    assert moderation.parse_custom("") == set()


def test_empty_text():
    assert not moderation.is_blocked("")
    assert not moderation.is_blocked(None)


def test_strip_custom_emoji():
    # the reported case: a custom emoji shortcode plus real text
    assert moderation.strip_emoji("<:hazMonka:6882587832768> No.") == "No."
    assert moderation.strip_emoji("<a:spin:12345> hype") == "hype"


def test_strip_unicode_emoji():
    assert moderation.strip_emoji("lol 😂 no") == "lol no"
    assert moderation.strip_emoji("great job 👍🏽") == "great job"
    assert moderation.strip_emoji("flag 🇺🇸 here") == "flag here"


def test_emoji_only_cleans_to_empty():
    assert moderation.strip_emoji("<:hazMonka:6882587832768>") == ""
    assert moderation.strip_emoji("😂😂😂") == ""
    assert moderation.strip_emoji("  🎉  ") == ""
    assert moderation.strip_emoji(None) == ""
    assert moderation.strip_emoji("") == ""


def test_strip_emoji_preserves_ordinary_text():
    # plain punctuation, arrows and symbols people actually mean must survive
    assert moderation.strip_emoji("2 + 2 = 4") == "2 + 2 = 4"
    assert moderation.strip_emoji("go left -> then right") == "go left -> then right"
    assert moderation.strip_emoji("café façade naïve") == "café façade naïve"
