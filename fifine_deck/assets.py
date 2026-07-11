"""Locate bundled assets (app icon, icon library)."""
from __future__ import annotations

import json
import os

ASSETS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets")
APP_ICON = os.path.join(ASSETS_DIR, "app", "fifine-deck.png")
LIBRARY_DIR = os.path.join(ASSETS_DIR, "icons", "library")
LIBRARY_INDEX = os.path.join(LIBRARY_DIR, "index.json")


def app_icon_path() -> str:
    return APP_ICON if os.path.exists(APP_ICON) else ""


def load_library() -> list[dict]:
    """Return [{name, file (abs path), label, category}], sorted by category."""
    if not os.path.exists(LIBRARY_INDEX):
        return []
    with open(LIBRARY_INDEX) as f:
        idx = json.load(f)
    items = []
    for name, meta in idx.items():
        items.append({
            "name": name,
            "file": os.path.join(LIBRARY_DIR, meta["file"]),
            "label": meta.get("label", name),
            "category": meta.get("category", "Other"),
        })
    items.sort(key=lambda x: (x["category"], x["label"]))
    return items
