"""Keystroke injection through the XDG RemoteDesktop portal.

The hotkey and type-text actions normally shell out to xdotool / ydotool /
wtype. That fails in two situations this module fixes:

- inside a Flatpak there are no host tools, and reaching the host is the
  permission Flathub review rejects;
- on a plain Wayland desktop where the user never set up ydotool (it needs a
  daemon plus uinput access), hotkeys simply do not work today.

org.freedesktop.portal.RemoteDesktop solves both: the compositor injects the
events for us after the user consents once. `persist_mode=2` plus a stored
restore token means that consent survives restarts.

THREADING: creating the session runs a nested Qt event loop, so prime() must
be called from the main thread at startup (the controller dispatches actions
on a worker thread). Once the session exists, the Notify* calls are plain
D-Bus messages and are safe from any thread.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

log = logging.getLogger(__name__)

# RemoteDesktop device types (bitmask): 1 = keyboard, 2 = pointer.
DEVICE_KEYBOARD = 1
# persist_mode 2 = keep the permission until the user revokes it.
PERSIST_UNTIL_REVOKED = 2

_SESSION: Optional[str] = None
_TRIED = False
_STATE_PRESSED = 1
_STATE_RELEASED = 0


def _token_path() -> str:
    """Where the restore token lives, so consent is asked once ever."""
    from .model import CONFIG_DIR
    return os.path.join(CONFIG_DIR, "remote-desktop.token")


def _load_token() -> str:
    try:
        with open(_token_path()) as f:
            return f.read().strip()
    except OSError:
        return ""


def _save_token(token: str) -> None:
    if not token:
        return
    try:
        from .model import ensure_dirs
        ensure_dirs()
        fd = os.open(_token_path(), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(token)
    except OSError as e:
        log.warning("remote desktop: could not store the restore token: %s", e)


def _keysym_for(ch: str) -> int:
    """X11 keysym for a character. Latin-1 maps directly; everything else
    uses the Unicode plane the keysym spec reserves for it."""
    cp = ord(ch)
    if cp < 0x100:
        return cp
    return cp | 0x01000000


def _call_portal(bus, iface_name: str, method: str, args: list, token: str):
    """Blocking portal call that waits for the async Response signal.

    Same Request/Response dance as the Background and Secret portals: build
    the predictable request path from our bus name plus the handle token,
    subscribe BEFORE calling so a fast reply cannot be missed, then spin a
    nested loop until the response arrives.
    """
    from PyQt6.QtCore import QEventLoop, QObject, QTimer, pyqtSlot
    from PyQt6.QtDBus import QDBusInterface, QDBusMessage

    sender = bus.baseService().lstrip(":").replace(".", "_")
    req_path = f"/org/freedesktop/portal/desktop/request/{sender}/{token}"
    loop = QEventLoop()

    class _Responder(QObject):
        answered = False
        code = 1
        results: dict = {}

        @pyqtSlot(QDBusMessage)
        def handle(self, msg: QDBusMessage) -> None:
            a = msg.arguments()
            self.answered = True
            self.code = int(a[0]) if a else 1
            self.results = a[1] if len(a) > 1 and isinstance(a[1], dict) else {}
            loop.quit()

    responder = _Responder()
    bus.connect("org.freedesktop.portal.Desktop", req_path,
                "org.freedesktop.portal.Request", "Response", responder.handle)
    try:
        iface = QDBusInterface("org.freedesktop.portal.Desktop",
                               "/org/freedesktop/portal/desktop",
                               iface_name, bus)
        reply = iface.call(method, *args)
        if reply.errorName():
            log.warning("remote desktop %s: %s", method, reply.errorMessage())
            return None
        # Generous: Start() shows a consent dialog the user must answer.
        QTimer.singleShot(120_000, loop.quit)
        loop.exec()
        if not responder.answered:
            QDBusInterface("org.freedesktop.portal.Desktop", req_path,
                           "org.freedesktop.portal.Request", bus).call("Close")
            log.warning("remote desktop %s: no response", method)
            return None
        if responder.code != 0:
            log.info("remote desktop %s: declined (code %s)", method,
                     responder.code)
            return None
        return responder.results
    finally:
        bus.disconnect("org.freedesktop.portal.Desktop", req_path,
                       "org.freedesktop.portal.Request", "Response",
                       responder.handle)


def _create_session() -> Optional[str]:
    """CreateSession, SelectDevices(keyboard), Start. Returns the handle."""
    import sys

    from PyQt6.QtCore import QCoreApplication, QMetaType
    from PyQt6.QtDBus import QDBusArgument, QDBusConnection

    from .app import _portal_token_seq

    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication(sys.argv[:1])
    _ = app
    bus = QDBusConnection.sessionBus()
    if not bus.isConnected():
        return None

    def tok() -> str:
        return f"fifinedeck{os.getpid()}_{next(_portal_token_seq)}"

    t1 = tok()
    res = _call_portal(bus, "org.freedesktop.portal.RemoteDesktop",
                       "CreateSession",
                       [{"handle_token": t1, "session_handle_token": t1}], t1)
    if not res or "session_handle" not in res:
        return None
    session = str(res["session_handle"])

    t2 = tok()
    opts = {
        "handle_token": t2,
        # uint32 on the wire: PyQt marshals a bare int as int32 and the
        # portal's strict option filter rejects the whole call.
        "types": QDBusArgument(DEVICE_KEYBOARD, QMetaType.Type.UInt.value),  # type: ignore[call-overload]
        "persist_mode": QDBusArgument(PERSIST_UNTIL_REVOKED,
                                      QMetaType.Type.UInt.value),  # type: ignore[call-overload]
    }
    restore = _load_token()
    if restore:
        opts["restore_token"] = restore
    if _call_portal(bus, "org.freedesktop.portal.RemoteDesktop",
                    "SelectDevices", [session, opts], t2) is None:
        return None

    t3 = tok()
    started = _call_portal(bus, "org.freedesktop.portal.RemoteDesktop",
                           "Start", [session, "", {"handle_token": t3}], t3)
    if started is None:
        return None
    if started.get("restore_token"):
        _save_token(str(started["restore_token"]))
    return session


def prime() -> bool:
    """Establish the portal session. Main thread, once, at startup."""
    global _SESSION, _TRIED
    if not _TRIED:
        import threading
        if threading.current_thread() is not threading.main_thread():
            log.warning("remote desktop: not primed before use off the main "
                        "thread; skipping")
            return False
        _TRIED = True
        try:
            _SESSION = _create_session()
        except Exception as e:
            log.warning("remote desktop: %s", e)
            _SESSION = None
    return _SESSION is not None


def available() -> bool:
    return _SESSION is not None


def _notify(method: str, value: int, state: int) -> bool:
    if _SESSION is None:
        return False
    from PyQt6.QtCore import QMetaType
    from PyQt6.QtDBus import QDBusArgument, QDBusConnection, QDBusInterface
    bus = QDBusConnection.sessionBus()
    iface = QDBusInterface("org.freedesktop.portal.Desktop",
                           "/org/freedesktop/portal/desktop",
                           "org.freedesktop.portal.RemoteDesktop", bus)
    reply = iface.call(method, _SESSION, {}, value,
                       QDBusArgument(state, QMetaType.Type.UInt.value))  # type: ignore[call-overload]
    if reply.errorName():
        log.warning("remote desktop %s: %s", method, reply.errorMessage())
        return False
    return True


def send_combo(keycodes: list[int]) -> bool:
    """Press every keycode in order, then release in reverse (evdev codes,
    the same numbers the ydotool path uses)."""
    if _SESSION is None or not keycodes:
        return False
    ok = True
    for code in keycodes:
        ok = _notify("NotifyKeyboardKeycode", code, _STATE_PRESSED) and ok
    for code in reversed(keycodes):
        ok = _notify("NotifyKeyboardKeycode", code, _STATE_RELEASED) and ok
    return ok


def type_text(text: str) -> bool:
    """Type text by keysym, so any character works regardless of layout."""
    if _SESSION is None or not text:
        return False
    ok = True
    for ch in text:
        sym = 0xFF0D if ch == "\n" else _keysym_for(ch)   # Return
        ok = _notify("NotifyKeyboardKeysym", sym, _STATE_PRESSED) and ok
        ok = _notify("NotifyKeyboardKeysym", sym, _STATE_RELEASED) and ok
    return ok
