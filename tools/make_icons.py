#!/usr/bin/env python3
"""
Generate the app icon and a library of action icons for fifine Control Deck.

All artwork is original (drawn here), matching the fifine/Stream Dock aesthetic:
rounded-square tiles, flat white glyphs, a blue accent. Icons are drawn at 4x
and downsampled for crisp anti-aliased edges.

    python3 tools/make_icons.py

Outputs:
    assets/app/fifine-deck.png            (512) + hicolor sizes
    assets/icons/library/*.png            action icons (256)
    assets/icons/library/index.json       {name: {file, label, category}}
"""
import json
import math
import os

from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_DIR = os.path.join(HERE, "assets", "app")
LIB_DIR = os.path.join(HERE, "assets", "icons", "library")
SS = 4  # supersample factor

ACCENT = (21, 81, 255)       # #1551ff
ACCENT2 = (64, 158, 255)     # #409eff
WHITE = (255, 255, 255)
DARK = (26, 26, 26)          # #1a1a1a


def canvas(size, bg):
    s = size * SS
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    return img, d, s


def rounded(d, box, r, fill):
    d.rounded_rectangle(box, radius=r, fill=fill)


def finish(img, size):
    return img.resize((size, size), Image.LANCZOS)


def tile(size, bg, draw_glyph):
    """A rounded-square colored tile with a white glyph drawn by draw_glyph(d, s)."""
    img, d, s = canvas(size, bg)
    rounded(d, [s * 0.06, s * 0.06, s * 0.94, s * 0.94], s * 0.18, bg)
    draw_glyph(d, s)
    return finish(img, size)


# ----- glyphs (draw in white on the s x s supersampled canvas) --------------
def g_speaker(d, s, waves=1, badge=None):
    cx, cy = s * 0.40, s * 0.5
    bw, bh = s * 0.12, s * 0.16
    # box + cone
    d.rectangle([cx - bw, cy - bh * 0.6, cx, cy + bh * 0.6], fill=WHITE)
    d.polygon([(cx, cy - bh * 0.6), (cx + s * 0.16, cy - bh * 1.2),
               (cx + s * 0.16, cy + bh * 1.2), (cx, cy + bh * 0.6)], fill=WHITE)
    if waves > 0:
        for i in range(waves):
            rr = s * (0.14 + i * 0.09)
            bbox = [cx + s * 0.18 - rr, cy - rr, cx + s * 0.18 + rr, cy + rr]
            d.arc(bbox, -45, 45, fill=WHITE, width=int(s * 0.035))
    if badge == "x":
        x0, y0 = s * 0.62, s * 0.36
        w = int(s * 0.05)
        d.line([x0, y0, x0 + s * 0.16, y0 + s * 0.16], fill=WHITE, width=w)
        d.line([x0 + s * 0.16, y0, x0, y0 + s * 0.16], fill=WHITE, width=w)
    elif badge in ("+", "-"):
        bx, by, L, w = s * 0.72, s * 0.5, s * 0.09, int(s * 0.05)
        d.line([bx - L, by, bx + L, by], fill=WHITE, width=w)
        if badge == "+":
            d.line([bx, by - L, bx, by + L], fill=WHITE, width=w)


def g_vol(d, s, waves):
    """Speaker with `waves` sound arcs. Volume-up draws more waves, volume-down
    fewer — the wave count alone distinguishes them (no +/- sign)."""
    cx, cy = s * 0.34, s * 0.5
    bw, bh = s * 0.11, s * 0.16
    d.rectangle([cx - bw, cy - bh * 0.6, cx, cy + bh * 0.6], fill=WHITE)
    d.polygon([(cx, cy - bh * 0.6), (cx + s * 0.15, cy - bh * 1.25),
               (cx + s * 0.15, cy + bh * 1.25), (cx, cy + bh * 0.6)], fill=WHITE)
    for i in range(waves):
        rr = s * (0.13 + i * 0.085)
        bbox = [cx + s * 0.17 - rr, cy - rr, cx + s * 0.17 + rr, cy + rr]
        d.arc(bbox, -55, 55, fill=WHITE, width=int(s * 0.036))


def g_play(d, s):
    d.polygon([(s * 0.36, s * 0.30), (s * 0.72, s * 0.5), (s * 0.36, s * 0.70)], fill=WHITE)


