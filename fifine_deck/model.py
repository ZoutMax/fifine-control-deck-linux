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
from dataclasses import dataclass, field
from typing import Optional

# XDG_CONFIG_HOME matters: under Flatpak it points into ~/.var/app/<id>/,
# and hardcoding ~/.config there writes into the sandbox's throwaway home —
# the config would silently vanish on every restart.
CONFIG_DIR = os.path.join(
    os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"),
    "fifine-control-deck")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
ICONS_DIR = os.path.join(CONFIG_DIR, "icons")
CONFIG_VERSION = 1


def ensure_dirs() -> None:
    os.makedirs(CONFIG_DIR, exist_ok=True)
    os.makedirs(ICONS_DIR, exist_ok=True)


def _new_id() -> str:
    return uuid.uuid4().hex[:8]


def _as_dict(v) -> dict:
    """Defensive: hand-edited configs can hold any JSON type anywhere."""
    return v if isinstance(v, dict) else {}


def _as_str(v, default: str) -> str:
    """Coerce a scalar that must be a string. A null/number here would pass
    load() structurally and then crash the GUI at startup on every launch —
    a loop the corrupt-config recovery can't catch because json.load and
    from_dict both succeed."""
    return v if isinstance(v, str) else default


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
        d = _as_dict(d)
        if not d:
            return cls()
        return cls(type=_as_str(d.get("type"), "none"),
                   params=dict(_as_dict(d.get("params"))))


