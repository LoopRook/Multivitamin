import image_utils as iu


def test_choose_font_routes_unrenderable_glyphs_to_cjk():
    fonts = iu.load_fonts(32)
    names = iu.font_names
    # Regression: DejaVu draws a .notdef box for Korean jamo (ㆍ, U+318D) but the
    # old bbox-only check accepted it, so the name rendered as tofu. It must now
    # fall through to the CJK font that actually has the glyph.
    def picked(text):
        return names[fonts.index(iu.choose_font(text, fonts, names))]

    assert picked("Aphelion ㆍ") == "NotoSansCJK"
    assert picked("日本語") == "NotoSansCJK"
    assert picked("Virillium") == "DejaVuSans"     # plain Latin stays fast-path
