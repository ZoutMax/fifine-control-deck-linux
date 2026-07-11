#!/usr/bin/env bash
# Launch the fifine Control Deck GUI using the system Python (PyQt6 + Pillow).
cd "$(dirname "$0")"
exec python3 -m fifine_deck "$@"
