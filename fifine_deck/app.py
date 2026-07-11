"""Application entry point: `python -m fifine_deck` (GUI) or `--headless`."""
from __future__ import annotations

import argparse
import signal
import sys

from .model import DeckConfig, ensure_dirs
from .controller import DeckController


def run_gui() -> int:
    from PyQt6.QtWidgets import QApplication
    from PyQt6.QtGui import QIcon
    from .gui.main_window import MainWindow
    from .gui.style import STYLESHEET
    from . import assets

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
    app.setQuitOnLastWindowClosed(False)  # live in the tray

    win = MainWindow(config, controller)
    win.show()

    # start the device in the background (non-fatal if absent)
    controller.start()
    win._set_status()

    signal.signal(signal.SIGINT, signal.SIG_DFL)
    rc = app.exec()
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
    args = ap.parse_args()
    return run_headless() if args.headless else run_gui()


if __name__ == "__main__":
    sys.exit(main())
