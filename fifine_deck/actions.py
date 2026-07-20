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
from typing import Optional, Protocol

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

# Confined snap: USB access needs the raw-usb / hardware-observe interfaces,
# which are manual-connect by default (a snap cannot connect them to itself),
# so the device is inert until the user runs `snap connect`.
IN_SNAP = bool(os.environ.get("SNAP") and os.environ.get("SNAP_NAME"))


def _snap_is_classic() -> bool:
    """True if this snap was built with classic confinement (reads meta/snap.yaml)."""
    snap = os.environ.get("SNAP")
    if not snap:
        return False
    try:
        with open(os.path.join(snap, "meta", "snap.yaml"), encoding="utf-8") as f:
            return any(line.strip() == "confinement: classic" for line in f)
    except OSError:
        return False


# A classic snap CAN open /dev/hidraw directly, but only if the host has the
# udev rule (a snap cannot install one) — so its guidance differs from strict.
IN_SNAP_CLASSIC = IN_SNAP and _snap_is_classic()


# The portals-first Flatpak manifest no longer requests
# org.freedesktop.Flatpak by default (Flathub review equates that grant with
# no sandbox), so host access is an explicit USER decision. This is the exact
# line the app tells them about when a host-side action needs it:
HOST_ACCESS_HINT = (
    "Host access is not enabled for this Flatpak, so actions that run host "
    "commands (launch app, shell command, hotkeys, media and volume tools) "
    "are unavailable. Enable it once with: flatpak override --user "
    "--talk-name=org.freedesktop.Flatpak io.github.zoutmax.FifineControlDeck "
    "(or turn on 'Talk: org.freedesktop.Flatpak' in Flatseal), then restart "
    "the app."
)

_host_access: bool | None = None


def host_access_available() -> bool:
    """Inside Flatpak: can flatpak-spawn actually reach the host? Probed once
    per process (the grant cannot change under a running sandbox). Outside a
    sandbox this is trivially True."""
    global _host_access
    if not IN_FLATPAK:
        return True
    if _host_access is None:
        try:
            r = subprocess.run(_HOST_PREFIX + ["true"], timeout=5,
                               stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
            _host_access = r.returncode == 0
        except Exception:
            _host_access = False
        if not _host_access:
            log.warning(HOST_ACCESS_HINT)
    return _host_access


def _host(args):
    """Prefix an argv list so it runs on the host when inside a Flatpak
    sandbox. Raises with the enable-me hint when the sandbox has no host
    grant: a clear message in the action log beats a silent portal error."""
    if IN_FLATPAK:
        if not host_access_available():
            raise RuntimeError(HOST_ACCESS_HINT)
        return _HOST_PREFIX + list(args)
    return list(args)


# Tools that do their job correctly from INSIDE the sandbox, because what
# they drive is reachable through a granted socket rather than the host
# filesystem: the audio CLIs talk to PipeWire/PulseAudio over
# --socket=pulseaudio. Everything else (input injection, window management,
# launching the user's apps) is only meaningful on the host.
SANDBOX_CAPABLE = frozenset({"pactl", "wpctl"})


def _has(cmd: str) -> bool:
    """Is `cmd` usable? Inside Flatpak, look in the SANDBOX first for the
    tools that work there (the KDE runtime ships pactl, so volume control
    needs no host access at all), then probe the HOST for the rest."""
    if IN_FLATPAK and cmd in SANDBOX_CAPABLE and shutil.which(cmd):
        return True
    if IN_FLATPAK and not host_access_available():
        return False           # no route to the host: nothing else is usable
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
    "monitor":       {"label": "System monitor", "params": [
        ("metric", "choice:cpu,ram,vram,gpu,gputemp,temp,net,disk,clock", "Metric"),
        ("style", "choice:number,gauge,graph", "Style"),
        ("interval", "text", "Refresh every (seconds)"),
        ("target", "text", "Disk mount / net iface / temp sensor"),
        ("clock_format", "choice:auto,24h,24h+seconds,12h,12h+seconds", "Clock format"),
        ("clock_date", "choice:auto,iso,us,none", "Clock date"),
    ]},
    "open_folder":   {"label": "Open folder", "params": []},
    "folder_back":   {"label": "Back (exit folder)", "params": []},
    "multi":         {"label": "Multi-action (steps)", "params": []},  # edited specially
}


