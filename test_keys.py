#!/usr/bin/env python3
"""
Interactive key-press tester. Run it, then press the physical deck keys.
It prints the decoded logical key and the raw hardware code for each press.
Ctrl+C to stop.

    python3 test_keys.py
"""
import sys, time, os, threading, select
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fifine_deck.device import register, FifineDeck
from StreamDock.DeviceManager import DeviceManager
from StreamDock.InputTypes import EventType

register()
docks = [d for d in DeviceManager().enumerate() if isinstance(d, FifineDeck)]
if not docks:
    print("No device found."); sys.exit(1)
dev = docks[0]
dev.open(); dev.init()
print(f"Connected. firmware={dev.firmware_version!r} keys={dev.KEY_COUNT}")
print("PRESS THE DECK KEYS NOW. (Ctrl+C to stop)\n")

n = [0]

def raw(device, data):
    if data and len(data) >= 11:
        sig = bytes(data[:3])
        if data[9] != 0xFF:   # skip write-confirm packets
            print(f"  raw: sig={sig!r} hw={data[9]} state={data[10]} "
                  f"bytes[9:13]={list(data[9:13])}", flush=True)

def cb(device, ev):
    if ev.event_type == EventType.BUTTON:
        n[0] += 1
        print(f"  -> LOGICAL KEY {ev.key.value}  "
              f"{'PRESS' if ev.state == 1 else 'release'}", flush=True)
    else:
        print(f"  -> event {ev.event_type}", flush=True)

dev.set_raw_read_callback(raw)
dev.set_key_callback(cb)

# Also watch the keyboard interface, in case presses arrive as keystrokes.
try:
    kb = os.open("/dev/hidraw1", os.O_RDONLY | os.O_NONBLOCK)
except OSError:
    kb = None

def kbread():
    if kb is None:
        return
    while True:
        r, _, _ = select.select([kb], [], [], 0.3)
        if r:
            try:
                d = os.read(kb, 64)
                if d and any(d):
                    print(f"  [keyboard-interface hidraw1] {d.hex()}", flush=True)
            except BlockingIOError:
                pass

threading.Thread(target=kbread, daemon=True).start()

try:
    while True:
        time.sleep(0.3)
except KeyboardInterrupt:
    pass
finally:
    dev.set_key_callback(None)
    dev.set_raw_read_callback(None)
    print(f"\nTotal presses decoded: {n[0]}")
    dev.close()
