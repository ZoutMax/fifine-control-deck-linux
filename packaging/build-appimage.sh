#!/usr/bin/env bash
# Build a self-contained AppImage for fifine Control Deck. No root needed.
#
#   ./packaging/build-appimage.sh [version]
#
# Produces: dist/fifine-control-deck-<version>-x86_64.AppImage
#
# WHY this exists: every other path we ship builds a .deb, so Fedora, Arch,
# openSUSE and SteamOS users have no install route at all. An AppImage runs on
# any glibc distro without a package manager.
#
# HOW it is built: start from python-appimage's manylinux Python (a relocatable
# CPython that already works inside an AppImage), pip the runtime deps into it,
# throw away the ~three quarters of PyQt6 we never import, drop our package in,
# and repack. The result carries its own Python and Qt, so nothing on the host
# matters except glibc, libudev and the graphics stack.
#
# WHAT IT CANNOT DO: install the udev rule. That needs root, and an AppImage has
# no install step. The rule is shipped inside at
# usr/share/fifine-control-deck/70-fifine-deck.rules and the app tells the user
# when device access is the problem; see docs/APPIMAGE.md.
set -euo pipefail

VERSION="${1:-}"
HERE="$(cd "$(dirname "$0")/.." && pwd)"
cd "$HERE"

if [ -z "$VERSION" ]; then
    # Same source of truth as install.sh: the packaged version, not a guess.
    VERSION="$(sed -n '1s/.*(\([^)]*\)).*/\1/p' debian/changelog | sed 's/ppa[0-9]*$//')"
fi
[ -n "$VERSION" ] || { echo "FATAL: no version given and none in debian/changelog" >&2; exit 1; }

PY_VER="3.12"
PY_FULL="3.12.12"
PY_TAG="cp312-cp312-manylinux2014_x86_64"
CACHE="${XDG_CACHE_HOME:-$HOME/.cache}/fifine-appimage"
BUILD="$(mktemp -d)"
OUT="$HERE/dist/fifine-control-deck-${VERSION}-x86_64.AppImage"
trap 'rm -rf "$BUILD"' EXIT

mkdir -p "$CACHE" "$HERE/dist"

fetch() {  # fetch <url> <dest>  — cached, so rebuilds are offline
    local url="$1" dest="$2"
    [ -s "$dest" ] && return 0
    echo ">> downloading $(basename "$dest")"
    curl -fsSL -o "$dest.part" "$url" && mv "$dest.part" "$dest"
}

fetch "https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage" \
      "$CACHE/appimagetool"
fetch "https://github.com/niess/python-appimage/releases/download/python${PY_VER}/python${PY_FULL}-${PY_TAG}.AppImage" \
      "$CACHE/python.AppImage"
chmod +x "$CACHE/appimagetool" "$CACHE/python.AppImage"

echo ">> extracting the Python base"
cd "$BUILD"
"$CACHE/python.AppImage" --appimage-extract >/dev/null
APPDIR="$BUILD/squashfs-root"
PYHOME="$APPDIR/opt/python${PY_VER}"
SP="$PYHOME/lib/python${PY_VER}/site-packages"

echo ">> installing runtime dependencies"
"$PYHOME/bin/python${PY_VER}" -m pip install --quiet --no-warn-script-location \
    PyQt6 Pillow psutil pyudev

echo ">> pruning Qt to what we actually import"
python3 "$HERE/packaging/appimage-prune.py" "$SP"

echo ">> adding the application"
rsync -a --exclude='__pycache__' --exclude='*.pyc' "$HERE/fifine_deck" "$SP/"
rsync -a --exclude='store' "$HERE/assets" "$SP/"
# x86_64 only: the arm64 transport lib and the Windows/macOS blobs are dead weight
find "$SP/fifine_deck" -type f \
     \( -name '*.dll' -o -name '*.dylib' -o -name 'libtransport_arm64.so' \) -delete
# Same guard as build-deb.sh and debian/rules: without this .so the app starts
# but can never open the device, and a whole run of PPA debs once shipped that way.
test -f "$SP/fifine_deck/backend/StreamDock/Transport/TransportDLL/libtransport.so" \
    || { echo "FATAL: x86_64 libtransport.so missing — refusing to build" >&2; exit 1; }