# Catalog grouping for the drag-and-drop sidebar: (category, [action types]).
ACTION_CATALOG = [
    ("Application", ["launch_app", "run_command", "open_url", "close_app"]),
    ("Keyboard",    ["hotkey", "text", "password"]),
    ("Media",       ["media", "volume"]),
    ("System",      ["monitor"]),
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
    # monitor: no icon/label on purpose — the live readout IS the key face,
    # and a library icon would overpaint it between ticks.
    "monitor": ("", ""),
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
        if not host_access_available():
            raise RuntimeError(HOST_ACCESS_HINT)
        args = _HOST_PREFIX + (["sh", "-c", args] if shell else list(args))
        shell = False
    subprocess.Popen(
        args, shell=shell, start_new_session=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
    )


def _run(args, input_text: bytes | None = None, **kw):
    """subprocess.run with a timeout + error guard so a hung helper (wpctl,
    playerctl, xdotool, …) can never freeze the action worker thread.

    `input_text` is written to the child's stdin. Anything secret MUST travel
    this way and never in `args`: /proc/<pid>/cmdline is world-readable, so a
    password in argv is readable by every process on the machine (and by any
    `ps`/monitoring sample) for the lifetime of the helper. The failure log
    below prints the exception, which carries argv — another reason the secret
    must not be there.
    """
    kw.setdefault("timeout", 8)
    kw.setdefault("stderr", subprocess.DEVNULL)
    try:
        subprocess.run(_host(args), input=input_text, **kw)
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
    if not combo:
        return
    if not KEY_TOOL:
        # No helper tool: ask the compositor to inject the keys for us.
        # This is the only route inside a Flatpak, and it also rescues a
        # plain Wayland desktop where ydotool was never set up.
        from . import portal_input
        codes = _ydotool_keycodes(combo)
        if codes and portal_input.send_combo(codes):
            return
        log.warning("no keystroke tool (install xdotool / ydotool / wtype) "
                    "and the RemoteDesktop portal is unavailable")
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
    """Type `text` into the focused window.

    The text goes in on stdin, never argv. This is the same path the "type
    password" action takes, and argv is world-readable through
    /proc/<pid>/cmdline — putting the secret there would undo everything
    secret_store.py does to keep it off disk. All three helpers support it:
    xdotool via `--file -`, wtype via a bare `-`, ydotool via
    `--file /dev/stdin`.

    ydotool gets /dev/stdin rather than "-": 1.0.x treats "-" as stdin, but
    legacy 0.1.8 (jammy, still a supported .deb target) fopen()s a literal
    file named "-" and silently types nothing. /dev/stdin works with every
    implementation that opens the argument as a path.

    Reading from stdin also disables ydotool's escape handling, so text is
    typed literally (`\\n` stays two characters); a real newline still presses
    Return, which is what the multi-line editor produces.
    """
    if not KEY_TOOL:
        from . import portal_input
        if not portal_input.type_text(text):
            log.warning("no keystroke tool and the RemoteDesktop portal is "
                        "unavailable: cannot type")
        return
    data = text.encode()
    if KEY_TOOL == "xdotool":
        _run(["xdotool", "type", "--clearmodifiers", "--file", "-"], input_text=data)
    elif KEY_TOOL == "wtype":
        _run(["wtype", "-"], input_text=data)
    elif KEY_TOOL == "ydotool":
        # /dev/stdin, not "-": see the docstring — legacy ydotool 0.1.8
        # fopen()s a literal "-" and silently types nothing.
        _run(["ydotool", "type", "--file", "/dev/stdin"], input_text=data)


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
    """Control the active player.

    MPRIS over D-Bus first: every modern player speaks it, so this works with
    no helper installed and inside a sandbox (where there is no host
    playerctl and reaching the host is the permission Flathub rejects).
    playerctl stays as the fallback for the rare player that only it knows.
    """
    from . import mpris
    if mpris.control(cmd):
        return
    if HAS_PLAYERCTL:
        _run(["playerctl", cmd], stderr=subprocess.DEVNULL)
    else:
        log.warning("media control found no MPRIS player (and no playerctl)")


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
            from . import secret_store
            pw = p.get("password") or (
                secret_store.get(p["secret_id"]) if p.get("secret_id") else "")
            _type_text(pw or "")
        elif t == "media":
            _media(p.get("cmd", "play-pause"))
        elif t == "volume":
            _volume(p.get("cmd", "up"), p.get("step", "5"))
        elif t == "close_app":
            _close_app(p.get("target", ""))
        elif t == "monitor":
            return    # display-only key: pressing it does nothing
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
            + (" [flatpak]" if IN_FLATPAK else "")
            + (" [snap]" if IN_SNAP else ""))


