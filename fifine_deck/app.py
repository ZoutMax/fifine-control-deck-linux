"""Application entry point: `python -m fifine_deck` (GUI) or `--headless`."""
from __future__ import annotations

import argparse
import itertools
import logging
import signal
import sys

from .model import DeckConfig, ensure_dirs
from .controller import DeckController


import os

_IPC_NAME = f"fifine-control-deck-{os.getuid()}"


def _runtime_dir() -> str:
    """Directory for the single-instance socket + lock. XDG_RUNTIME_DIR is
    0700 and user-owned — immune to another local user squatting our fixed
    name. In world-writable /tmp, anyone could pre-bind the socket and
    silently swallow every launch (each one would "hand off" to the squatter
    and exit 0 with no app running). /tmp stays as the fallback for exotic
    sessions without a runtime dir."""
    d = os.environ.get("XDG_RUNTIME_DIR")
    if d:
        try:
            st = os.stat(d)
            if os.path.isdir(d) and st.st_uid == os.getuid():
                return d
        except OSError:
            pass
    import tempfile
    return tempfile.gettempdir()


def _ipc_socket_path() -> str:
    """The QLocalServer socket file, as an ABSOLUTE path. Passing the full
    path to listen()/connectToServer() keeps every participant — server,
    client, and the --quit liveness poll — on the same file even when their
    TMPDIR environments differ (a daemon autostarted by the session has no
    TMPDIR; a shell may export one). Its existence is a poke-free liveness
    signal."""
    return os.path.join(_runtime_dir(), _IPC_NAME)


def _liveness_paths() -> set:
    """Every place a running instance's socket file may live: the current
    home (absolute path) and the pre-0.8.2 temp-dir location, so a --quit
    sent to an old instance across an upgrade still sees it exit."""
    import tempfile
    return {_ipc_socket_path(), os.path.join(tempfile.gettempdir(), _IPC_NAME)}


def _acquire_instance_lock():
    """Atomic single-instance claim, via flock on a lock file next to the
    socket. The socket alone cannot be the claim: two racing launches can
    both fail listen() on a stale socket, and the loser's removeServer()
    then unlinks the WINNER's live socket — two instances end up running,
    fighting over the device and clobbering each other's config saves
    (0.8.1 audit). flock is atomic and evaporates with the process, so a
    crash leaves nothing to mis-diagnose. Returns the held fd (keep it open
    for the process lifetime), or None when another live instance holds it."""
    import fcntl
    fd = os.open(_ipc_socket_path() + ".lock",
                 os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd

log = logging.getLogger(__name__)


def _configure_logging() -> None:
    """Configure root logging; level from $FIFINE_LOG (default INFO)."""
    level = os.environ.get("FIFINE_LOG", "INFO").upper()
    logging.basicConfig(level=getattr(logging, level, logging.INFO),
                        format="%(levelname)s %(name)s: %(message)s")


def _signal_existing(command: str) -> bool:
    """If an instance is already running, send it a command and return True.

    Tries the current socket (absolute path in the runtime dir) first, then
    the pre-0.8.2 location (bare name — Qt resolves it in ITS temp dir,
    exactly as old builds did) so 'show'/'--quit' still reach an instance
    that survived an upgrade."""
    from PyQt6.QtNetwork import QLocalSocket
    for target in (_ipc_socket_path(), _IPC_NAME):
        sock = QLocalSocket()
        sock.connectToServer(target)
        if sock.waitForConnected(250):
            sock.write(command.encode())
            sock.flush()
            sock.waitForBytesWritten(500)
            sock.disconnectFromServer()
            return True
    return False


def autostart_file() -> str:
    """The XDG autostart entry path. Honors XDG_CONFIG_HOME (outside Flatpak
    that is the user's real ~/.config; inside it points into the sandbox,
    which is why the Flatpak path uses the Background portal instead)."""
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "autostart", "fifine-control-deck.desktop")


_AUTOSTART_ENTRY = """[Desktop Entry]
Type=Application
Name=fifine Control Deck
Comment=Keep the deck active on login (window hidden)
Exec=fifine-control-deck --hidden
Icon=fifine-control-deck
Terminal=false
X-GNOME-Autostart-enabled=true
"""


