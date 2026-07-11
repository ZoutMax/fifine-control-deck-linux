"""
Configuration model for fifine Control Deck (Linux).

A deck has one or more Profiles. Each Profile has one or more Pages. Each Page
maps key indices (1..KEY_COUNT) to KeyConfig entries, and knob indices to
KnobConfig entries. Everything serializes to / from JSON so the GUI and the
runtime daemon share one source of truth.

Config lives at:  ~/.config/fifine-control-deck/config.json
User icons at:    ~/.config/fifine-control-deck/icons/
"""
from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass, field, asdict
from typing import Optional

CONFIG_DIR = os.path.expanduser("~/.config/fifine-control-deck")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
ICONS_DIR = os.path.join(CONFIG_DIR, "icons")
CONFIG_VERSION = 1


def ensure_dirs() -> None:
    os.makedirs(CONFIG_DIR, exist_ok=True)
    os.makedirs(ICONS_DIR, exist_ok=True)


def _new_id() -> str:
    return uuid.uuid4().hex[:8]


@dataclass
class Action:
    """A single action bound to a key or knob gesture.

    type is one of the keys in actions.ACTION_TYPES; params is type-specific.
    """
    type: str = "none"
    params: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"type": self.type, "params": dict(self.params)}

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> "Action":
        if not d:
            return cls()
        return cls(type=d.get("type", "none"), params=dict(d.get("params", {})))


@dataclass
class KeyConfig:
    """Appearance + behaviour of one key on one page."""
    label: str = ""
    icon: str = ""              # absolute path to an image, or "" for generated tile
    bg_color: str = "#101020"
    text_color: str = "#ffffff"
    action: Action = field(default_factory=Action)

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "icon": self.icon,
            "bg_color": self.bg_color,
            "text_color": self.text_color,
            "action": self.action.to_dict(),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "KeyConfig":
        return cls(
            label=d.get("label", ""),
            icon=d.get("icon", ""),
            bg_color=d.get("bg_color", "#101020"),
            text_color=d.get("text_color", "#ffffff"),
            action=Action.from_dict(d.get("action")),
        )

    def is_empty(self) -> bool:
        return not self.label and not self.icon and self.action.type == "none"


@dataclass
class KnobConfig:
    """Behaviour of one knob/dial: press, rotate left, rotate right."""
    label: str = ""
    press: Action = field(default_factory=Action)
    left: Action = field(default_factory=Action)
    right: Action = field(default_factory=Action)

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "press": self.press.to_dict(),
            "left": self.left.to_dict(),
            "right": self.right.to_dict(),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "KnobConfig":
        return cls(
            label=d.get("label", ""),
            press=Action.from_dict(d.get("press")),
            left=Action.from_dict(d.get("left")),
            right=Action.from_dict(d.get("right")),
        )


@dataclass
class Page:
    """A page of key/knob bindings. keys maps a 1-based key index -> KeyConfig."""
    name: str = "Page"
    id: str = field(default_factory=_new_id)
    keys: dict[int, KeyConfig] = field(default_factory=dict)
    knobs: dict[int, KnobConfig] = field(default_factory=dict)

    def key(self, index: int) -> KeyConfig:
        if index not in self.keys:
            self.keys[index] = KeyConfig()
        return self.keys[index]

    def knob(self, index: int) -> KnobConfig:
        if index not in self.knobs:
            self.knobs[index] = KnobConfig()
        return self.knobs[index]

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "id": self.id,
            "keys": {str(k): v.to_dict() for k, v in self.keys.items()},
            "knobs": {str(k): v.to_dict() for k, v in self.knobs.items()},
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Page":
        return cls(
            name=d.get("name", "Page"),
            id=d.get("id", _new_id()),
            keys={int(k): KeyConfig.from_dict(v) for k, v in d.get("keys", {}).items()},
            knobs={int(k): KnobConfig.from_dict(v) for k, v in d.get("knobs", {}).items()},
        )


@dataclass
class Profile:
    """A named collection of pages. Optionally auto-activated for an app (wm_class)."""
    name: str = "Default"
    id: str = field(default_factory=_new_id)
    pages: list[Page] = field(default_factory=lambda: [Page(name="Main")])
    wm_class: str = ""          # optional: focus-follow auto-switch (future)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "id": self.id,
            "wm_class": self.wm_class,
            "pages": [p.to_dict() for p in self.pages],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Profile":
        pages = [Page.from_dict(p) for p in d.get("pages", [])] or [Page(name="Main")]
        return cls(
            name=d.get("name", "Default"),
            id=d.get("id", _new_id()),
            wm_class=d.get("wm_class", ""),
            pages=pages,
        )


@dataclass
class DeckConfig:
    """Top-level persisted configuration."""
    version: int = CONFIG_VERSION
    brightness: int = 80
    glow: bool = True          # glow a key on the device while it is pressed
    profiles: list[Profile] = field(default_factory=lambda: [Profile()])
    active_profile_id: str = ""

    def __post_init__(self):
        if not self.active_profile_id and self.profiles:
            self.active_profile_id = self.profiles[0].id

    # -- lookups -----------------------------------------------------------
    def active_profile(self) -> Profile:
        for p in self.profiles:
            if p.id == self.active_profile_id:
                return p
        return self.profiles[0]

    def profile_by_id(self, pid: str) -> Optional[Profile]:
        return next((p for p in self.profiles if p.id == pid), None)

    # -- persistence -------------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "brightness": self.brightness,
            "glow": self.glow,
            "active_profile_id": self.active_profile_id,
            "profiles": [p.to_dict() for p in self.profiles],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "DeckConfig":
        profiles = [Profile.from_dict(p) for p in d.get("profiles", [])] or [Profile()]
        cfg = cls(
            version=d.get("version", CONFIG_VERSION),
            brightness=int(d.get("brightness", 80)),
            glow=bool(d.get("glow", True)),
            profiles=profiles,
            active_profile_id=d.get("active_profile_id", ""),
        )
        return cfg

    def save(self, path: str = CONFIG_PATH) -> None:
        ensure_dirs()
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.to_dict(), f, indent=2)
        os.replace(tmp, path)

    @classmethod
    def load(cls, path: str = CONFIG_PATH) -> "DeckConfig":
        if not os.path.exists(path):
            cfg = cls()
            cfg.save(path)
            return cfg
        try:
            with open(path) as f:
                return cls.from_dict(json.load(f))
        except (json.JSONDecodeError, OSError, AttributeError, ValueError,
                TypeError, KeyError):
            # Corrupt or structurally-invalid config (bad JSON *or* wrong shape):
            # back it up and start fresh rather than crash on launch.
            try:
                os.replace(path, path + ".bak")
            except OSError:
                pass
            cfg = cls()
            cfg.save(path)
            return cfg