def snap_usb_hint() -> Optional[str]:
    """Guidance for granting a snap access to the USB device.

    Returns None unless running as a snap. The deck is driven over /dev/hidraw:
    - classic snap  -> can open it directly, but needs the host udev rule (a snap
      cannot install one); the rule is bundled at $SNAP/udev/70-fifine-deck.rules.
    - strict snap   -> hidraw cannot be granted at all; this build should not be
      used for device control (kept for completeness).
    """
    if not IN_SNAP:
        return None
    name = os.environ.get("SNAP_NAME", "fifine-control-deck")
    if IN_SNAP_CLASSIC:
        rule = os.path.join(os.environ.get("SNAP", ""), "udev", "70-fifine-deck.rules")
        return (
            "The deck is controlled over /dev/hidraw, which needs a udev rule so "
            "this snap can open it (a snap can't install the rule itself).\n\n"
            "If your deck is plugged in but not detected, run this once, then "
            "unplug/replug the device:\n\n"
            f"    sudo cp {rule} /etc/udev/rules.d/\n"
            "    sudo udevadm control --reload-rules && sudo udevadm trigger\n\n"
            "Tip: the .deb / PPA build installs this rule for you."
        )
    return (
        "This is the strict-confinement snap, which cannot access /dev/hidraw and "
        "so cannot control the deck. Install the classic snap, or the .deb / PPA:\n\n"
        "    sudo add-apt-repository ppa:zoutmax/fifine\n"
        "    sudo apt install fifine-control-deck\n\n"
        f"(For reference, USB interfaces on this build: "
        f"`sudo snap connect {name}:raw-usb`, `:hardware-observe`.)"
    )


def can_install_udev_rule() -> bool:
    """True if the one-click 'enable device access' path is available.

    Only the classic snap ships the bundled rule + helper and runs unconfined
    enough to call pkexec.
    """
    if not IN_SNAP_CLASSIC:
        return False
    helper = os.path.join(os.environ.get("SNAP", ""), "bin", "fifine-install-udev-rule")
    return os.path.exists(helper)


def install_udev_rule_pkexec() -> tuple[bool, str]:
    """Install the bundled udev rule as root via pkexec (graphical auth prompt).

    A snap can't install a udev rule itself, so the classic snap ships the rule
    and a small helper and elevates via polkit. Returns (ok, message); a
    non-zero exit means the user cancelled the auth dialog or it failed.
    """
    if not can_install_udev_rule():
        return (False, "The one-click installer is only available in the classic snap.")
    helper = os.path.join(os.environ["SNAP"], "bin", "fifine-install-udev-rule")
    # Call pkexec by absolute path — the snap's PATH may not include it, and the
    # real setuid binary lives at /usr/bin/pkexec (a symlink is fine too).
    pkexec = "/usr/bin/pkexec" if os.path.exists("/usr/bin/pkexec") else (
        shutil.which("pkexec") or "pkexec")
    try:
        r = subprocess.run(
            [pkexec, helper],
            capture_output=True, text=True, timeout=120,
        )
    except FileNotFoundError:
        return (False, "pkexec is not available on this system.")
    except subprocess.TimeoutExpired:
        return (False, "Timed out waiting for authentication.")
    if r.returncode == 0:
        return (True, "Device-access rule installed. Reconnecting to the deck…")
    if r.returncode in (126, 127):   # pkexec: dismissed / not authorized
        return (False, "Authentication was cancelled.")
    return (False, (r.stderr or r.stdout or "Could not install the rule.").strip())