def set_autostart(enable: bool, config=None) -> int:
    """Enable/disable start-on-login. Returns 0 on success, non-zero when the
    request was denied (Flatpak portal) so the GUI can revert its toggle.

    Outside a sandbox this writes/removes the XDG autostart .desktop entry.
    Inside Flatpak that file would land in the sandbox home and never run, so
    the request goes through the org.freedesktop.portal.Background portal,
    which manages a host-side autostart entry on our behalf.

    The portal has no query API, so DeckConfig.autostart_enabled is the
    toggle's only memory of the granted state. The GUI passes its live
    `config` (and persists it itself); the CLI passes none, so a granted
    request is persisted here — otherwise `--enable-autostart` would leave
    the next GUI launch showing a stale toggle.
    """
    from .actions import IN_FLATPAK
    if IN_FLATPAK:
        if _portal_autostart(enable):
            state = "enabled" if enable else "disabled"
            print(f"Autostart {state} via the Background portal.")
            if config is not None:
                config.autostart_enabled = enable      # caller persists
            else:
                from .model import DeckConfig
                cfg = DeckConfig.load()
                cfg.autostart_enabled = enable
                cfg.save()
            return 0
        print("The desktop denied the Background-portal autostart request.")
        return 1
    path = autostart_file()
    if enable:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(_AUTOSTART_ENTRY)
        print(f"Autostart enabled: {path}")
        print("The deck will run (hidden) on login; open the window by launching the app.")
    else:
        try:
            os.remove(path)
            print("Autostart disabled.")
        except FileNotFoundError:
            print("Autostart was not enabled.")
    return 0


_portal_token_seq = itertools.count()


def _portal_autostart(enable: bool) -> bool:
    """Ask the XDG Background portal to (un)register login autostart.

    Blocks on a nested event loop until the portal's async Response arrives
    (usually instant; some desktops show a consent dialog). Returns True when
    the request was granted. Needs a Qt application object — the GUI always
    has one; the CLI path creates one and HOLDS it (an unreferenced
    QCoreApplication is destroyed immediately by PyQt, which kills both the
    event loop and the timeout guard).
    """
    from PyQt6.QtCore import (QCoreApplication, QEventLoop, QMetaType,
                              QObject, QTimer, pyqtSlot)
    from PyQt6.QtDBus import (QDBusArgument, QDBusConnection, QDBusInterface,
                              QDBusMessage)

    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication(sys.argv[:1])    # held until we return (CLI)
    _ = app
    bus = QDBusConnection.sessionBus()
    if not bus.isConnected():
        log.warning("portal autostart: no D-Bus session bus")
        return False

    # The portal replies via a Response signal on a Request object whose path
    # is predictable from our unique name + handle_token — subscribe BEFORE
    # calling so a fast reply cannot be missed (the spec's documented race).
    # The token must be unique per request: two in-flight requests sharing a
    # path would adopt each other's responses.
    token = f"fifinedeck{os.getpid()}_{next(_portal_token_seq)}"
    sender = bus.baseService().lstrip(":").replace(".", "_")
    req_path = f"/org/freedesktop/portal/desktop/request/{sender}/{token}"

    loop = QEventLoop()

    class _Responder(QObject):
        granted = False
        answered = False

        @pyqtSlot(QDBusMessage)
        def handle(self, msg: QDBusMessage) -> None:
            args = msg.arguments()
            code = int(args[0]) if args else 1
            results = args[1] if len(args) > 1 and isinstance(args[1], dict) else {}
            ok = code == 0
            if enable and "autostart" in results:
                ok = ok and bool(results["autostart"])
            self.answered = True
            self.granted = ok
            loop.quit()

    responder = _Responder()
    bus.connect("org.freedesktop.portal.Desktop", req_path,
                "org.freedesktop.portal.Request", "Response",
                responder.handle)
    try:
        iface = QDBusInterface("org.freedesktop.portal.Desktop",
                               "/org/freedesktop/portal/desktop",
                               "org.freedesktop.portal.Background", bus)
        reply = iface.call("RequestBackground", "", {
            "handle_token": token,
            "reason": "Start the deck controller on login",
            "autostart": enable,
            # Explicitly typed as a string array: PyQt6 otherwise marshals a
            # Python list as 'av', and xdg-desktop-portal's strict option
            # filter rejects the whole call ("expected 'as', found 'av'").
            # stubs mistype the enum's .value; the wire type is pinned by
            # tests/test_portal_wire.py against a validating fake portal
            "commandline": QDBusArgument(["fifine-control-deck", "--hidden"],
                                         QMetaType.Type.QStringList.value),  # type: ignore[call-overload]
            "dbus-activatable": False,
        })
        if reply.errorName():
            log.warning("portal autostart: %s", reply.errorMessage())
            return False

        # 30 s guards against a portal that never answers; a visible consent
        # dialog quits the loop the moment the user decides.
        QTimer.singleShot(30_000, loop.quit)
        loop.exec()
        if not responder.answered:
            # Timed out. Close the pending Request so a LATE "Allow" in a
            # still-open consent dialog cannot flip host state that our
            # toggle/config no longer track (the portal has no query API).
            QDBusInterface("org.freedesktop.portal.Desktop", req_path,
                           "org.freedesktop.portal.Request", bus).call("Close")
            log.warning("portal autostart: no response from the portal (30s)")
        return responder.granted
    finally:
        bus.disconnect("org.freedesktop.portal.Desktop", req_path,
                       "org.freedesktop.portal.Request", "Response",
                       responder.handle)


