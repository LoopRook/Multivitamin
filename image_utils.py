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
    """(panel_bg, label_colour) from an accent, or a neutral pair when accent is None."""
    if accent is None:
        return (18, 17, 16), (232, 227, 219)
    h, s, _ = colorsys.rgb_to_hsv(*(c / 255 for c in accent))
    pr, pg, pb = colorsys.hsv_to_rgb(h, min(s, 0.6), 0.11)     # dark tinted panel
    lr, lg, lb = colorsys.hsv_to_rgb(h, min(s, 0.85), 0.96)    # bright label
    return (int(pr * 255), int(pg * 255), int(pb * 255)), (int(lr * 255), int(lg * 255), int(lb * 255))


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


_LABEL_TRACKING = 1   # letter-spacing (px) for the uppercase QUOTE/ICON labels


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
        panel, label_col = _palette(accent)

        base = Image.new("RGBA", (W, H), panel + (255,))
        draw = ImageDraw.Draw(base)

        # Icon: square, rounded corners, left.
        iso = 400
        sq = _square(icon, iso)
        mask = Image.new("L", (iso, iso), 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, iso, iso), radius=28, fill=255)
        base.paste(sq, (24, 25), mask)

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

        # Credits: QUOTE (left) and ICON (right) columns, fixed near the bottom.
        lab_font = _archivo(13, 600)
        col_w = (tw - 16) // 2
        for label, name, cx in (("QUOTE", quote_user, tx), ("ICON", icon_user, tx + col_w + 16)):
            _draw_tracked(draw, (cx, 344), label, lab_font, label_col, _LABEL_TRACKING)
            nfont = _text_font(18, 500, name or "Unknown")
            shown = _fit_width(draw, name or "Unknown", nfont, col_w)
            draw.text((cx, 362), shown, font=nfont, fill=(236, 231, 223))

        # Year-progress bar: dating element, playhead at today.
        by = 410
        frac = _year_fraction(datetime.date.today())
        px = tx + int(tw * frac)
        draw.line([(tx, by), (tx_r, by)], fill=(255, 255, 255, 40), width=3)
        draw.line([(tx, by), (px, by)], fill=label_col, width=3)
        draw.ellipse([px - 4, by - 4, px + 4, by + 4], fill=(247, 241, 228))
        small = _archivo(12, 500)
        today = datetime.date.today()
        draw.text((tx, by + 8), "Jan", font=small, fill=(150, 144, 135))
        draw.text((tx_r, by + 8), "Dec", font=small, anchor="ra", fill=(150, 144, 135))
        draw.text((px, by + 8), f"{today.strftime('%b')} {today.day}",
                  font=small, anchor="ma", fill=(224, 217, 205))

        buf = BytesIO()
        base.save(buf, format="PNG")
        buf.seek(0)
        return buf
    except Exception as e:
        log.error("Image generation failed: %s", e)
        return None