@dataclass
class KeyConfig:
    """Appearance + behaviour of one key on one page."""
    label: str = ""
    icon: str = ""              # absolute path to an image, or "" for generated tile
    bg_color: str = "#101020"
    text_color: str = "#ffffff"
    action: Action = field(default_factory=Action)
    # Optional long-press action (fires after HOLD_THRESHOLD while the key is
    # held; the primary action then fires only on a short press's release).
    # type "none" == no hold action, and is omitted from the saved config.
    hold_action: Action = field(default_factory=Action)
    folder: "Folder | None" = None   # set when action == "open_folder"

    def to_dict(self) -> dict:
        d = {
            "label": self.label,
            "icon": self.icon,
            "bg_color": self.bg_color,
            "text_color": self.text_color,
            "action": self.action.to_dict(),
        }
        if self.hold_action.type != "none":
            d["hold_action"] = self.hold_action.to_dict()
        if self.folder is not None:
            d["folder"] = self.folder.to_dict()
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "KeyConfig":
        d = _as_dict(d)
        fdata = d.get("folder")
        return cls(
            label=_as_str(d.get("label"), ""),
            icon=_as_str(d.get("icon"), ""),
            bg_color=_as_str(d.get("bg_color"), "#101020"),
            text_color=_as_str(d.get("text_color"), "#ffffff"),
            action=Action.from_dict(d.get("action")),
            hold_action=Action.from_dict(d.get("hold_action")),
            folder=Folder.from_dict(fdata) if isinstance(fdata, dict) else None,
        )

    def is_empty(self) -> bool:
        return (not self.label and not self.icon
                and self.action.type == "none"
                and self.hold_action.type == "none"
                and self.folder is None)


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
        d = _as_dict(d)
        return cls(
            label=_as_str(d.get("label"), ""),
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
        d = _as_dict(d)
        keys: dict[int, KeyConfig] = {}
        for k, v in _as_dict(d.get("keys")).items():
            try:
                keys[int(k)] = KeyConfig.from_dict(v)
            except (TypeError, ValueError):
                continue        # malformed index: drop the entry, keep the page
        knobs: dict[int, KnobConfig] = {}
        for k, v in _as_dict(d.get("knobs")).items():
            try:
                knobs[int(k)] = KnobConfig.from_dict(v)
            except (TypeError, ValueError):
                continue
        return cls(
            name=_as_str(d.get("name"), "Page"),
            id=_as_str(d.get("id"), "") or _new_id(),
            keys=keys,
            knobs=knobs,
        )


@dataclass
class Folder:
    """A nested key-set owned by a key (like a Stream Deck folder). Has its own
    pages; enter it from a key, and a 'Back' key returns to the parent."""
    name: str = "Folder"
    id: str = field(default_factory=_new_id)
    pages: list[Page] = field(default_factory=lambda: [Page(name="Main")])

    def to_dict(self) -> dict:
        return {"name": self.name, "id": self.id,
                "pages": [p.to_dict() for p in self.pages]}

    @classmethod
    def from_dict(cls, d: dict) -> "Folder":
        d = _as_dict(d)
        raw = d.get("pages")
        pages = ([Page.from_dict(p) for p in raw] if isinstance(raw, list) else []) \
            or [Page(name="Main")]
        return cls(name=_as_str(d.get("name"), "Folder"),
                   id=_as_str(d.get("id"), "") or _new_id(), pages=pages)


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
        d = _as_dict(d)
        raw = d.get("pages")
        pages = ([Page.from_dict(p) for p in raw] if isinstance(raw, list) else []) \
            or [Page(name="Main")]
        return cls(
            name=_as_str(d.get("name"), "Default"),
            id=_as_str(d.get("id"), "") or _new_id(),
            wm_class=_as_str(d.get("wm_class"), ""),
            pages=pages,
        )


@dataclass
class DeckConfig:
    """Top-level persisted configuration."""
    version: int = CONFIG_VERSION
    brightness: int = 80
    glow: bool = True          # glow a key on the device while it is pressed
    snap_hint_dismissed: bool = False   # user ticked "don't show again" on the snap USB hint
    # Flatpak only: last granted portal autostart state. The Background portal
    # has no query API, so this is the toggle's memory between runs (outside
    # Flatpak the autostart .desktop file's existence is the truth instead).
    autostart_enabled: bool = False
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
            "snap_hint_dismissed": self.snap_hint_dismissed,
            "autostart_enabled": self.autostart_enabled,
            "active_profile_id": self.active_profile_id,
            "profiles": [p.to_dict() for p in self.profiles],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "DeckConfig":
        d = _as_dict(d)
        raw = d.get("profiles")
        profiles = ([Profile.from_dict(p) for p in raw] if isinstance(raw, list)
                    else []) or [Profile()]
        try:
            brightness = max(0, min(100, int(d.get("brightness", 80))))
        except (TypeError, ValueError):
            brightness = 80
        cfg = cls(
            version=d.get("version", CONFIG_VERSION),
            brightness=brightness,
            glow=bool(d.get("glow", True)),
            snap_hint_dismissed=bool(d.get("snap_hint_dismissed", False)),
            autostart_enabled=bool(d.get("autostart_enabled", False)),
            profiles=profiles,
            active_profile_id=_as_str(d.get("active_profile_id"), ""),
        )
        return cfg

    def save(self, path: Optional[str] = None) -> None:
        # Resolved here, not as a default argument: a default binds CONFIG_PATH
        # at import, so redirecting it later (as the tests do) would silently
        # miss every save() call that relies on the default.
        path = path or CONFIG_PATH
        ensure_dirs()
        tmp = path + ".tmp"
        # The config can hold secrets (a password action falls back to storing
        # the value here when no keyring is available), so create the file
        # private from the start. Writing it 0644 and chmod'ing afterwards
        # leaves a window in which any local user can read it.
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            # O_CREAT's mode applies only when the file is CREATED. A stale
            # .tmp left 0644 by an older version's crash would keep its mode
            # and os.replace would carry that onto config.json — so force it.
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(self.to_dict(), f, indent=2)
                # fsync before the rename: without it, a power loss inside the
                # writeback window can journal the rename before the data
                # blocks land, leaving a zero-length config.json after reboot
                # (XFS/btrfs; ext4's auto_da_alloc only mitigates by chance).
                f.flush()
                os.fsync(fd)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        # os.replace carries the temp file's 0600 mode onto the real path.
        os.replace(tmp, path)
        # Persist the rename itself. Best-effort: some filesystems refuse
        # directory fsync, and losing the rename (not the data) on power cut
        # just means the previous config shows up — never a truncated one.
        try:
            dfd = os.open(os.path.dirname(path) or ".", os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except OSError:
            pass

    @staticmethod
    def looks_like_config(data) -> bool:
        """Structural sanity check for imported data (avoid wiping the live
        config when the user picks an unrelated JSON file)."""
        if not isinstance(data, dict):
            return False
        profiles = data.get("profiles")
        if not isinstance(profiles, list) or not profiles:
            return False
        for p in profiles:
            if not isinstance(p, dict) or not isinstance(p.get("pages"), list):
                return False
        return True

    @classmethod
    def load(cls, path: Optional[str] = None) -> "DeckConfig":
        path = path or CONFIG_PATH          # resolved at call time; see save()
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
            # preserve it and start fresh rather than crash on launch. NOT
            # ".bak" — that's the import flow's backup of a known-GOOD config
            # (main_window._import_config), and overwriting it with this
            # corpse would destroy the one copy the user could restore from.
            try:
                os.replace(path, path + ".corrupt")
            except OSError:
                pass
            cfg = cls()
            cfg.save(path)
            return cfg
