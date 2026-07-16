"""Key rendering: colour parsing, image size, press flash, device JPEG encoding."""
import io

from PIL import Image, ImageStat

from fifine_deck import rendering

RED, GREEN, BLUE, YELLOW = (255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0)


def _mean_luma(img):
    return ImageStat.Stat(img.convert("L")).mean[0]


def _quadrants():
    """An image with four differently-coloured quadrants, so any rotation or
    flip is visible. Solid blocks survive JPEG; single pixels would not."""
    img = Image.new("RGB", (64, 64))
    img.paste(RED, (0, 0, 32, 32))          # top-left
    img.paste(GREEN, (32, 0, 64, 32))       # top-right
    img.paste(BLUE, (0, 32, 32, 64))        # bottom-left
    img.paste(YELLOW, (32, 32, 64, 64))     # bottom-right
    return img


def _corners(data):
    """Decode device JPEG bytes, sample the middle of each quadrant."""
    img = Image.open(io.BytesIO(data)).convert("RGB")
    return [img.getpixel(p) for p in ((16, 16), (48, 16), (16, 48), (48, 48))]


def _near(got, want, tol=40):
    return all(abs(a - b) <= tol for a, b in zip(got, want))


def _assert_layout(data, want):
    got = _corners(data)
    assert all(_near(g, w) for g, w in zip(got, want)), f"got {got}, want {want}"


def test_hex_parsing():
    assert rendering._hex("#ff0000") == (255, 0, 0)
    assert rendering._hex("#f00") == (255, 0, 0)        # 3-digit expands
    assert rendering._hex("not-a-color") == (16, 16, 32)  # fallback


def test_render_key_dimensions():
    img = rendering.render_key(100, label="Test", bg_color="#202040")
    assert img.size == (100, 100)
    assert img.mode == "RGB"


def test_render_key_pressed_is_brighter():
    normal = rendering.render_key(80, bg_color="#404040")
    pressed = rendering.render_key(80, bg_color="#404040", pressed=True)
    assert _mean_luma(pressed) > _mean_luma(normal)


def test_to_device_jpeg_returns_jpeg():
    img = Image.new("RGB", (100, 100), (10, 20, 30))
    data = rendering.to_device_jpeg(img, rotation=180, flip=(True, False))
    assert isinstance(data, bytes)
    assert data[:2] == b"\xff\xd8"   # JPEG start-of-image marker


# -- icons ------------------------------------------------------------------

def _icon(tmp_path, color=(255, 0, 0, 255), size=(64, 64)):
    p = tmp_path / "icon.png"
    Image.new("RGBA", size, color).save(p)
    return str(p)


def test_icon_is_composited_onto_the_key(tmp_path):
    plain = rendering.render_key(100, bg_color="#000000")
    withicon = rendering.render_key(100, bg_color="#000000",
                                    icon_path=_icon(tmp_path))
    assert _mean_luma(withicon) > _mean_luma(plain)


def test_oversized_icon_is_scaled_to_fit(tmp_path):
    """thumbnail() must keep the icon inside the key, whatever its source size."""
    img = rendering.render_key(100, bg_color="#000000",
                               icon_path=_icon(tmp_path, size=(512, 512)))
    assert img.size == (100, 100)


def test_transparent_icon_keeps_the_background(tmp_path):
    """RGBA alpha must be honoured, not pasted as a black box."""
    img = rendering.render_key(100, bg_color="#ff0000",
                               icon_path=_icon(tmp_path, color=(0, 0, 0, 0)))
    assert _near(img.getpixel((50, 50)), (255, 0, 0), tol=10)


def test_missing_icon_falls_back_to_a_plain_key(tmp_path):
    img = rendering.render_key(100, bg_color="#202040",
                               icon_path=str(tmp_path / "nope.png"))
    assert img.size == (100, 100)


