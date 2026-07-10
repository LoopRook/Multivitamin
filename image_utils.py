import colorsys
import datetime
import logging
import os
import string
from io import BytesIO

from PIL import Image, ImageDraw, ImageFont, ImageFilter

log = logging.getLogger(__name__)

# Fonts live in ./fonts next to this module (resolved absolutely so it works
# regardless of the process working directory).
_FONTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")

_FONT_PATHS = [
    ("DejaVuSans", "DejaVuSans-Bold.ttf"),
    ("NotoSansCJK", "NotoSansCJK-Bold.ttc"),
]

# Cache fonts at module load time — load_fonts() was being called on every
# card generation (potentially dozens per day per guild), paying the file-open
# cost each time. Cache by size so the TTF is opened exactly once.
_font_cache: dict[int, list] = {}

font_names = [name for name, _ in _FONT_PATHS]

_ASCII_SET = frozenset(string.ascii_letters + string.digits + string.punctuation + " ")


def load_fonts(size: int) -> list:
    """Return a list of ImageFont objects for *size*, loading from disk only once."""
    if size in _font_cache:
        return _font_cache[size]
    fonts = []
    for name, path in _FONT_PATHS:
        try:
            fonts.append(ImageFont.truetype(os.path.join(_FONTS_DIR, path), size))
        except Exception as e:
            log.warning("Failed to load font '%s': %s", path, e)
            fonts.append(None)
    _font_cache[size] = fonts
    return fonts


def is_pure_ascii(text: str) -> bool:
    return all(c in _ASCII_SET for c in text)


# A Unicode noncharacter: no font has a real glyph for it, so it always renders
# as the font's .notdef box. We compare every char's glyph against it to detect
# a missing glyph — a bare bbox check can't, because .notdef (the tofu box) has
# ink too, which made DejaVu wrongly "pass" for e.g. Korean jamo and draw tofu.
_NOTDEF_PROBE = "￾"


def can_render_all(text: str, font, name: str) -> bool:
    """Return True only if *font* has a real glyph for every char in *text*."""
    try:
        notdef = bytes(font.getmask(_NOTDEF_PROBE))
        for char in text:
            if char in (" ", "\t", "\n"):
                continue
            mask = font.getmask(char)
            if not mask.getbbox():
                return False
            if bytes(mask) == notdef:  # font lacks this glyph -> would draw tofu
                return False
        return True
    except Exception:
        return False


def choose_font(text: str, fonts: list, names: list):
    """Pick the first font that can render *text*, ASCII-fast-path first."""
    if is_pure_ascii(text) and fonts[0] is not None:
        return fonts[0]
    for font, name in zip(fonts, names):
        if font and can_render_all(text, font, name):
            return font
    return fonts[0]  # last-resort fallback


def truncate_to_100_chars(text: str) -> str:
    if len(text) <= 100:
        return text
    return text[:97].rsplit(" ", 1)[0] + "..."


# ── Card rendering ────────────────────────────────────────────────────────────
# Share-card layout (icon square left, text panel right) with a panel tinted from
# a colour sampled out of the icon, editorial QUOTE/ICON credit labels, and a
# year-progress bar as a dating element. No branding — the card is always posted
# by the bot, so Discord already shows its name and avatar above the message.

_ARCHIVO = "Archivo-VariableFont_wdth,wght.ttf"
_archivo_cache: dict[tuple, ImageFont.FreeTypeFont] = {}


def _archivo(size: int, weight: int) -> ImageFont.FreeTypeFont:
    """Archivo at a given size and weight (100-900), cached. Falls back to DejaVu."""
    key = (size, weight)
    if key not in _archivo_cache:
        try:
            f = ImageFont.truetype(os.path.join(_FONTS_DIR, _ARCHIVO), size)
            f.set_variation_by_axes([weight, 100])   # axes are [Weight, Width]
        except Exception as e:
            log.warning("Archivo load/variation failed (%s); using DejaVu.", e)
            f = load_fonts(size)[0]
        _archivo_cache[key] = f
    return _archivo_cache[key]


def _text_font(size: int, weight: int, text: str):
    """Archivo at *weight* for Latin, falling back to the CJK font when needed."""
    cascade = [_archivo(size, weight), load_fonts(size)[1]]
    return choose_font(text, cascade, ["Archivo", "NotoSansCJK"]) or cascade[0]


