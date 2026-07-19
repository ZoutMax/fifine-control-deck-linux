"""Single-instance plumbing: socket location, atomic flock claim, CLI
delegation. All 0.8.1-audit regressions — no QApplication needed."""
import os

import pytest

app = pytest.importorskip("fifine_deck.app")


def test_socket_lives_in_xdg_runtime_dir(tmp_path, monkeypatch):
    """0.8.1 audit: a fixed, predictable name in world-writable /tmp lets any
    other local user pre-bind the socket and silently swallow every launch.
    XDG_RUNTIME_DIR is 0700 and user-owned — nobody else can squat there."""
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    assert app._runtime_dir() == str(tmp_path)
    assert app._ipc_socket_path() == os.path.join(str(tmp_path), app._IPC_NAME)
    assert os.path.isabs(app._ipc_socket_path())


def test_socket_falls_back_when_runtime_dir_is_unusable(tmp_path, monkeypatch):
    import tempfile
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    assert app._runtime_dir() == tempfile.gettempdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "missing"))
    assert app._runtime_dir() == tempfile.gettempdir()


def test_instance_lock_is_exclusive_and_releases(tmp_path, monkeypatch):
    """0.8.1 audit: the socket-based claim raced — two launches could both
    fail listen() on a stale socket and the loser's removeServer() unlinked
    the winner's LIVE socket, ending with two running instances. The flock
    claim is atomic: exactly one holder, and a crash releases it for free."""
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    fd1 = app._acquire_instance_lock()
    assert fd1 is not None
    assert app._acquire_instance_lock() is None      # second claim loses
    os.close(fd1)                                    # holder exits/crashes
    fd2 = app._acquire_instance_lock()
    assert fd2 is not None                           # claim recovers
    os.close(fd2)
    mode = os.stat(app._ipc_socket_path() + ".lock").st_mode & 0o777
    assert mode == 0o600


def test_autostart_cli_delegates_to_running_instance(monkeypatch):
    """0.8.1 audit: --enable/--disable-autostart while the GUI runs got
    clobbered by the GUI's next debounced autosave (its in-memory config
    still held the old value) — under Flatpak the desync was permanent.
    The CLI must hand the request to the running instance instead."""
    sent = []
    monkeypatch.setattr(app, "_signal_existing",
                        lambda cmd: (sent.append(cmd), True)[1])
    monkeypatch.setattr(app, "set_autostart",
                        lambda *a, **k: pytest.fail("must delegate, not write"))
    import sys
    monkeypatch.setattr(sys, "argv", ["fifine-control-deck", "--disable-autostart"])
    assert app.main() == 0
    assert sent == ["autostart-off"]

    # No instance running: falls back to acting locally.
    sent.clear()
    calls = []
    monkeypatch.setattr(app, "_signal_existing",
                        lambda cmd: (sent.append(cmd), False)[1])
    monkeypatch.setattr(app, "set_autostart",
                        lambda enable: (calls.append(enable), 0)[1])
    monkeypatch.setattr(sys, "argv", ["fifine-control-deck", "--enable-autostart"])
    assert app.main() == 0
    assert sent == ["autostart-on"] and calls == [True]
