#!/usr/bin/env bash
# Runs as root (via pkexec): installs the udev rule AND fixes permissions on
# the already-connected device so no replug is needed.
set -e
RULE_SRC="$(dirname "$0")/99-fifine-deck.rules"
install -m 0644 "$RULE_SRC" /etc/udev/rules.d/99-fifine-deck.rules
udevadm control --reload-rules || true
udevadm trigger || true

# Immediate effect for the currently-plugged device (no replug required).
for h in /sys/class/hidraw/hidraw*; do
    dev="/dev/$(basename "$h")"
    if grep -qi "3142" "$h/device/uevent" 2>/dev/null; then
        chgrp plugdev "$dev" 2>/dev/null || true
        chmod 0660 "$dev" 2>/dev/null || true
    fi
done
for p in /sys/bus/usb/devices/*; do
    if [ -f "$p/idVendor" ] && [ "$(cat "$p/idVendor" 2>/dev/null)" = "3142" ]; then
        b=$(cat "$p/busnum" 2>/dev/null); d=$(cat "$p/devnum" 2>/dev/null)
        if [ -n "$b" ] && [ -n "$d" ]; then
            node=$(printf "/dev/bus/usb/%03d/%03d" "$b" "$d")
            chgrp plugdev "$node" 2>/dev/null || true
            chmod 0660 "$node" 2>/dev/null || true
        fi
    fi
done
echo "OK: udev rule installed and current device permissions fixed."
