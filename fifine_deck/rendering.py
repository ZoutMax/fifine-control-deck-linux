"""
Key image rendering. Composes a background colour, an optional icon, and an
optional text label into a square key image. Used both to push JPEGs to the
device and to draw the live preview in the GUI.
"""
from __future__ import annotations

import io
import os
from functools import lru_cache

from PIL import Image, ImageDraw, ImageFont, ImageEnhance, ImageFilter

_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
]


@lru_cache(maxsize=64)
def _font(size: int) -> ImageFont.FreeTypeFont:
    for path in _FONT_CANDIDATES:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default()


def _hex(color: str, fallback=(16, 16, 32)):
    try:
        c = color.lstrip("#")
        if len(c) == 3:
            c = "".join(ch * 2 for ch in c)
        return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))
    except (ValueError, IndexError):
        return fallback


def render_key(
    size: int,
    label: str = "",
    icon_path: str = "",
    bg_color: str = "#101020",
    text_color: str = "#ffffff",
    pressed: bool = False,
) -> Image.Image:
    """Return an RGB PIL image (size x size), upright (no device rotation yet).
    If pressed, apply a glow (brighten + soft border) for on-device feedback."""
    img = Image.new("RGB", (size, size), _hex(bg_color))

    if icon_path and os.path.exists(icon_path):
        try:
            icon = Image.open(icon_path).convert("RGBA")
            pad = int(size * 0.08)
            box = size - 2 * pad
            # If there's also a label, leave room at the bottom.
            if label:
                box = int(box * 0.72)
            icon.thumbnail((box, box), Image.LANCZOS)
            x = (size - icon.width) // 2
            y = pad if label else (size - icon.height) // 2
            img.paste(icon, (x, y), icon)
        except OSError:
            pass

    if label:
        draw = ImageDraw.Draw(img)
        fs = max(10, int(size * (0.20 if icon_path else 0.24)))
        font = _font(fs)
        # wrap to fit width
        lines = _wrap(draw, label, font, size - 8)
        line_h = fs + 2
        total_h = line_h * len(lines)
        y0 = (size - total_h) if icon_path else (size - total_h) // 2
        y0 = max(0, min(y0, size - total_h))
        tcol = _hex(text_color, (255, 255, 255))
        for i, line in enumerate(lines):
            bb = draw.textbbox((0, 0), line, font=font)
            w = bb[2] - bb[0]
            x = (size - w) // 2 - bb[0]
            y = y0 + i * line_h
            # subtle shadow for legibility over icons/backgrounds
            draw.text((x + 1, y + 1), line, font=font, fill=(0, 0, 0))
            draw.text((x, y), line, font=font, fill=tcol)

    if pressed:
        img = _apply_glow(img)
    return img


def _apply_glow(img: Image.Image) -> Image.Image:
    """Pressed-key feedback: a symmetric glowing halo around the button border.
    The key content is left as-is; a soft blurred ring plus a crisp bright edge
    hug the border evenly on all sides."""
    size = img.width
    m = max(2, int(size * 0.03))               # equal margin on all sides
    rad = int(size * 0.14)
    box = [m, m, size - 1 - m, size - 1 - m]
    out = img.convert("RGBA")

    # soft, evenly-blurred halo following the border
    glow = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ImageDraw.Draw(glow).rounded_rectangle(
        box, radius=rad, outline=(80, 200, 255, 255), width=max(4, int(size * 0.07)))
    glow = glow.filter(ImageFilter.GaussianBlur(max(2, int(size * 0.045))))
    out = Image.alpha_composite(out, glow)

    # crisp bright ring for a defined halo edge
    ring = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ImageDraw.Draw(ring).rounded_rectangle(
        box, radius=rad, outline=(210, 240, 255, 255), width=max(1, int(size * 0.02)))
    out = Image.alpha_composite(out, ring)
    return out.convert("RGB")


def _wrap(draw, text, font, max_w):
    words = text.split()
    if not words:
        return [text]
    lines, cur = [], words[0]
    for w in words[1:]:
        trial = cur + " " + w
        if draw.textbbox((0, 0), trial, font=font)[2] <= max_w:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    lines.append(cur)
    return lines[:3]


def pil_to_qimage(image: Image.Image):
    """Convert a PIL image to a QImage (deep copy so it owns its buffer)."""
    from PyQt6.QtGui import QImage
    im = image.convert("RGBA")
    data = im.tobytes("raw", "RGBA")
    qimg = QImage(data, im.width, im.height, QImage.Format.Format_RGBA8888)
    return qimg.copy()


def to_device_jpeg(image: Image.Image, rotation: int = 0,
                   flip=(False, False), quality: int = 90) -> bytes:
    """Apply device orientation and return JPEG bytes ready for the transport."""
    if rotation:
        image = image.rotate(rotation)
    if flip[0]:
        image = image.transpose(Image.FLIP_LEFT_RIGHT)
    if flip[1]:
        image = image.transpose(Image.FLIP_TOP_BOTTOM)
    buf = io.BytesIO()
    image.convert("RGB").save(buf, format="JPEG", quality=quality)
    return buf.getvalue()