def g_pause(d, s):
    d.rectangle([s * 0.36, s * 0.30, s * 0.45, s * 0.70], fill=WHITE)
    d.rectangle([s * 0.55, s * 0.30, s * 0.64, s * 0.70], fill=WHITE)


def g_stop(d, s):
    rounded(d, [s * 0.34, s * 0.34, s * 0.66, s * 0.66], s * 0.03, WHITE)


def g_next(d, s):
    d.polygon([(s * 0.30, s * 0.32), (s * 0.52, s * 0.5), (s * 0.30, s * 0.68)], fill=WHITE)
    d.polygon([(s * 0.50, s * 0.32), (s * 0.72, s * 0.5), (s * 0.50, s * 0.68)], fill=WHITE)
    d.rectangle([s * 0.72, s * 0.32, s * 0.78, s * 0.68], fill=WHITE)


def g_prev(d, s):
    d.polygon([(s * 0.70, s * 0.32), (s * 0.48, s * 0.5), (s * 0.70, s * 0.68)], fill=WHITE)
    d.polygon([(s * 0.50, s * 0.32), (s * 0.28, s * 0.5), (s * 0.50, s * 0.68)], fill=WHITE)
    d.rectangle([s * 0.22, s * 0.32, s * 0.28, s * 0.68], fill=WHITE)


def g_chevron(d, s, right=True):
    w = int(s * 0.06)
    if right:
        pts = [(s * 0.42, s * 0.28), (s * 0.64, s * 0.5), (s * 0.42, s * 0.72)]
    else:
        pts = [(s * 0.58, s * 0.28), (s * 0.36, s * 0.5), (s * 0.58, s * 0.72)]
    d.line(pts, fill=WHITE, width=w, joint="curve")


def g_sun(d, s, badge=None):
    cx, cy, r = s * 0.5, s * 0.5, s * 0.12
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=WHITE)
    for i in range(8):
        a = i * math.pi / 4
        x1 = cx + math.cos(a) * r * 1.6
        y1 = cy + math.sin(a) * r * 1.6
        x2 = cx + math.cos(a) * r * 2.3
        y2 = cy + math.sin(a) * r * 2.3
        d.line([x1, y1, x2, y2], fill=WHITE, width=int(s * 0.03))
    if badge:
        bx, by, L, w = s * 0.5, s * 0.5, s * 0.05, int(s * 0.035)
        # small +/- inside circle
        d.line([bx - L, by, bx + L, by], fill=ACCENT, width=w)
        if badge == "+":
            d.line([bx, by - L, bx, by + L], fill=ACCENT, width=w)


def g_folder(d, s):
    d.polygon([(s * 0.28, s * 0.36), (s * 0.44, s * 0.36), (s * 0.50, s * 0.42),
               (s * 0.72, s * 0.42), (s * 0.72, s * 0.36)], fill=WHITE)
    rounded(d, [s * 0.28, s * 0.40, s * 0.72, s * 0.66], s * 0.02, WHITE)


def g_terminal(d, s):
    rounded(d, [s * 0.26, s * 0.30, s * 0.74, s * 0.70], s * 0.03, WHITE)
    rounded(d, [s * 0.29, s * 0.33, s * 0.71, s * 0.67], s * 0.02, DARK)
    w = int(s * 0.03)
    d.line([s * 0.34, s * 0.42, s * 0.42, s * 0.50, ], fill=WHITE, width=w, joint="curve")
    d.line([s * 0.42, s * 0.50, s * 0.34, s * 0.58], fill=WHITE, width=w, joint="curve")
    d.line([s * 0.46, s * 0.58, s * 0.58, s * 0.58], fill=WHITE, width=w)


def g_globe(d, s):
    cx, cy, r = s * 0.5, s * 0.5, s * 0.20
    w = int(s * 0.028)
    d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=WHITE, width=w)
    d.ellipse([cx - r * 0.5, cy - r, cx + r * 0.5, cy + r], outline=WHITE, width=w)
    d.line([cx - r, cy, cx + r, cy], fill=WHITE, width=w)
    d.arc([cx - r, cy - r * 0.5, cx + r, cy + r * 1.5], 200, 340, fill=WHITE, width=w)
    d.arc([cx - r, cy - r * 1.5, cx + r, cy + r * 0.5], 20, 160, fill=WHITE, width=w)