def run_gui(quit_flag: bool = False, hidden: bool = False) -> int:
    from PyQt6.QtWidgets import QApplication
    from PyQt6.QtGui import QIcon, QGuiApplication
    from PyQt6.QtNetwork import QLocalServer
    from .gui.main_window import MainWindow
    from .gui.style import STYLESHEET
    from . import assets

    # Single instance: hand off to a running copy instead of starting a second
    # one (a second copy could not open the already-claimed device anyway).
    if _signal_existing("quit" if quit_flag else "show"):
        if quit_flag:
            # Wait for the instance to actually exit: returning while it is
            # still shutting down makes "quit && relaunch" a race — the new
            # launch defers to the dying instance and the user keeps running
            # stale code (bit us twice during 0.8.1 testing). Poll the IPC
            # socket FILE, never connect: older versions treat any incoming
            # connection as "show", which interrupts their own shutdown.
            import time as _time
            candidates = _liveness_paths()
            deadline = _time.monotonic() + 10.0
            while _time.monotonic() < deadline:
                if not any(os.path.exists(p) for p in candidates):
                    print("Running instance stopped.")
                    return 0
                _time.sleep(0.2)
            print("Signalled quit, but the instance is still running "
                  "after 10s.", file=sys.stderr)
            return 1
        print("Signalled the running instance; exiting.")
        return 0
    if quit_flag:
        print("No running instance to quit.")
        return 0

    # Set identity BEFORE constructing QApplication so the Wayland /
    # xdg-desktop-portal integration knows the app-id at init time. Setting it
    # afterwards makes Qt 6 emit a benign "Failed to register with host portal
    # ... Connection already associated with an application ID" warning.
    QGuiApplication.setApplicationName("fifine Control Deck")
    QGuiApplication.setApplicationDisplayName("fifine Control Deck")
    QGuiApplication.setDesktopFileName("fifine-control-deck")

    app = QApplication(sys.argv)

    # Claim single-instance ownership now, before the expensive init below.
    # The probe above cannot stand alone: two launches racing (autostart plus
    # a launcher click) both find no socket and both carry on. The claim is an
    # atomic flock — see _acquire_instance_lock for why the socket itself
    # can't play this role (the 0.8.1 audit found the socket-based recovery
    # still racing two launches into two live instances).
    lock_fd = _acquire_instance_lock()
    if lock_fd is None:
        # A live instance holds the lock but didn't answer the probe above —
        # it is mid-startup (lock claimed, socket not up yet). Give it a
        # moment and hand off.
        import time as _time
        for _ in range(20):
            _time.sleep(0.1)
            if _signal_existing("show"):
                return 0
        print("Another instance is starting but not responding.",
              file=sys.stderr)
        return 1

    # We hold the lock, so any existing socket file is stale by definition
    # (its owner is dead — the lock would otherwise still be held). Removing
    # it here can no longer unlink a live server.
    server = QLocalServer()
    if not server.listen(_ipc_socket_path()):
        QLocalServer.removeServer(_ipc_socket_path())
        if not server.listen(_ipc_socket_path()):
            print(f"Could not claim the single-instance socket: "
                  f"{server.errorString()}", file=sys.stderr)
            os.close(lock_fd)
            return 1

    ensure_dirs()
    config = DeckConfig.load()
    controller = DeckController(config)

    if assets.app_icon_path():
        app.setWindowIcon(QIcon(assets.app_icon_path()))
    app.setStyleSheet(STYLESHEET)
    app.setQuitOnLastWindowClosed(False)  # keep running when the window closes

    win = MainWindow(config, controller)

    def _on_conn():
        conn = server.nextPendingConnection()
        if conn is None:
            return
        if conn.waitForReadyRead(250):
            cmd = bytes(conn.readAll()).decode(errors="ignore").strip()
            if cmd == "quit":
                win._quit()
            elif cmd == "ping":
                pass                # liveness probe from a waiting --quit
            elif cmd in ("autostart-on", "autostart-off"):
                # CLI --enable/--disable-autostart delegated by main(): the
                # GUI applies it through its own toggle so the live config
                # and the menu state stay in sync (changing the file behind
                # a running GUI got clobbered by its next autosave).
                win.autostart_act.setChecked(cmd == "autostart-on")
            else:
                win.show_and_raise()
        conn.close()

    server.newConnection.connect(_on_conn)

    if not hidden:
        win.show()

    # start the device in the background (non-fatal if absent)
    controller.start()
    win._set_status()
    # Under a confined snap with no device found, tell the user how to grant
    # USB access (raw-usb is manual-connect and a snap can't self-connect it).
    if not hidden:
        win.maybe_show_snap_hint()

    # SIGTERM (logout, `systemctl --user stop`, kill) and SIGINT must run the
    # SAME orderly shutdown as Ctrl+Q / IPC quit: flush the pending debounced
    # config save and clear the deck. The old SIG_DFL died instantly, losing
    # up to 600 ms of edits and leaving stale icons on the device. A Python
    # handler only runs when the interpreter executes bytecode and Qt's C
    # event loop doesn't return to Python on its own — set_wakeup_fd writes a
    # byte the QSocketNotifier wakes on, which re-enters Python and lets the
    # handler (and the queued quit) run.
    import socket as _socket
    from PyQt6.QtCore import QSocketNotifier
    sig_r, sig_w = _socket.socketpair()
    sig_r.setblocking(False)
    sig_w.setblocking(False)
    signal.set_wakeup_fd(sig_w.fileno())
    notifier = QSocketNotifier(sig_r.fileno(), QSocketNotifier.Type.Read, app)

    def _drain():
        try:
            sig_r.recv(64)
        except BlockingIOError:
            pass

    notifier.activated.connect(_drain)
    for _sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(_sig, lambda *_: win._quit())

    rc = app.exec()
    signal.set_wakeup_fd(-1)
    server.close()
    QLocalServer.removeServer(_ipc_socket_path())
    controller.stop()
    os.close(lock_fd)      # release the single-instance claim last
    return rc


