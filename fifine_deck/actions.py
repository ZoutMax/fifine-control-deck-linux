"""
Action engine: executes the action bound to a key/knob gesture on Linux.

Actions that only affect the OS (launch, command, hotkey, media, volume, url,
text) are executed here. Actions that affect the deck itself (switch page /
profile, brightness) are delegated to an ActionContext supplied by the runtime
controller, because they need device + config state.
"""
from __future__ import annotations

import logging
import os
import shlex
import shutil
import subprocess
import time
from typing import Protocol

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Environment detection (done once).
# ---------------------------------------------------------------------------
IS_WAYLAND = bool(os.environ.get("WAYLAND_DISPLAY")) or \
    os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland"

# Flatpak sandbox: host helper tools (ydotool, playerctl, wpctl) and the user's
# real apps live OUTSIDE the sandbox, so they must be reached via
# `flatpak-spawn --host`. Detected once at import.
IN_FLATPAK = os.path.exists("/.flatpak-info") or bool(os.environ.get("FLATPAK_ID"))
_HOST_PREFIX = ["flatpak-spawn", "--host"]


def _host(args):
    """Prefix an argv list so it runs on the host when inside a Flatpak sandbox."""
    return _HOST_PREFIX + list(args) if IN_FLATPAK else list(args)


def _has(cmd: str) -> bool:
    """Is `cmd` available? Inside Flatpak, probe the HOST — the sandbox PATH
    would not see host-side tools."""
    if IN_FLATPAK:
        try:
            r = subprocess.run(
                _HOST_PREFIX + ["sh", "-c", "command -v " + shlex.quote(cmd)],
                timeout=5, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            return r.returncode == 0
        except Exception:
            return False
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
HAS_PLAYERCTL = _has("playerctl")


class ActionContext(Protocol):
    """Deck-side operations an action may request from the runtime controller."""
    def switch_profile(self, profile_id: str) -> None: ...
    def next_profile(self) -> None: ...
    def prev_profile(self) -> None: ...
    def goto_page(self, index: int) -> None: ...
    def next_page(self) -> None: ...
    def prev_page(self) -> None: ...
    def set_brightness(self, percent: int) -> None: ...
    def adjust_brightness(self, delta: int) -> None: ...
    def sleep_screen(self) -> None: ...


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
    "password":      {"label": "Type password", "params": [("password", "password", "Password")]},
    "media":         {"label": "Media control", "params": [("cmd", "choice:play-pause,next,previous,stop", "Command")]},
    "volume":        {"label": "Volume", "params": [("cmd", "choice:up,down,mute", "Command"), ("step", "text", "Step % (up/down)")]},
    "close_app":     {"label": "Close application", "params": [("target", "text", "App / window name")]},
    "next_page":     {"label": "Next page", "params": []},
    "prev_page":     {"label": "Previous page", "params": []},
    "goto_page":     {"label": "Go to page #", "params": [("page", "text", "Page number (1-based)")]},
    "switch_profile": {"label": "Switch profile", "params": [("profile_id", "profiles", "Profile")]},
    "next_profile":  {"label": "Next profile (Scene Shift)", "params": []},
    "prev_profile":  {"label": "Previous profile", "params": []},
    "brightness":    {"label": "Brightness", "params": [("mode", "choice:set,up,down", "Mode"), ("value", "text", "Value / step")]},
    "sleep_screen":  {"label": "Sleep screen", "params": []},
    "open_folder":   {"label": "Open folder", "params": []},
    "folder_back":   {"label": "Back (exit folder)", "params": []},
    "multi":         {"label": "Multi-action (steps)", "params": []},  # edited specially
}


# Catalog grouping for the drag-and-drop sidebar: (category, [action types]).
ACTION_CATALOG = [
    ("Application", ["launch_app", "run_command", "open_url", "close_app"]),
    ("Keyboard",    ["hotkey", "text", "password"]),
    ("Media",       ["media", "volume"]),
    ("Deck",        ["next_page", "prev_page", "goto_page", "switch_profile",
                     "next_profile", "prev_profile", "brightness", "sleep_screen"]),
    ("Folders",     ["open_folder", "folder_back"]),
    ("Advanced",    ["multi"]),
]