def _sample_accent(img: Image.Image):
    """A vibrant (r,g,b) sampled from the image, or None if it's too gray to use."""
    small = img.convert("RGB").resize((48, 48))
    quant = small.quantize(colors=8)
    palette = quant.getpalette()
    best, best_score = None, 0.0
    for count, idx in (quant.getcolors() or []):
        r, g, b = palette[idx * 3:idx * 3 + 3]
        _, s, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
        score = s * min(v, 0.9) * (count ** 0.5)      # saturated, not too dark, common
        if score > best_score:
            best_score, best = score, (r, g, b, s)
    if not best or best[3] < 0.25:                     # too gray -> no usable accent
        return None
    return best[:3]


def _palette(accent):
    """(panel_lo, panel_hi, label_col). panel_hi is the brighter top of the panel gradient."""
    if accent is None:
        return (16, 15, 14), (28, 26, 24), (232, 227, 219)
    h, s, _ = colorsys.rgb_to_hsv(*(c / 255 for c in accent))

    def rgb(sat, val):
        r, g, b = colorsys.hsv_to_rgb(h, sat, val)
        return (int(r * 255), int(g * 255), int(b * 255))

    return rgb(min(s, 0.60), 0.10), rgb(min(s, 0.62), 0.20), rgb(min(s, 0.85), 0.96)


def _v_gradient(w: int, h: int, top, bot) -> Image.Image:
    """A vertical gradient image, *top* colour at the top fading to *bot* at the bottom."""
    strip = Image.new("RGB", (1, h))
    px = strip.load()
    for y in range(h):
        t = y / max(1, h - 1)
        px[0, y] = tuple(int(top[i] * (1 - t) + bot[i] * t) for i in range(3))
    return strip.resize((w, h))


def _bar_color(frac: float, accent, label_col):
    """Timeline colour at position *frac* (0=Jan .. 1=Dec): dim early, full accent by Dec."""
    if accent is None:
        dim = (86, 82, 76)
    else:
        h, s, _ = colorsys.rgb_to_hsv(*(c / 255 for c in accent))
        r, g, b = colorsys.hsv_to_rgb(h, min(s, 0.5), 0.45)
        dim = (int(r * 255), int(g * 255), int(b * 255))
    return tuple(int(dim[i] * (1 - frac) + label_col[i] * frac) for i in range(3))


