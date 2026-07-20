"""Media control over MPRIS, spoken directly on the session bus.

Every modern Linux media player (Spotify, VLC, mpv, browsers, Rhythmbox...)
exposes the MPRIS2 interface on D-Bus. Talking to it ourselves removes the
`playerctl` dependency entirely, which matters twice over:

- inside a Flatpak there is no host `playerctl`, and shelling out to the host
  is exactly the permission Flathub review rejects; a `--talk-name` for
  `org.mpris.MediaPlayer2.*` is standard and granted routinely;
- outside a sandbox, media keys now work on a plain desktop where the user
  never installed playerctl.

Only blocking method calls are used (no signals, no nested event loop), so
this is safe to call from the controller's action worker thread.
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)

MPRIS_PREFIX = "org.mpris.MediaPlayer2."
OBJECT_PATH = "/org/mpris/MediaPlayer2"
PLAYER_IFACE = "org.mpris.MediaPlayer2.Player"

# action command -> MPRIS method
COMMANDS = {
    "play-pause": "PlayPause",
    "next": "Next",
    "previous": "Previous",
    "stop": "Stop",
}


def _bus():
    """The session bus, or None when D-Bus/PyQt6 is unavailable."""
    try:
        from PyQt6.QtDBus import QDBusConnection
        bus = QDBusConnection.sessionBus()
        return bus if bus.isConnected() else None
    except Exception:
        return None


def _players(bus) -> list[str]:
    """Bus names of every running MPRIS player."""
    from PyQt6.QtDBus import QDBusInterface
    iface = QDBusInterface("org.freedesktop.DBus", "/org/freedesktop/DBus",
                           "org.freedesktop.DBus", bus)
    reply = iface.call("ListNames")
    if reply.errorName():
        return []
    args = reply.arguments()
    names = args[0] if args and isinstance(args[0], list) else []
    return [n for n in names if isinstance(n, str) and n.startswith(MPRIS_PREFIX)]


def _playback_status(bus, name: str) -> str:
    from PyQt6.QtDBus import QDBusInterface
    props = QDBusInterface(name, OBJECT_PATH,
                           "org.freedesktop.DBus.Properties", bus)
    reply = props.call("Get", PLAYER_IFACE, "PlaybackStatus")
    if reply.errorName():
        return ""
    args = reply.arguments()
    return str(args[0]) if args and isinstance(args[0], str) else ""


def _target(bus, cmd: str) -> str:
    """Which player to command.

    Prefer one that is actually Playing, which is what a user means by "next
    track" when several players are open. For play-pause fall back to a
    Paused player (that is the one they want to resume), then to any player
    at all, matching playerctl's own precedence.
    """
    names = _players(bus)
    if not names:
        return ""
    paused = ""
    for name in names:
        status = _playback_status(bus, name)
        if status == "Playing":
            return name
        if status == "Paused" and not paused:
            paused = name
    return paused or names[0]


def available() -> bool:
    """True when at least one MPRIS player is running right now."""
    bus = _bus()
    return bool(bus is not None and _players(bus))


def control(cmd: str) -> bool:
    """Run a media command. Returns True when a player accepted it."""
    method = COMMANDS.get(cmd)
    if method is None:
        return False
    bus = _bus()
    if bus is None:
        return False
    name = _target(bus, cmd)
    if not name:
        return False
    from PyQt6.QtDBus import QDBusInterface
    player = QDBusInterface(name, OBJECT_PATH, PLAYER_IFACE, bus)
    reply = player.call(method)
    if reply.errorName():
        log.warning("mpris %s on %s: %s", method, name, reply.errorMessage())
        return False
    return True
