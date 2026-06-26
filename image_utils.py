import logging
import string
from io import BytesIO

from PIL import Image, ImageDraw, ImageFont, ImageFilter

log = logging.getLogger(__name__)

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
            fonts.append(ImageFont.truetype(path, size))
        except Exception as e:
            log.warning("Failed to load font '%s': %s", path, e)
            fonts.append(None)
    _font_cache[size] = fonts
    return fonts


def is_pure_ascii(text: str) -> bool:
    return all(c in _ASCII_SET for c in text)


def can_render_all(text: str, font, name: str) -> bool:
    """Return True if *font* can render every character in *text*."""
    try:
        for char in text:
            if char in (" ", "\t", "\n"):
                continue
            if not font.getmask(char).getbbox():
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


async def generate_card(
    server_name: str,
    quote_user: str,
    icon_user: str,
    icon_bytes: bytes,
):
    try:
        title_fonts = load_fonts(36)
        meta_fonts = load_fonts(24)

        base = Image.new("RGBA", (800, 450), (0, 0, 0, 255))
        icon = Image.open(BytesIO(icon_bytes)).convert("RGBA").resize((400, 400))
        blurred_bg = icon.resize((800, 450)).filter(ImageFilter.GaussianBlur(12))
        base.paste(blurred_bg, (0, 0))

        mask = Image.new("L", (400, 400), 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, 400, 400), radius=40, fill=255)
        icon.putalpha(mask)
        base.paste(icon, (25, 25), icon)

        draw = ImageDraw.Draw(base)

        # Word-wrap server name to fit within 300 px
        words = server_name.split()
        line, lines = "", []
        for word in words:
            test = f"{line} {word}".strip()
            font = choose_font(test, title_fonts, font_names) or title_fonts[0]
            if font and draw.textlength(test, font=font) < 300:
                line = test
            else:
                if line:
                    lines.append(line)
                line = word
        if line:
            lines.append(line)

        y_text = 80
        for ln in lines:
            font = choose_font(ln, title_fonts, font_names) or title_fonts[0]
            if font:
                draw.text((450, y_text), ln, font=font, fill=(255, 255, 255))
            y_text += 40

        def render_meta(label: str, name: str, offset: int) -> None:
            font = choose_font(name, meta_fonts, font_names)
            if not font or not can_render_all(name, font, label):
                log.warning("Falling back to 'Unknown' for %s '%s' (unsupported glyphs)", label, name)
                name = "Unknown"
                font = meta_fonts[0]
            if not font:
                return
            color = (200, 200, 200) if label == "Quote by" else (180, 180, 180)
            draw.text((450, offset), f"{label}: {name}", font=font, fill=color)

        render_meta("Quote by", quote_user, y_text + 10)
        render_meta("Icon by", icon_user, y_text + 50)

        buf = BytesIO()
        base.save(buf, format="PNG")
        buf.seek(0)
        return buf
    except Exception as e:
        log.error("Image generation failed: %s", e)
        return None