def test_corrupt_icon_does_not_raise(tmp_path):
    """A file that isn't an image must not break the render of a key."""
    bad = tmp_path / "bad.png"
    bad.write_bytes(b"this is not a PNG")
    img = rendering.render_key(100, bg_color="#202040", icon_path=str(bad))
    assert img.size == (100, 100)


# -- labels ------------------------------------------------------------------

def test_long_label_wraps_and_is_capped_at_three_lines():
    from PIL import ImageDraw

    draw = ImageDraw.Draw(Image.new("RGB", (100, 100)))
    lines = rendering._wrap(draw, "alpha bravo charlie delta echo foxtrot golf",
                            rendering._font(16), 60)
    assert 1 < len(lines) <= 3


def test_wrap_keeps_an_unsplittable_word_whole():
    from PIL import ImageDraw

    draw = ImageDraw.Draw(Image.new("RGB", (100, 100)))
    assert rendering._wrap(draw, "supercalifragilistic", rendering._font(16), 20) \
        == ["supercalifragilistic"]


def test_wrap_handles_empty_text():
    from PIL import ImageDraw

    draw = ImageDraw.Draw(Image.new("RGB", (100, 100)))
    assert rendering._wrap(draw, "", rendering._font(16), 60) == [""]


def test_label_renders_without_an_icon():
    plain = rendering.render_key(100, bg_color="#000000")
    labelled = rendering.render_key(100, bg_color="#000000", label="Mute")
    assert _mean_luma(labelled) > _mean_luma(plain)      # text drawn


def test_font_falls_back_when_no_font_file_exists(monkeypatch):
    """Bundled fonts differ across distros/snap/flatpak; a missing one must
    degrade to PIL's default rather than crash every key render."""
    monkeypatch.setattr(rendering, "_FONT_CANDIDATES", ["/nonexistent/none.ttf"])
    rendering._font.cache_clear()
    try:
        assert rendering._font(14) is not None
    finally:
        rendering._font.cache_clear()       # don't poison other tests


# -- Qt bridge ---------------------------------------------------------------

def test_pil_to_qimage_owns_its_buffer():
    """The QImage must deep-copy: the PIL buffer it was built from is freed."""
    q = rendering.pil_to_qimage(Image.new("RGB", (8, 4), (255, 0, 0)))
    assert (q.width(), q.height()) == (8, 4)
    assert not q.isNull()


# -- device orientation ------------------------------------------------------
# These pin the transform itself, not just that bytes come back: a broken
# rotation still encodes a perfectly valid JPEG — of an upside-down key.

def test_no_transform_preserves_layout():
    data = rendering.to_device_jpeg(_quadrants(), rotation=0, flip=(False, False))
    _assert_layout(data, [RED, GREEN, BLUE, YELLOW])


def test_rotation_180_swaps_opposite_corners():
    """DEVICE_PROFILE ships rotation=180 — this is the shipped path."""
    data = rendering.to_device_jpeg(_quadrants(), rotation=180, flip=(False, False))
    _assert_layout(data, [YELLOW, BLUE, GREEN, RED])


def test_horizontal_flip_mirrors_left_to_right():
    data = rendering.to_device_jpeg(_quadrants(), rotation=0, flip=(True, False))
    _assert_layout(data, [GREEN, RED, YELLOW, BLUE])


def test_vertical_flip_mirrors_top_to_bottom():
    data = rendering.to_device_jpeg(_quadrants(), rotation=0, flip=(False, True))
    _assert_layout(data, [BLUE, YELLOW, RED, GREEN])


def test_rotation_and_flip_compose_in_that_order():
    """rotate first, then flip — a 180 rotation plus a horizontal flip is a
    vertical mirror of the original."""
    data = rendering.to_device_jpeg(_quadrants(), rotation=180, flip=(True, False))
    _assert_layout(data, [BLUE, YELLOW, RED, GREEN])


def test_rgba_input_is_encodable():
    """render_key can hand back RGBA (icon compositing); JPEG has no alpha."""
    data = rendering.to_device_jpeg(_quadrants().convert("RGBA"), rotation=0)
    assert data[:2] == b"\xff\xd8"
