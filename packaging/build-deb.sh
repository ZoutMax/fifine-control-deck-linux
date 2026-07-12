#!/usr/bin/env bash
# Build a .deb for fifine Control Deck. No root needed to build.
#   ./packaging/build-deb.sh [version]
# Produces: dist/fifine-control-deck_<version>_amd64.deb
set -euo pipefail

VERSION="${1:-0.1.0}"
ARCH="${2:-amd64}"        # amd64 | arm64
PKG="fifine-control-deck"
HERE="$(cd "$(dirname "$0")/.." && pwd)"
STAGE="$(mktemp -d)"
OUT="$HERE/dist"
APPDIR="usr/lib/$PKG"

echo "Staging $PKG $VERSION in $STAGE"

# --- application payload -------------------------------------------------
mkdir -p "$STAGE/$APPDIR"
# copy the python package + assets, excluding caches and non-linux/x86 binaries
rsync -a --exclude='__pycache__' --exclude='*.pyc' \
      "$HERE/fifine_deck" "$STAGE/$APPDIR/"
rsync -a --exclude='store' "$HERE/assets" "$STAGE/$APPDIR/"
# slim: keep only the Linux transport lib for THIS architecture.
# (The Python loader picks libtransport.so on x86_64 and libtransport_arm64.so
#  on aarch64, so each .deb ships exactly the one its CPU needs.)
find "$STAGE/$APPDIR" -type f \( -name '*.dll' -o -name '*.dylib' \) -delete 2>/dev/null || true
if [ "$ARCH" = "arm64" ]; then
    find "$STAGE/$APPDIR" -type f -name 'libtransport.so' -delete 2>/dev/null || true
else
    find "$STAGE/$APPDIR" -type f -name 'libtransport_arm64.so' -delete 2>/dev/null || true
fi

# --- launcher ------------------------------------------------------------
mkdir -p "$STAGE/usr/bin"
install -m 0755 "$HERE/packaging/$PKG.launcher" "$STAGE/usr/bin/$PKG"

# --- desktop entry -------------------------------------------------------
mkdir -p "$STAGE/usr/share/applications"
install -m 0644 "$HERE/packaging/$PKG.desktop" \
        "$STAGE/usr/share/applications/$PKG.desktop"

# --- icons (hicolor) -----------------------------------------------------
for sz in 16 24 32 48 64 128 256; do
    dir="$STAGE/usr/share/icons/hicolor/${sz}x${sz}/apps"
    mkdir -p "$dir"
    src="$HERE/assets/app/fifine-deck-${sz}.png"
    [ -f "$src" ] && install -m 0644 "$src" "$dir/$PKG.png"
done
# 512 + scalable fallback
dir="$STAGE/usr/share/icons/hicolor/512x512/apps"; mkdir -p "$dir"
install -m 0644 "$HERE/assets/app/fifine-deck.png" "$dir/$PKG.png"

# --- AppStream metainfo (rich listing in GNOME Software / App Center) -----
mkdir -p "$STAGE/usr/share/metainfo"
install -m 0644 "$HERE/packaging/io.github.zoutmax.FifineControlDeck.metainfo.xml" \
        "$STAGE/usr/share/metainfo/io.github.zoutmax.FifineControlDeck.metainfo.xml"

# --- udev rule -----------------------------------------------------------
# Use /lib/udev/rules.d: read by ALL udev versions (old non-merged-usr and
# modern merged-usr distros alike), maximising cross-flavour compatibility.
mkdir -p "$STAGE/lib/udev/rules.d"
install -m 0644 "$HERE/packaging/99-fifine-deck.rules" \
        "$STAGE/lib/udev/rules.d/99-fifine-deck.rules"

# --- docs: copyright + changelog ----------------------------------------
DOCDIR="$STAGE/usr/share/doc/$PKG"
mkdir -p "$DOCDIR"
cat > "$DOCDIR/copyright" <<'EOF'
Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/
Upstream-Name: fifine-control-deck-linux
Source: https://github.com/ZoutMax/fifine-control-deck-linux

Files: *
Copyright: 2026 ZoutMax
License: MIT

Files: usr/lib/fifine-control-deck/fifine_deck/backend/StreamDock/*
Copyright: MiraBox
License: MIT
 Bundled StreamDock Device SDK (github.com/MiraboxSpace/StreamDock-Device-SDK),
 including the precompiled libtransport.so USB transport library.

License: MIT
 Permission is hereby granted, free of charge, to any person obtaining a copy
 of this software and associated documentation files (the "Software"), to deal
 in the Software without restriction, including without limitation the rights
 to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
 copies of the Software, and to permit persons to whom the Software is
 furnished to do so, subject to the following conditions:
 .
 The above copyright notice and this permission notice shall be included in all
 copies or substantial portions of the Software.
 .
 THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND.
EOF
cat > "$DOCDIR/changelog" <<EOF
$PKG ($VERSION) unstable; urgency=low

  * Release $VERSION.

 -- ZoutMax <danielhoutmann@hotmail.com>  Sat, 11 Jul 2026 00:00:00 +0000
EOF
gzip -9n "$DOCDIR/changelog"

# --- control + maintainer scripts ---------------------------------------
mkdir -p "$STAGE/DEBIAN"
INSTALLED_KB=$(du -sk "$STAGE" | cut -f1)
cat > "$STAGE/DEBIAN/control" <<EOF
Package: $PKG
Version: $VERSION
Section: utils
Priority: optional
Architecture: $ARCH
Depends: python3 (>= 3.10), python3-pyqt6, python3-pil, libc6, libstdc++6, libgcc-s1 | libgcc1, libudev1
Recommends: playerctl, ydotool, wireplumber | pulseaudio-utils, xdg-utils, python3-keyring, python3-pyudev
Installed-Size: $INSTALLED_KB
Maintainer: ZoutMax <danielhoutmann@hotmail.com>
Homepage: https://github.com/ZoutMax/fifine-control-deck-linux
Description: Control app for fifine / Stream Dock macro keypads
 A native Linux control application for the fifine Control Deck
 (Mirabox "Stream Dock" 293V3-family, USB 3142:0060). Draws icons and
 labels on the LCD keys and binds them to actions: launch apps, hotkeys,
 media and volume control, scripts, brightness, and page/profile switching.
 Includes a PyQt6 configuration GUI with profiles and multiple pages.
EOF
install -m 0755 "$HERE/packaging/deb/postinst" "$STAGE/DEBIAN/postinst"
install -m 0755 "$HERE/packaging/deb/postrm"  "$STAGE/DEBIAN/postrm"

# normalise permissions (dpkg-deb/lintian want 0755 dirs, 0644 data files)
find "$STAGE" -type d -exec chmod 0755 {} +
find "$STAGE/usr/lib/$PKG" "$STAGE/usr/share" -type f -exec chmod 0644 {} +
chmod 0755 "$STAGE/usr/bin/$PKG"

# --- build ---------------------------------------------------------------
mkdir -p "$OUT"
DEB="$OUT/${PKG}_${VERSION}_${ARCH}.deb"
dpkg-deb --root-owner-group --build "$STAGE" "$DEB"
rm -rf "$STAGE"
echo
echo "Built: $DEB"
dpkg-deb --info "$DEB" | sed 's/^/  /'
echo
echo "Install with:  sudo apt install $DEB    (or: sudo dpkg -i $DEB)"