def g_mic(d, s):
    cx = s * 0.5
    rounded(d, [cx - s * 0.09, s * 0.26, cx + s * 0.09, s * 0.54], s * 0.09, WHITE)
    w = int(s * 0.03)
    d.arc([cx - s * 0.16, s * 0.34, cx + s * 0.16, s * 0.62], 20, 160, fill=WHITE, width=w)
    d.line([cx, s * 0.62, cx, s * 0.70], fill=WHITE, width=w)
    d.line([cx - s * 0.08, s * 0.70, cx + s * 0.08, s * 0.70], fill=WHITE, width=w)


def g_camera(d, s):
    rounded(d, [s * 0.26, s * 0.36, s * 0.74, s * 0.66], s * 0.04, WHITE)
    d.rectangle([s * 0.40, s * 0.31, s * 0.52, s * 0.37], fill=WHITE)
    cx, cy, r = s * 0.5, s * 0.51, s * 0.08
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=DARK)


def g_power(d, s):
    cx, cy, r = s * 0.5, s * 0.52, s * 0.16
    w = int(s * 0.045)
    d.arc([cx - r, cy - r, cx + r, cy + r], 300, 240, fill=WHITE, width=w)
    d.line([cx, s * 0.30, cx, s * 0.52], fill=WHITE, width=w)


def g_star(d, s):
    cx, cy, R, r = s * 0.5, s * 0.52, s * 0.22, s * 0.09
    pts = []
    for i in range(10):
        ang = -math.pi / 2 + i * math.pi / 5
        rad = R if i % 2 == 0 else r
        pts.append((cx + math.cos(ang) * rad, cy + math.sin(ang) * rad))
    d.polygon(pts, fill=WHITE)


def g_heart(d, s):
    cx, cy = s * 0.5, s * 0.46
    r = s * 0.11
    d.ellipse([cx - 2 * r, cy - r, cx, cy + r], fill=WHITE)
    d.ellipse([cx, cy - r, cx + 2 * r, cy + r], fill=WHITE)
    d.polygon([(cx - 2 * r, cy), (cx + 2 * r, cy), (cx, cy + s * 0.22)], fill=WHITE)


def g_home(d, s):
    d.polygon([(s * 0.5, s * 0.28), (s * 0.74, s * 0.5), (s * 0.26, s * 0.5)], fill=WHITE)
    d.rectangle([s * 0.33, s * 0.5, s * 0.67, s * 0.70], fill=WHITE)
    d.rectangle([s * 0.45, s * 0.56, s * 0.55, s * 0.70], fill=DARK)


def g_gear(d, s):
    cx, cy = s * 0.5, s * 0.5
    R, r = s * 0.20, s * 0.11
    for i in range(8):
        a = i * math.pi / 4
        x = cx + math.cos(a) * R
        y = cy + math.sin(a) * R
        d.ellipse([x - s * 0.05, y - s * 0.05, x + s * 0.05, y + s * 0.05], fill=WHITE)
    d.ellipse([cx - R * 0.9, cy - R * 0.9, cx + R * 0.9, cy + R * 0.9], fill=WHITE)
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=DARK)


def g_lock(d, s):
    cx = s * 0.5
    w = int(s * 0.035)
    d.arc([cx - s * 0.10, s * 0.30, cx + s * 0.10, s * 0.52], 180, 360, fill=WHITE, width=w)
    rounded(d, [cx - s * 0.15, s * 0.44, cx + s * 0.15, s * 0.70], s * 0.03, WHITE)


def g_dot(d, s, color=WHITE):
    d.ellipse([s * 0.40, s * 0.40, s * 0.60, s * 0.60], fill=color)


# ----- library definition ---------------------------------------------------
BG_MEDIA = (34, 34, 40)
BG_AUDIO = (22, 42, 90)
BG_SYS = (40, 40, 46)
BG_NAV = ACCENT
BG_APP = (60, 40, 90)

