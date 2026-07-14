"""
Device wrapper for the fifine Control Deck (HOTSPOTEKUSB 3142:0060).

The exact geometry is captured in DEVICE_PROFILE. These defaults are a
best-guess (15-key, 112px, 180° like the Stream Dock 293V3 family) and are
confirmed/corrected empirically with probe_device.py. Changing the profile
here is all that's needed to retune key count / size / orientation / mapping.
"""
from __future__ import annotations

import os
from typing import Any

# Make the vendored backend importable no matter how we're launched.
_BACKEND = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backend")
import sys
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from StreamDock.Devices.StreamDock import StreamDock
from StreamDock.InputTypes import InputEvent, ButtonKey, EventType
from StreamDock.FeatrueOption import device_type
from StreamDock import ProductIDs

from . import rendering

VID = 0x3142
PID = 0x0060

# ---------------------------------------------------------------------------
# Editable device profile. Confirm with probe_device.py, then adjust.
# ---------------------------------------------------------------------------
DEVICE_PROFILE: dict[str, Any] = {
    "name": "fifine Control Deck",
    "key_count": 15,
    "cols": 5,
    "rows": 3,
    "key_size": 100,          # square key pixel size (293V3 family = 100px)
    "rotation": 180,          # degrees applied before sending to device
    "flip": (False, False),   # (horizontal, vertical)
    "report_size": (513, 1025, 0),
    # logical key (reading order, 1..15) -> hardware key index. This is the
    # Stream Dock 293V3 mapping, confirmed for this device family.
    "image_key_map": {
        1: 11, 2: 12, 3: 13, 4: 14, 5: 15,
        6: 6,  7: 7,  8: 8,  9: 9,  10: 10,
        11: 1, 12: 2, 13: 3, 14: 4, 15: 5,
    },
    "dial_count": 0,
}


def _image_map():
    m = DEVICE_PROFILE.get("image_key_map") or {}
    if m:
        return {int(k): int(v) for k, v in m.items()}
    return {i: i for i in range(1, DEVICE_PROFILE["key_count"] + 1)}


class FifineDeck(StreamDock):
    """Generic Stream-Dock-protocol device configured via DEVICE_PROFILE."""

    def __init__(self, transport1, devInfo):
        p = DEVICE_PROFILE
        self.KEY_COUNT = p["key_count"]
        self.KEY_COLS = p["cols"]
        self.KEY_ROWS = p["rows"]
        self.KEY_PIXEL_WIDTH = p["key_size"]
        self.KEY_PIXEL_HEIGHT = p["key_size"]
        self.KEY_IMAGE_FORMAT = "JPEG"
        self.KEY_ROTATION = p["rotation"]
        self.KEY_FLIP = p["flip"]
        self.DIAL_COUNT = p["dial_count"]
        self.DECK_TYPE = p["name"]
        self._map = _image_map()
        self._rmap = {v: k for k, v in self._map.items()}
        super().__init__(transport1, devInfo)

    # -- required abstract methods ----------------------------------------
    def set_device(self):
        rs = DEVICE_PROFILE["report_size"]
        self.transport.set_report_size(*rs)
        self.feature_option.deviceType = device_type.dock_universal

    def get_image_key(self, logical_key) -> int:
        k = int(logical_key)
        return self._map.get(k, k)

    def decode_input_event(self, hardware_code: int, state: int) -> InputEvent:
        # This device reports presses in plain reading order (1..KEY_COUNT),
        # while key *images* are addressed through image_key_map. The map is an
        # involution, so applying it to input would double-map and swap rows
        # 1-5 <-> 11-15. Input is therefore identity.
        logical = hardware_code
        if 1 <= logical <= self.KEY_COUNT:
            return InputEvent(
                event_type=EventType.BUTTON,
                key=ButtonKey(logical),
                state=1 if state == 0x01 else 0,
            )
        return InputEvent(event_type=EventType.UNKNOWN)

    def set_brightness(self, percent):
        return self.transport.setBrightness(max(0, min(100, int(percent))))

    def set_touchscreen_image(self, path):
        return -1  # this device has no separate touchscreen background

    # -- image-format descriptors (used by the GIF controller) ------------
    def key_image_format(self):
        return {
            "size": (self.KEY_PIXEL_WIDTH, self.KEY_PIXEL_HEIGHT),
            "format": self.KEY_IMAGE_FORMAT,
            "rotation": self.KEY_ROTATION,
            "flip": self.KEY_FLIP,
        }

    def touchscreen_image_format(self):
        # No real touchscreen; return a size that never matches a key so the
        # GIF encoder always uses the key path.
        return {"size": (800, 480), "format": "JPEG", "rotation": self.KEY_ROTATION,
                "flip": self.KEY_FLIP}

    def set_key_image(self, key, path):
        """Compatibility path-based setter (renders file to key)."""
        from PIL import Image
        img = Image.open(path)
        return self.set_key_image_pil(key, img)

    # -- convenience used by the controller (no temp files) ---------------
    def set_key_image_pil(self, logical_key: int, pil_image):
        size = self.KEY_PIXEL_WIDTH
        if pil_image.size != (size, size):
            pil_image = pil_image.convert("RGB").resize((size, size))
        jpeg = rendering.to_device_jpeg(
            pil_image, rotation=self.KEY_ROTATION, flip=self.KEY_FLIP)
        hw = self.get_image_key(int(logical_key))
        return self.transport.set_key_image_stream(jpeg, hw)

    def set_key_jpeg(self, logical_key: int, jpeg_bytes: bytes):
        hw = self.get_image_key(int(logical_key))
        return self.transport.set_key_image_stream(jpeg_bytes, hw)


def register():
    """Register (VID, PID) -> FifineDeck in the backend product table (idempotent)."""
    entry = (VID, PID, FifineDeck)
    if entry not in ProductIDs.g_products:
        # remove any stale mapping for this VID/PID first
        ProductIDs.g_products[:] = [
            e for e in ProductIDs.g_products if not (e[0] == VID and e[1] == PID)
        ]
        ProductIDs.g_products.append(entry)
    return entry