echo ">> desktop integration"
rm -f "$APPDIR/AppRun" "$APPDIR"/python*.desktop "$APPDIR/python.png"
install -Dm644 "$HERE/packaging/70-fifine-deck.rules" \
        "$APPDIR/usr/share/fifine-control-deck/70-fifine-deck.rules"
install -Dm644 "$HERE/packaging/io.github.zoutmax.FifineControlDeck.metainfo.xml" \
        "$APPDIR/usr/share/metainfo/io.github.zoutmax.FifineControlDeck.metainfo.xml"
install -Dm644 "$HERE/assets/app/fifine-deck-256.png" \
        "$APPDIR/usr/share/icons/hicolor/256x256/apps/fifine-control-deck.png"
cp "$HERE/assets/app/fifine-deck-256.png" "$APPDIR/fifine-control-deck.png"
sed 's|^Exec=.*|Exec=fifine-control-deck|' "$HERE/packaging/fifine-control-deck.desktop" \
    > "$APPDIR/fifine-control-deck.desktop"

cat > "$APPDIR/AppRun" <<APPRUN
#!/bin/bash
# fifine Control Deck AppImage entry point.
set -e
if [ -z "\${APPIMAGE}" ]; then
    self="\$(readlink -f -- "\$0")"; APPDIR="\${self%/*}"
fi
export APPDIR="\${APPDIR:-\$(dirname "\$(readlink -f -- "\$0")")}"
# Stash the host's own values BEFORE overwriting them. A key that launches an
# app must hand that app the environment it would have had from a terminal,
# not ours: a host python3 inheriting our PYTHONHOME dies looking for its
# stdlib in our tree. actions.child_env() restores these for every program we
# exec. Only variables the host actually set are stashed, so "unset" survives
# the round trip as unset rather than as empty.
for _v in PYTHONHOME PYTHONPATH PYTHONDONTWRITEBYTECODE LD_LIBRARY_PATH LD_PRELOAD QT_PLUGIN_PATH QT_QPA_PLATFORM_PLUGIN_PATH; do
    if [ -n "\${!_v+x}" ]; then export "FIFINE_HOST_\${_v}=\${!_v}"; fi
done
unset _v
PYHOME="\${APPDIR}/opt/python${PY_VER}"
export PYTHONHOME="\${PYHOME}"
export PYTHONDONTWRITEBYTECODE=1
# Qt comes from inside the bundle. A host QT_PLUGIN_PATH pointing at a different
# Qt build is a classic AppImage crash, so ours wins and the host hint is cleared.
export QT_PLUGIN_PATH="\${PYHOME}/lib/python${PY_VER}/site-packages/PyQt6/Qt6/plugins"
unset QT_QPA_PLATFORM_PLUGIN_PATH
export LD_LIBRARY_PATH="\${PYHOME}/lib/python${PY_VER}/site-packages/PyQt6/Qt6/lib:\${LD_LIBRARY_PATH:-}"
exec "\${PYHOME}/bin/python${PY_VER}" -m fifine_deck "\$@"
APPRUN
chmod +x "$APPDIR/AppRun"

echo ">> packing"
ARCH=x86_64 "$CACHE/appimagetool" --appimage-extract-and-run "$APPDIR" "$OUT" >/dev/null 2>&1
chmod +x "$OUT"

echo
echo "Built: $OUT  ($(du -h "$OUT" | cut -f1))"
echo
echo "Run it:  $OUT"
echo
echo "Device access needs the udev rule installed once, which needs root and"
echo "which an AppImage cannot do for you. The rule travels inside the bundle:"
echo "  \"$OUT\" --appimage-extract usr/share/fifine-control-deck/70-fifine-deck.rules"
echo "  sudo install -m644 squashfs-root/usr/share/fifine-control-deck/70-fifine-deck.rules \\"
echo "      /usr/lib/udev/rules.d/70-fifine-deck.rules"
echo "  sudo udevadm control --reload-rules && sudo udevadm trigger"
echo "See docs/APPIMAGE.md."