LIBRARY = [
    ("volume_up",   "Volume +",  BG_AUDIO, lambda d, s: g_vol(d, s, 3), "Audio"),
    ("volume_down", "Volume −",  (18, 34, 70), lambda d, s: g_vol(d, s, 1), "Audio"),
    ("mute",        "Mute",      (90, 30, 30), lambda d, s: g_speaker(d, s, 0, "x"), "Audio"),
    ("play",        "Play",      BG_MEDIA, g_play, "Media"),
    ("pause",       "Pause",     BG_MEDIA, g_pause, "Media"),
    ("stop",        "Stop",      BG_MEDIA, g_stop, "Media"),
    ("next",        "Next",      BG_MEDIA, g_next, "Media"),
    ("prev",        "Previous",  BG_MEDIA, g_prev, "Media"),
    ("brightness_up",   "Bright +", ACCENT2, lambda d, s: g_sun(d, s, "+"), "Device"),
    ("brightness_down", "Bright −", (30, 90, 140), lambda d, s: g_sun(d, s, "-"), "Device"),
    ("next_page",   "Next page", BG_NAV, lambda d, s: g_chevron(d, s, True), "Navigation"),
    ("prev_page",   "Prev page", BG_NAV, lambda d, s: g_chevron(d, s, False), "Navigation"),
    ("folder",      "Folder",    (60, 50, 25), g_folder, "Apps"),
    ("terminal",    "Terminal",  BG_SYS, g_terminal, "Apps"),
    ("web",         "Website",   (20, 70, 70), g_globe, "Apps"),
    ("mic",         "Mic",       BG_APP, g_mic, "Media"),
    ("camera",      "Camera",    BG_APP, g_camera, "Media"),
    ("power",       "Power",     (90, 30, 30), g_power, "System"),
    ("lock",        "Lock",      BG_SYS, g_lock, "System"),
    ("settings",    "Settings",  BG_SYS, g_gear, "System"),
    ("home",        "Home",      ACCENT, g_home, "Apps"),
    ("star",        "Star",      (120, 90, 20), g_star, "Generic"),
    ("heart",       "Heart",     (120, 30, 60), g_heart, "Generic"),
    ("dot",         "Dot",       (40, 40, 46), g_dot, "Generic"),
]


def make_library():
    os.makedirs(LIB_DIR, exist_ok=True)
    index = {}
    for name, label, bg, glyph, cat in LIBRARY:
        img = tile(256, bg, glyph)
        fn = f"{name}.png"
        img.save(os.path.join(LIB_DIR, fn))
        index[name] = {"file": fn, "label": label, "category": cat}
    with open(os.path.join(LIB_DIR, "index.json"), "w") as f:
        json.dump(index, f, indent=2)
    print(f"library: {len(LIBRARY)} icons -> {LIB_DIR}")


def make_app_icon():
    """Original app icon: rounded blue square with a 3x2 grid of colored keys."""
    os.makedirs(APP_DIR, exist_ok=True)
    S = 512 * SS
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    rounded(d, [S * 0.04, S * 0.04, S * 0.96, S * 0.96], S * 0.22, ACCENT)
    # inner subtle border
    rounded(d, [S * 0.04, S * 0.04, S * 0.96, S * 0.96], S * 0.22, None)
    keys = [
        (0, 0, (235, 60, 90)), (1, 0, (240, 120, 60)), (2, 0, None),   # last = knob
        (0, 1, (60, 220, 160)), (1, 1, (120, 90, 235)), (2, 1, (240, 90, 200)),
    ]
    gx0, gy0 = S * 0.16, S * 0.20
    cell = S * 0.22
    gap = S * 0.045
    for col, row, color in keys:
        x = gx0 + col * (cell + gap)
        y = gy0 + row * (cell + gap)
        # cyan offset shadow (like the original)
        rounded(d, [x + cell * 0.12, y + cell * 0.12, x + cell * 1.12, y + cell * 1.12],
                cell * 0.12, None)
        d.rounded_rectangle([x + cell * 0.12, y + cell * 0.12,
                             x + cell * 1.06, y + cell * 1.06],
                            radius=cell * 0.10, outline=ACCENT2, width=int(S * 0.008))
        if color is None:
            # knob: rotated gradient-ish diamond
            cx, cy = x + cell * 0.5, y + cell * 0.5
            r = cell * 0.42
            d.rounded_rectangle([cx - r, cy - r, cx + r, cy + r], radius=r * 0.3,
                                fill=(150, 130, 240))
        else:
            d.rounded_rectangle([x, y, x + cell, y + cell], radius=cell * 0.10,
                                fill=color, outline=(10, 10, 10), width=int(S * 0.01))
    base = img.resize((512, 512), Image.LANCZOS)
    base.save(os.path.join(APP_DIR, "fifine-deck.png"))
    for sz in (16, 24, 32, 48, 64, 128, 256):
        base.resize((sz, sz), Image.LANCZOS).save(
            os.path.join(APP_DIR, f"fifine-deck-{sz}.png"))
    print(f"app icon: 512 + hicolor sizes -> {APP_DIR}")


if __name__ == "__main__":
    make_library()
    make_app_icon()