# A default library-icon name + label to auto-assign when an action is dropped.
ACTION_DEFAULT_ICON = {
    "launch_app": ("home", "App"),
    "run_command": ("terminal", "Run"),
    "open_url": ("web", "Web"),
    "hotkey": ("dot", "Hotkey"),
    "text": ("dot", "Text"),
    "password": ("lock", "Password"),
    "media": ("play", "Play"),
    "volume": ("volume_up", "Volume"),
    "close_app": ("power", "Close"),
    "next_page": ("next_page", "Next"),
    "prev_page": ("prev_page", "Prev"),
    "goto_page": ("next_page", "Page"),
    "switch_profile": ("settings", "Profile"),
    "next_profile": ("next_page", "Scene ▶"),
    "prev_profile": ("prev_page", "Scene ◀"),
    "brightness": ("brightness_up", "Bright"),
    "sleep_screen": ("dot", "Sleep"),
    "open_folder": ("folder", "Folder"),
    "folder_back": ("prev_page", "Back"),
    "multi": ("star", "Multi"),
}


def default_icon_for(action) -> tuple[str, str]:
    """Best (library-icon-name, label) for an action, following its sub-command
    so e.g. Volume up/down/mute each get their own icon."""
    t = action.type
    p = action.params
    if t == "volume":
        return ({"up": "volume_up", "down": "volume_down", "mute": "mute"}
                .get(p.get("cmd", "up"), "volume_up"), "Volume")
    if t == "media":
        return ({"play-pause": "play", "next": "next", "previous": "prev",
                 "stop": "stop"}.get(p.get("cmd", "play-pause"), "play"), "Media")
    if t == "brightness":
        return ({"up": "brightness_up", "down": "brightness_down",
                 "set": "brightness_up"}.get(p.get("mode", "set"), "brightness_up"), "Bright")
    return ACTION_DEFAULT_ICON.get(t, ("", ""))


def _popen_detached(args, shell=False, host=False):
    """Launch a detached process. With host=True inside a Flatpak sandbox, run it
    on the host via flatpak-spawn so it can reach the user's real apps/scripts."""
    if IN_FLATPAK and host:
        args = _HOST_PREFIX + (["sh", "-c", args] if shell else list(args))
        shell = False
    subprocess.Popen(
        args, shell=shell, start_new_session=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
    )


def _run(args, **kw):
    """subprocess.run with a timeout + error guard so a hung helper (wpctl,
    playerctl, xdotool, …) can never freeze the action worker thread."""
    kw.setdefault("timeout", 8)
    kw.setdefault("stderr", subprocess.DEVNULL)
    try:
        subprocess.run(_host(args), **kw)
    except Exception as e:
        log.warning("command failed: %s", e)


# Linux input-event key codes for translating hotkey names -> ydotool keycodes.
_KEYCODES = {
    "ctrl": 29, "control": 29, "ctrl_r": 97, "shift": 42, "shift_r": 54,
    "alt": 56, "alt_r": 100, "altgr": 100, "super": 125, "meta": 125,
    "win": 125, "logo": 125,
    "esc": 1, "escape": 1, "tab": 15, "enter": 28, "return": 28, "space": 57,
    "backspace": 14, "delete": 111, "del": 111, "insert": 110, "ins": 110,
    "home": 102, "end": 107, "pageup": 104, "pgup": 104, "pagedown": 109,
    "pgdn": 109, "up": 103, "down": 108, "left": 105, "right": 106,
    "minus": 12, "-": 12, "equal": 13, "=": 13, "comma": 51, ",": 51,
    "dot": 52, "period": 52, ".": 52, "slash": 53, "/": 53,
    "semicolon": 39, ";": 39, "capslock": 58, "printscreen": 99, "print": 99,
}
for _i, _c in enumerate("1234567890"):
    _KEYCODES[_c] = 2 + _i
for _c, _v in {"a": 30, "b": 48, "c": 46, "d": 32, "e": 18, "f": 33, "g": 34,
               "h": 35, "i": 23, "j": 36, "k": 37, "l": 38, "m": 50, "n": 49,
               "o": 24, "p": 25, "q": 16, "r": 19, "s": 31, "t": 20, "u": 22,
               "v": 47, "w": 17, "x": 45, "y": 21, "z": 44}.items():
    _KEYCODES[_c] = _v
for _n in range(1, 11):
    _KEYCODES[f"f{_n}"] = 58 + _n            # F1=59 .. F10=68
_KEYCODES["f11"] = 87
_KEYCODES["f12"] = 88


def _ydotool_keycodes(combo: str):
    """Translate 'ctrl+shift+m' -> [(29),(42),(50)] input keycodes, or None."""
    codes = []
    for part in combo.split("+"):
        code = _KEYCODES.get(part.strip().lower())
        if code is None:
            return None
        codes.append(code)
    return codes