def _square(img: Image.Image, s: int) -> Image.Image:
    """Center-crop to a square and resize (avoids distorting non-square icons)."""
    w, h = img.size
    m = min(w, h)
    img = img.crop(((w - m) // 2, (h - m) // 2, (w - m) // 2 + m, (h - m) // 2 + m))
    return img.resize((s, s))


def _wrap(draw, text, font, max_w):
    words, lines, line = text.split(), [], ""
    for w in words:
        test = f"{line} {w}".strip()
        if not line or draw.textlength(test, font=font) <= max_w:
            line = test
        else:
            lines.append(line)
            line = w
    if line:
        lines.append(line)
    return lines


def _fit_width(draw, text, font, max_w):
    """Truncate *text* with an ellipsis so it fits *max_w*."""
    if draw.textlength(text, font=font) <= max_w:
        return text
    while text and draw.textlength(text + "…", font=font) > max_w:
        text = text[:-1]
    return (text + "…") if text else ""


def _fit_name(draw, name, max_w, base=18, floor=14):
    """Shrink a credit name to fit *max_w* (down to *floor* px), then ellipsis if still too long."""
    for size in range(base, floor - 1, -1):
        font = _text_font(size, 500, name)
        if draw.textlength(name, font=font) <= max_w:
            return font, name
    font = _text_font(floor, 500, name)
    return font, _fit_width(draw, name, font, max_w)


_LABEL_TRACKING = 1     # letter-spacing (px) for the uppercase QUOTE/ICON labels
_PANEL_GRADIENT = True  # panel as a top-to-bottom gradient vs a flat tint
_BAR_GRADIENT = True    # timeline colour intensifies from Jan (dim) to Dec (full accent)
_ICON_INSET = 25        # icon margin from the card edge
_ICON_RADIUS = 28       # icon corner radius
_CARD_RADIUS = _ICON_INSET + _ICON_RADIUS   # concentric with the icon's corners


def _draw_tracked(draw, xy, text, font, fill, tracking):
    """draw.text with per-character letter-spacing (Pillow has no native tracking)."""
    x, y = xy
    for ch in text:
        draw.text((x, y), ch, font=font, fill=fill)
        x += draw.textlength(ch, font=font) + tracking


def _year_fraction(today: datetime.date) -> float:
    start = datetime.date(today.year, 1, 1)
    end = datetime.date(today.year + 1, 1, 1)
    return (today - start).days / (end - start).days


async def generate_card(
    server_name: str,      # the quote text
    quote_user: str,
    icon_user: str,
    icon_bytes: bytes,
):
    try:
        W, H = 800, 450
        icon = Image.open(BytesIO(icon_bytes)).convert("RGBA")
        accent = _sample_accent(icon)
        panel_lo, panel_hi, label_col = _palette(accent)

        base = Image.new("RGBA", (W, H), (0, 0, 0, 255))
        if _PANEL_GRADIENT:
            base.paste(_v_gradient(W, H, panel_hi, panel_lo), (0, 0))
        else:
            base.paste(Image.new("RGB", (W, H), panel_lo), (0, 0))
        draw = ImageDraw.Draw(base)

        # Icon: square, rounded corners, left. Inset + radius are concentric with the card.
        iso = H - 2 * _ICON_INSET
        sq = _square(icon, iso)
        mask = Image.new("L", (iso, iso), 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, iso, iso), radius=_ICON_RADIUS, fill=255)
        base.paste(sq, (_ICON_INSET, _ICON_INSET), mask)

        tx, tx_r = 452, W - 24        # text column left / right edges
        tw = tx_r - tx

        # Quote: largest heavy-weight size whose wrap fits the box (y 58..322).
        quote = (server_name or "").strip()
        avail_h = 322 - 58
        qfont, lines, line_h = None, None, 0
        for size in range(46, 23, -2):
            f = _text_font(size, 800, quote)
            wrapped = _wrap(draw, quote, f, tw)
            lh = size * 1.16
            if len(wrapped) * lh <= avail_h and all(draw.textlength(x, font=f) <= tw for x in wrapped):
                qfont, lines, line_h = f, wrapped, lh
                break
        if lines is None:
            qfont = _text_font(24, 800, quote)
            lines = _wrap(draw, quote, qfont, tw)[:8]
            line_h = 24 * 1.16

        y = 58 + max(0, (avail_h - len(lines) * line_h) / 2)   # vertically centered
        for ln in lines:
            draw.text((tx, y), ln, font=qfont, fill=(245, 242, 236))
            y += line_h

        # Credits: QUOTE (left) and ICON (right) columns. Both names share ONE font
        # size (the largest that fits both in their half-column) so they always
        # match — independently-sized names look off. Ellipsis only if it still
        # overflows at the floor.
        lab_font = _archivo(13, 600)
        col_w = (tw - 16) // 2
        q_name, i_name = quote_user or "Unknown", icon_user or "Unknown"
        name_size = 14
        for size in range(18, 13, -1):
            if all(draw.textlength(n, font=_text_font(size, 500, n)) <= col_w for n in (q_name, i_name)):
                name_size = size
                break
        for label, name, cx in (("QUOTE", q_name, tx), ("ICON", i_name, tx + col_w + 16)):
            _draw_tracked(draw, (cx, 344), label, lab_font, label_col, _LABEL_TRACKING)
            nfont = _text_font(name_size, 500, name)
            draw.text((cx, 362), _fit_width(draw, name, nfont, col_w), font=nfont, fill=(236, 231, 223))

        # Year-progress bar: dating element, playhead at today. Under _BAR_GRADIENT the
        # filled portion warms from dim (Jan) to full accent (Dec).
        by = 410
        frac = _year_fraction(datetime.date.today())
        px = tx + int(tw * frac)
        draw.line([(tx, by), (tx_r, by)], fill=(255, 255, 255, 40), width=3)   # track
        if _BAR_GRADIENT:
            for x in range(tx, px + 1):
                draw.line([(x, by - 1), (x, by + 1)], fill=_bar_color((x - tx) / max(1, tw), accent, label_col))
        else:
            draw.line([(tx, by), (px, by)], fill=label_col, width=3)
        draw.ellipse([px - 4, by - 4, px + 4, by + 4], fill=(247, 241, 228))
        # Only the date label, riding the playhead (clamped so it never overflows the ends).
        small = _archivo(12, 500)
        today = datetime.date.today()
        date_txt = f"{today.strftime('%b')} {today.day}"
        if px <= tx + 24:
            draw.text((tx, by + 8), date_txt, font=small, fill=(224, 217, 205))
        elif px >= tx_r - 24:
            draw.text((tx_r, by + 8), date_txt, font=small, anchor="ra", fill=(224, 217, 205))
        else:
            draw.text((px, by + 8), date_txt, font=small, anchor="ma", fill=(224, 217, 205))

        # Round the card's own corners (concentric with the icon).
        card_mask = Image.new("L", (W, H), 0)
        ImageDraw.Draw(card_mask).rounded_rectangle((0, 0, W - 1, H - 1), radius=_CARD_RADIUS, fill=255)
        base.putalpha(card_mask)

        buf = BytesIO()
        base.save(buf, format="PNG")
        buf.seek(0)
        return buf
    except Exception as e:
        log.error("Image generation failed: %s", e)
        return None
