from PIL import Image, ImageDraw, ImageFont, ImageFilter
from io import BytesIO
import string

def load_fonts(size):
    fonts = []
    paths = [
        "DejaVuSans-Bold.ttf",
        "NotoSansCJK-Bold.ttc"
    ]
    for path in paths:
        try:
            font = ImageFont.truetype(path, size)
            fonts.append(font)
        except Exception as e:
            print(f"❌ Failed to load font '{path}': {e}")
            fonts.append(None)
    return fonts

font_names = ["DejaVuSans", "NotoSansCJK"]

def is_pure_ascii(text):
    return all(c in string.ascii_letters + string.digits + string.punctuation + " " for c in text)

def choose_font(text, fonts, names):
    if is_pure_ascii(text) and fonts[0] is not None:
        return fonts[0]
    for font, name in zip(fonts, names):
        if font and can_render_all(text, font, name):
            return font
    return fonts[0]

def can_render_all(text, font, name):
    try:
        for char in text:
            if char == ' ':
                continue
            if not font.getmask(char).getbbox():
                print(f"❌ Font '{name}' cannot render '{char}' (U+{ord(char):04X})")
                return False
        print(f"✅ Font '{name}' supports full string: \"{text}\"")
        return True
    except Exception as e:
        print(f"❌ Exception checking '{name}': {e}")
        return False

def truncate_to_100_chars(text):
    return text if len(text) <= 100 else text[:97].rsplit(' ', 1)[0] + '...'

async def generate_card(server_name, quote_user, icon_user, icon_bytes):
    try:
        base = Image.new("RGBA", (800, 450), (0, 0, 0, 255))
        icon = Image.open(BytesIO(icon_bytes)).convert("RGBA").resize((400, 400))
        blurred_bg = icon.resize((800, 450)).filter(ImageFilter.GaussianBlur(12))
        base.paste(blurred_bg, (0, 0))

        mask = Image.new("L", (400, 400), 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, 400, 400), radius=40, fill=255)
        icon.putalpha(mask)
        base.paste(icon, (25, 25), icon)

        draw = ImageDraw.Draw(base)
        title_fonts = load_fonts(36)
        meta_fonts = load_fonts(24)
        words, line, lines = server_name.split(), "", []
        for word in words:
            test = f"{line} {word}".strip()
            font = choose_font(test, title_fonts, font_names) or title_fonts[0]
            if draw.textlength(test, font=font) < 300:
                line = test
            else:
                lines.append(line)
                line = word
        lines.append(line)

        y_text = 80
        for line in lines:
            font = choose_font(line, title_fonts, font_names) or title_fonts[0]
            draw.text((450, y_text), line, font=font, fill=(255, 255, 255))
            y_text += 40

        def render_meta(label, name, offset):
            font = choose_font(name, meta_fonts, font_names)
            if not font or not can_render_all(name, font, label):
                print(f"⚠️ Fallback: {label} '{name}' has unsupported glyphs, using 'Unknown'")
                name = "Unknown"
                font = meta_fonts[0]
            color = (200, 200, 200) if label == "Quote by" else (180, 180, 180)
            draw.text((450, offset), f"{label}: {name}", font=font, fill=color)

        render_meta("Quote by", quote_user, y_text + 10)
        render_meta("Icon by", icon_user, y_text + 50)

        buffer = BytesIO()
        base.save(buffer, format="PNG")
        buffer.seek(0)
        return buffer
    except Exception as e:
        print(f"❌ Image generation failed: {e}")
        return None