def run_headless() -> int:
    import time
    ensure_dirs()
    config = DeckConfig.load()
    controller = DeckController(config)
    ok = controller.start()
    log.info("headless started; device connected=%s. Ctrl+C to quit.", ok)

    def _term(*_):
        # SIGTERM (logout / service stop) must reach the same cleanup as
        # Ctrl+C — the deck is cleared and closed, not left showing stale
        # icons. The loop below is pure Python, so the handler runs promptly.
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _term)
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        controller.stop()
    return 0


def main() -> int:
    _configure_logging()
    ap = argparse.ArgumentParser(description="fifine Control Deck for Linux")
    ap.add_argument("--headless", action="store_true",
                    help="run the key daemon without the GUI")
    ap.add_argument("--hidden", action="store_true",
                    help="start with the window hidden (deck active in background)")
    ap.add_argument("--quit", action="store_true",
                    help="tell a running GUI instance to quit")
    ap.add_argument("--enable-autostart", action="store_true",
                    help="run (hidden) automatically on login")
    ap.add_argument("--disable-autostart", action="store_true",
                    help="stop running automatically on login")
    args = ap.parse_args()
    if args.enable_autostart or args.disable_autostart:
        enable = bool(args.enable_autostart)
        # A running GUI holds the config in memory: changing the state behind
        # its back desyncs its toggle, and its next debounced autosave writes
        # the stale value straight back (under Flatpak that desync is
        # permanent — the portal has no query API to resync from). Delegate
        # to the instance so its toggle applies + persists the change.
        if _signal_existing("autostart-on" if enable else "autostart-off"):
            print("Signalled the running instance to update autostart.")
            return 0
        return set_autostart(enable)
    if args.headless:
        return run_headless()
    return run_gui(quit_flag=args.quit, hidden=args.hidden)


if __name__ == "__main__":
    sys.exit(main())
