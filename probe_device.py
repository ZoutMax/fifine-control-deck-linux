#!/usr/bin/env python3
"""
Probe the fifine Control Deck (HOTSPOTEKUSB 3142:0060) to discover its profile:
  - firmware version
  - how many keys light up (KEY_COUNT + physical layout + orientation)
  - the hardware key codes reported on press/release

Run AFTER installing the udev rule (so /dev/hidraw* is accessible):
    python3 probe_device.py

Push numbered tiles to hardware keys 1..36, then read input events for 40s.
Press each physical key once (top-left to bottom-right) and note what prints.
"""
import os, sys, time, io, ctypes

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "fifine_deck", "backend"))

from StreamDock.Transport.LibUSBHIDAPI import LibUSBHIDAPI
from PIL import Image, ImageDraw, ImageFont

VID, PID = 0x3142, 0x0060
KEY_SIZE = int(os.environ.get("KEY_SIZE", "112"))      # candidate key pixel size
ROTATION = int(os.environ.get("ROTATION", "180"))      # candidate rotation
MAX_KEYS = int(os.environ.get("MAX_KEYS", "36"))       # probe up to this many keys


def make_tile(n, size, rotation):
    img = Image.new("RGB", (size, size), (20, 20, 30))
    d = ImageDraw.Draw(img)
    # bright border so we can see the key bounds
    d.rectangle([0, 0, size - 1, size - 1], outline=(0, 200, 255), width=3)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", int(size * 0.5))
    except Exception:
        font = ImageFont.load_default()
    txt = str(n)
    tb = d.textbbox((0, 0), txt, font=font)
    d.text(((size - (tb[2] - tb[0])) / 2 - tb[0], (size - (tb[3] - tb[1])) / 2 - tb[1]),
           txt, fill=(255, 255, 255), font=font)
    if rotation:
        img = img.rotate(rotation)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return buf.getvalue()


def main():
    devs = LibUSBHIDAPI.enumerate_devices(VID, PID)
    if not devs:
        print("No device found for %04x:%04x. Is it plugged in?" % (VID, PID))
        return 1
    info = devs[0]
    print("Device:", info["path"], "product=", info["product_string"],
          "usage_page=0x%04x" % info["usage_page"])

    di = LibUSBHIDAPI.create_device_info_from_dict(info)
    t = LibUSBHIDAPI(di)
    if not t.open(info["path"].encode("utf-8")):
        print("FAILED to open %s. Did you install the udev rule + replug?" % info["path"])
        print("  (node is root-only until the rule is applied)")
        return 2

    try:
        t.set_report_size(513, 1025, 0)
        fw = t.get_firmware_version()
        print("Firmware:", repr(fw))
        t.wakeup_screen()
        t.set_key_brightness(100)

        print(f"Pushing numbered tiles (size={KEY_SIZE}, rotation={ROTATION}) to keys 1..{MAX_KEYS} ...")
        pushed = 0
        for hw in range(1, MAX_KEYS + 1):
            jpg = make_tile(hw, KEY_SIZE, ROTATION)
            res = t.set_key_image_stream(jpg, hw)
            if res != 0:
                print(f"  key {hw}: write returned {res} (stopping)")
                break
            pushed += 1
            time.sleep(0.02)
        t.refresh_screen()
        print(f"Pushed {pushed} tiles + refreshed. LOOK AT THE DEVICE:")
        print("  - How many keys show a number? -> KEY_COUNT")
        print("  - Are numbers upright? If upside-down, re-run with ROTATION=0")
        print("  - Note which physical position shows '1', '2', ... (layout/order)")
        print()
        print("Now press each physical key (top-left -> bottom-right). Reading 40s...")
        print("-" * 56)

        t0 = time.time()
        while time.time() - t0 < 40:
            data = t.read_(1024)
            if not data or len(data) < 11:
                continue
            # input-event packet: 'ACK'.. 'OK' .. [9]=hw code [10]=state
            if data[0] == 0x41 and data[1] == 0x43 and data[2] == 0x4B and \
               data[5] == 0x4F and data[6] == 0x4B:
                hw, state = data[9], data[10]
                if hw == 0xFF:
                    continue
                st = {1: "PRESS", 2: "RELEASE", 0: "release"}.get(state, f"state={state}")
                print(f"  hw_key={hw:<3} {st}   raw[9..12]={list(data[9:13])}")
    finally:
        try:
            t.clear_all_keys(); t.refresh_screen()
        except Exception:
            pass
        t.close()
        print("-" * 56)
        print("Done. Tell me: key count, orientation, and the hw_key numbers you saw.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
