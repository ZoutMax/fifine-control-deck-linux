"""Application entry point: `python -m fifine_deck` (GUI) or `--headless`."""
from __future__ import annotations

import argparse
import signal
import sys

from .model import DeckConfig, ensure_dirs
from .controller import DeckController


import os

_IPC_NAME = f"fifine-control-deck-{os.getuid()}"


def _signal_existing(command: str) -> bool:
    """If an instance is already running, send it a command and return True."""
    from PyQt6.QtNetwork import QLocalSocket
    sock = QLocalSocket()
    sock.connectToServer(_IPC_NAME)
    if not sock.waitForConnected(250):
        return False
    sock.write(command.encode())
    sock.flush()
    sock.waitForBytesWritten(500)
    sock.disconnectFromServer()
    return True


AUTOSTART_FILE = os.path.expanduser("~/.config/autostart/fifine-control-deck.desktop")

_AUTOSTART_ENTRY = """[Desktop Entry]
Type=Application
Name=fifine Control Deck
Comment=Keep the deck active on login (window hidden)
Exec=fifine-control-deck --hidden
Icon=fifine-control-deck
Terminal=false
X-GNOME-Autostart-enabled=true
"""


def set_autostart(enable: bool) -> int:
    if enable:
        os.makedirs(os.path.dirname(AUTOSTART_FILE), exist_ok=True)
        with open(AUTOSTART_FILE, "w") as f:
            f.write(_AUTOSTART_ENTRY)
        print(f"Autostart enabled: {AUTOSTART_FILE}")
        print("The deck will run (hidden) on login; open the window by launching the app.")
    else:
        try:
            os.remove(AUTOSTART_FILE)
            print("Autostart disabled.")
        except FileNotFoundError:
            print("Autostart was not enabled.")
    return 0


def run_gui(quit_flag: bool = False, hidden: bool = False) -> int:
    from PyQt6.QtWidgets import QApplication
    from PyQt6.QtGui import QIcon
    from PyQt6.QtNetwork import QLocalServer
    from .gui.main_window import MainWindow
    from .gui.style import STYLESHEET
    from . import assets

    # Single instance: hand off to a running copy instead of starting a second
    # one (a second copy could not open the already-claimed device anyway).
    if _signal_existing("quit" if quit_flag else "show"):
        print("Signalled the running instance; exiting.")
        return 0
    if quit_flag:
        print("No running instance to quit.")
        return 0

    ensure_dirs()
    config = DeckConfig.load()
    controller = DeckController(config)

    app = QApplication(sys.argv)
    app.setApplicationName("fifine Control Deck")
    app.setApplicationDisplayName("fifine Control Deck")
    app.setDesktopFileName("fifine-control-deck")
    if assets.app_icon_path():
        app.setWindowIcon(QIcon(assets.app_icon_path()))
    app.setStyleSheet(STYLESHEET)
    app.setQuitOnLastWindowClosed(False)  # keep running when the window closes

    win = MainWindow(config, controller)

    # IPC server: a second launch tells us to show the window (or quit).
    QLocalServer.removeServer(_IPC_NAME)  # clear any stale socket
    server = QLocalServer()
    server.listen(_IPC_NAME)

    def _on_conn():
        conn = server.nextPendingConnection()
        if conn is None:
            return
        if conn.waitForReadyRead(250):
            cmd = bytes(conn.readAll()).decode(errors="ignore").strip()
            if cmd == "quit":
                win._quit()
            else:
                win.show_and_raise()
        conn.close()

    server.newConnection.connect(_on_conn)

    if not hidden:
        win.show()

    # start the device in the background (non-fatal if absent)
    controller.start()
    win._set_status()

    signal.signal(signal.SIGINT, signal.SIG_DFL)
    rc = app.exec()
    server.close()
    QLocalServer.removeServer(_IPC_NAME)
    controller.stop()
    return rc


def run_headless() -> int:
    import time
    ensure_dirs()
    config = DeckConfig.load()
    controller = DeckController(config)
    ok = controller.start()
    print(f"[headless] started; device connected = {ok}. Ctrl+C to quit.", flush=True)
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        controller.stop()
    return 0


def main() -> int:
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
    if args.enable_autostart:
        return set_autostart(True)
    if args.disable_autostart:
        return set_autostart(False)
    if args.headless:
        return run_headless()
    return run_gui(quit_flag=args.quit, hidden=args.hidden)


if __name__ == "__main__":
    sys.exit(main())
