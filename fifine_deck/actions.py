"""
Action engine: executes the action bound to a key/knob gesture on Linux.

Actions that only affect the OS (launch, command, hotkey, media, volume, url,
text) are executed here. Actions that affect the deck itself (switch page /
profile, brightness) are delegated to an ActionContext supplied by the runtime
controller, because they need device + config state.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from typing import Protocol

# ---------------------------------------------------------------------------
# Environment detection (done once).
# ---------------------------------------------------------------------------
IS_WAYLAND = bool(os.environ.get("WAYLAND_DISPLAY")) or \
    os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland"


def _has(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def _audio_backend() -> str:
    if _has("wpctl"):
        return "pipewire"
    if _has("pactl"):
        return "pulseaudio"
    return ""


AUDIO = _audio_backend()

# Ordered preference of a keystroke-injection tool for the current session.
if IS_WAYLAND:
    _KEY_TOOLS = ["ydotool", "wtype", "xdotool"]
else:
    _KEY_TOOLS = ["xdotool", "ydotool", "wtype"]
KEY_TOOL = next((t for t in _KEY_TOOLS if _has(t)), "")


class ActionContext(Protocol):
    """Deck-side operations an action may request from the runtime controller."""
    def switch_profile(self, profile_id: str) -> None: ...
    def goto_page(self, index: int) -> None: ...
    def next_page(self) -> None: ...
    def prev_page(self) -> None: ...
    def set_brightness(self, percent: int) -> None: ...
    def adjust_brightness(self, delta: int) -> None: ...


# ---------------------------------------------------------------------------
# Action metadata (drives the GUI editor). Each entry: label + param spec.
# param spec: list of (key, kind, label) where kind in text/multiline/choice.
# ---------------------------------------------------------------------------
ACTION_TYPES: dict[str, dict] = {
    "none":          {"label": "— None —", "params": []},
    "launch_app":    {"label": "Launch application", "params": [("command", "text", "Command / app")]},
    "run_command":   {"label": "Run shell command", "params": [("command", "multiline", "Shell command")]},
    "open_url":      {"label": "Open website / file", "params": [("url", "text", "URL or path")]},
    "hotkey":        {"label": "Send hotkey", "params": [("keys", "text", "e.g. ctrl+shift+m")]},
    "text":          {"label": "Type text", "params": [("text", "multiline", "Text to type")]},
    "media":         {"label": "Media control", "params": [("cmd", "choice:play-pause,next,previous,stop", "Command")]},
    "volume":        {"label": "Volume", "params": [("cmd", "choice:up,down,mute", "Command"), ("step", "text", "Step % (up/down)")]},
    "next_page":     {"label": "Next page", "params": []},
    "prev_page":     {"label": "Previous page", "params": []},
    "goto_page":     {"label": "Go to page #", "params": [("page", "text", "Page number (1-based)")]},
    "switch_profile": {"label": "Switch profile", "params": [("profile_id", "text", "Profile id")]},
    "brightness":    {"label": "Brightness", "params": [("mode", "choice:set,up,down", "Mode"), ("value", "text", "Value / step")]},
    "multi":         {"label": "Multi-action (steps)", "params": []},  # steps edited specially
}


# Catalog grouping for the drag-and-drop sidebar: (category, [action types]).
ACTION_CATALOG = [
    ("Application", ["launch_app", "run_command", "open_url"]),
    ("Keyboard",    ["hotkey", "text"]),
    ("Media",       ["media", "volume"]),
    ("Deck",        ["next_page", "prev_page", "goto_page", "switch_profile", "brightness"]),
    ("Advanced",    ["multi"]),
]

# A default library-icon name + label to auto-assign when an action is dropped.
ACTION_DEFAULT_ICON = {
    "launch_app": ("home", "App"),
    "run_command": ("terminal", "Run"),
    "open_url": ("web", "Web"),
    "hotkey": ("dot", "Hotkey"),
    "text": ("dot", "Text"),
    "media": ("play", "Play"),
    "volume": ("volume_up", "Volume"),
    "next_page": ("next_page", "Next"),
    "prev_page": ("prev_page", "Prev"),
    "goto_page": ("next_page", "Page"),
    "switch_profile": ("settings", "Profile"),
    "brightness": ("brightness_up", "Bright"),
    "multi": ("star", "Multi"),
}


def _popen_detached(args, shell=False):
    subprocess.Popen(
        args, shell=shell, start_new_session=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
    )


def _send_hotkey(combo: str) -> None:
    """Send a key combination like 'ctrl+shift+m'. Best-effort across tools."""
    combo = combo.strip()
    if not combo or not KEY_TOOL:
        if not KEY_TOOL:
            print("[action] no keystroke tool (install xdotool / ydotool / wtype)", flush=True)
        return
    if KEY_TOOL == "xdotool":
        subprocess.run(["xdotool", "key", "--clearmodifiers", combo],
                       stderr=subprocess.DEVNULL)
    elif KEY_TOOL == "wtype":
        parts = [p.lower() for p in combo.split("+")]
        mods, key = parts[:-1], parts[-1]
        args = ["wtype"]
        for m in mods:
            args += ["-M", {"ctrl": "ctrl", "control": "ctrl", "alt": "alt",
                            "shift": "shift", "super": "logo", "meta": "logo"}.get(m, m)]
        args += ["-k", key]
        for m in mods:
            args += ["-m", {"ctrl": "ctrl", "control": "ctrl", "alt": "alt",
                            "shift": "shift", "super": "logo", "meta": "logo"}.get(m, m)]
        subprocess.run(args, stderr=subprocess.DEVNULL)
    elif KEY_TOOL == "ydotool":
        # ydotool key uses keycodes; fall back to `ydotool key` with names via 'key'
        subprocess.run(["ydotool", "key", combo], stderr=subprocess.DEVNULL)


def _type_text(text: str) -> None:
    if not KEY_TOOL:
        return
    if KEY_TOOL == "xdotool":
        subprocess.run(["xdotool", "type", "--clearmodifiers", "--", text],
                       stderr=subprocess.DEVNULL)
    elif KEY_TOOL == "wtype":
        subprocess.run(["wtype", "--", text], stderr=subprocess.DEVNULL)
    elif KEY_TOOL == "ydotool":
        subprocess.run(["ydotool", "type", "--", text], stderr=subprocess.DEVNULL)


def _media(cmd: str) -> None:
    if _has("playerctl"):
        subprocess.run(["playerctl", cmd], stderr=subprocess.DEVNULL)
    else:
        print("[action] media control needs 'playerctl'", flush=True)


SINK = "@DEFAULT_AUDIO_SINK@"


def _volume(cmd: str, step: str) -> None:
    try:
        pct = int(str(step or "5").strip().rstrip("%"))
    except ValueError:
        pct = 5
    if AUDIO == "pipewire":
        if cmd == "up":
            subprocess.run(["wpctl", "set-volume", "-l", "1.5", SINK, f"{pct}%+"])
        elif cmd == "down":
            subprocess.run(["wpctl", "set-volume", SINK, f"{pct}%-"])
        elif cmd == "mute":
            subprocess.run(["wpctl", "set-mute", SINK, "toggle"])
    elif AUDIO == "pulseaudio":
        s = "@DEFAULT_SINK@"
        if cmd == "up":
            subprocess.run(["pactl", "set-sink-volume", s, f"+{pct}%"])
        elif cmd == "down":
            subprocess.run(["pactl", "set-sink-volume", s, f"-{pct}%"])
        elif cmd == "mute":
            subprocess.run(["pactl", "set-sink-mute", s, "toggle"])
    else:
        print("[action] volume control needs pipewire (wpctl) or pulseaudio (pactl)", flush=True)


def execute(action, context: ActionContext | None = None) -> None:
    """Execute a single Action. Never raises; logs failures."""
    t = action.type
    p = action.params
    try:
        if t == "none":
            return
        elif t == "launch_app":
            cmd = p.get("command", "").strip()
            if cmd:
                _popen_detached(cmd, shell=True)
        elif t == "run_command":
            cmd = p.get("command", "").strip()
            if cmd:
                _popen_detached(cmd, shell=True)
        elif t == "open_url":
            url = p.get("url", "").strip()
            if url:
                _popen_detached(["xdg-open", url])
        elif t == "hotkey":
            _send_hotkey(p.get("keys", ""))
        elif t == "text":
            _type_text(p.get("text", ""))
        elif t == "media":
            _media(p.get("cmd", "play-pause"))
        elif t == "volume":
            _volume(p.get("cmd", "up"), p.get("step", "5"))
        elif t == "next_page" and context:
            context.next_page()
        elif t == "prev_page" and context:
            context.prev_page()
        elif t == "goto_page" and context:
            context.goto_page(int(p.get("page", "1")) - 1)
        elif t == "switch_profile" and context:
            context.switch_profile(p.get("profile_id", ""))
        elif t == "brightness" and context:
            mode = p.get("mode", "set")
            val = int(p.get("value", "10") or "10")
            if mode == "set":
                context.set_brightness(val)
            elif mode == "up":
                context.adjust_brightness(abs(val))
            elif mode == "down":
                context.adjust_brightness(-abs(val))
        elif t == "multi":
            for step in p.get("steps", []):
                from .model import Action as _A
                sub = _A.from_dict(step.get("action", step))
                execute(sub, context)
                delay = float(step.get("delay", 0) or 0)
                if delay:
                    time.sleep(delay)
        else:
            print(f"[action] unhandled or context-less action: {t}", flush=True)
    except Exception as e:  # actions must never crash the reader thread
        print(f"[action] '{t}' failed: {e}", flush=True)


def environment_summary() -> str:
    return (f"session={'wayland' if IS_WAYLAND else 'x11'} "
            f"audio={AUDIO or 'none'} keytool={KEY_TOOL or 'none'} "
            f"playerctl={'yes' if _has('playerctl') else 'no'}")