def _send_hotkey(combo: str) -> None:
    """Send a key combination like 'ctrl+shift+m'. Best-effort across tools."""
    combo = combo.strip()
    if not combo or not KEY_TOOL:
        if not KEY_TOOL:
            log.warning("no keystroke tool (install xdotool / ydotool / wtype)")
        return
    if KEY_TOOL == "xdotool":
        _run(["xdotool", "key", "--clearmodifiers", combo],
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
        _run(args, stderr=subprocess.DEVNULL)
    elif KEY_TOOL == "ydotool":
        # ydotool needs numeric keycodes: press all down (in order), release up.
        codes = _ydotool_keycodes(combo)
        if not codes:
            log.warning("hotkey '%s': unknown key name for ydotool", combo)
            return
        seq = [f"{c}:1" for c in codes] + [f"{c}:0" for c in reversed(codes)]
        _run(["ydotool", "key", *seq], stderr=subprocess.DEVNULL)


def _type_text(text: str) -> None:
    if not KEY_TOOL:
        return
    if KEY_TOOL == "xdotool":
        _run(["xdotool", "type", "--clearmodifiers", "--", text],
                       stderr=subprocess.DEVNULL)
    elif KEY_TOOL == "wtype":
        _run(["wtype", "--", text], stderr=subprocess.DEVNULL)
    elif KEY_TOOL == "ydotool":
        _run(["ydotool", "type", "--", text], stderr=subprocess.DEVNULL)


def _close_app(target: str) -> None:
    """Close an app by window title/class (wmctrl) or process name (pkill)."""
    target = target.strip()
    if not target:
        return
    if _has("wmctrl"):
        _run(["wmctrl", "-c", target], stderr=subprocess.DEVNULL)
    elif _has("pkill"):
        _run(["pkill", "-f", target], stderr=subprocess.DEVNULL)
    else:
        log.warning("close needs 'wmctrl' or 'pkill'")


def _media(cmd: str) -> None:
    if HAS_PLAYERCTL:
        _run(["playerctl", cmd], stderr=subprocess.DEVNULL)
    else:
        log.warning("media control needs 'playerctl'")


SINK = "@DEFAULT_AUDIO_SINK@"


def _volume(cmd: str, step: str) -> None:
    try:
        pct = int(str(step or "5").strip().rstrip("%"))
    except ValueError:
        pct = 5
    if AUDIO == "pipewire":
        if cmd == "up":
            _run(["wpctl", "set-volume", "-l", "1.5", SINK, f"{pct}%+"])
        elif cmd == "down":
            _run(["wpctl", "set-volume", SINK, f"{pct}%-"])
        elif cmd == "mute":
            _run(["wpctl", "set-mute", SINK, "toggle"])
    elif AUDIO == "pulseaudio":
        s = "@DEFAULT_SINK@"
        if cmd == "up":
            _run(["pactl", "set-sink-volume", s, f"+{pct}%"])
        elif cmd == "down":
            _run(["pactl", "set-sink-volume", s, f"-{pct}%"])
        elif cmd == "mute":
            _run(["pactl", "set-sink-mute", s, "toggle"])
    else:
        log.warning("volume control needs pipewire (wpctl) or pulseaudio (pactl)")


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
                _popen_detached(cmd, shell=True, host=True)
        elif t == "run_command":
            cmd = p.get("command", "").strip()
            if cmd:
                _popen_detached(cmd, shell=True, host=True)
        elif t == "open_url":
            url = p.get("url", "").strip()
            if url:
                _popen_detached(["xdg-open", url])
        elif t == "hotkey":
            _send_hotkey(p.get("keys", ""))
        elif t == "text":
            _type_text(p.get("text", ""))
        elif t == "password":
            _type_text(p.get("password", ""))
        elif t == "media":
            _media(p.get("cmd", "play-pause"))
        elif t == "volume":
            _volume(p.get("cmd", "up"), p.get("step", "5"))
        elif t == "close_app":
            _close_app(p.get("target", ""))
        elif t == "sleep_screen" and context:
            context.sleep_screen()
        elif t == "next_profile" and context:
            context.next_profile()
        elif t == "prev_profile" and context:
            context.prev_profile()
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
            log.warning("unhandled or context-less action: %s", t)
    except Exception as e:  # actions must never crash the reader thread
        log.error("'%s' failed: %s", t, e)


def environment_summary() -> str:
    return (f"session={'wayland' if IS_WAYLAND else 'x11'} "
            f"audio={AUDIO or 'none'} keytool={KEY_TOOL or 'none'} "
            f"playerctl={'yes' if HAS_PLAYERCTL else 'no'}"
            + (" [flatpak]" if IN_FLATPAK else ""))
