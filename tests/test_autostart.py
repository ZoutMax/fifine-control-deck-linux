"""Start-on-login: XDG autostart entry outside a sandbox, the Background
portal under Flatpak (issue #3). The portal itself is mocked — these tests pin
the routing, the XDG path handling, and the persisted toggle state."""
import os

import pytest

from fifine_deck import app
from fifine_deck.model import DeckConfig


@pytest.fixture
def xdg(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    return tmp_path


def test_autostart_file_honors_xdg_config_home(xdg):
    assert app.autostart_file() == str(
        xdg / "autostart" / "fifine-control-deck.desktop")


def test_autostart_file_falls_back_to_home_config(monkeypatch):
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    assert app.autostart_file() == os.path.expanduser(
        "~/.config/autostart/fifine-control-deck.desktop")


def test_set_autostart_writes_and_removes_the_entry(xdg, monkeypatch):
    monkeypatch.setattr("fifine_deck.actions.IN_FLATPAK", False)
    assert app.set_autostart(True) == 0
    path = app.autostart_file()
    entry = open(path).read()
    assert "Exec=fifine-control-deck --hidden" in entry
    assert "Type=Application" in entry
    assert app.set_autostart(False) == 0
    assert not os.path.exists(path)
    # disabling twice stays a friendly no-op
    assert app.set_autostart(False) == 0


def test_flatpak_routes_to_the_portal_not_the_sandbox_home(xdg, monkeypatch):
    """Inside Flatpak the .desktop write would land in the sandbox and never
    run — set_autostart must call the Background portal instead."""
    monkeypatch.setattr("fifine_deck.actions.IN_FLATPAK", True)
    calls = []

    def fake_portal(enable):
        calls.append(enable)
        return True

    monkeypatch.setattr(app, "_portal_autostart", fake_portal)
    assert app.set_autostart(True) == 0
    assert calls == [True]
    assert not os.path.exists(app.autostart_file())


def test_portal_denial_is_reported_as_failure(xdg, monkeypatch):
    monkeypatch.setattr("fifine_deck.actions.IN_FLATPAK", True)
    monkeypatch.setattr(app, "_portal_autostart", lambda enable: False)
    assert app.set_autostart(True) != 0
    assert app.set_autostart(False) != 0


def test_config_round_trips_the_flatpak_toggle_state():
    cfg = DeckConfig(autostart_enabled=True)
    assert DeckConfig.from_dict(cfg.to_dict()).autostart_enabled is True
    assert DeckConfig.from_dict({}).autostart_enabled is False


def test_flatpak_cli_persists_the_granted_state(xdg, monkeypatch):
    """The portal has no query API — the config flag is the GUI toggle's only
    memory. A granted CLI --enable-autostart therefore MUST persist it, or the
    next GUI launch shows a stale toggle (audit finding, 0.7.0)."""
    from fifine_deck import model
    monkeypatch.setattr("fifine_deck.actions.IN_FLATPAK", True)
    monkeypatch.setattr(app, "_portal_autostart", lambda enable: True)
    cfg_path = os.path.join(str(xdg), "fifine-control-deck", "config.json")
    monkeypatch.setattr(model, "CONFIG_DIR", os.path.dirname(cfg_path))
    monkeypatch.setattr(model, "CONFIG_PATH", cfg_path)
    assert app.set_autostart(True) == 0                       # CLI: no config arg
    assert model.DeckConfig.load().autostart_enabled is True
    assert app.set_autostart(False) == 0
    assert model.DeckConfig.load().autostart_enabled is False


def test_flatpak_gui_config_is_updated_in_place_not_reloaded(xdg, monkeypatch):
    """With a live config passed, set_autostart must mutate THAT object and
    never touch the disk itself — a fresh load/save would clobber unsaved
    in-memory edits of the running GUI."""
    monkeypatch.setattr("fifine_deck.actions.IN_FLATPAK", True)
    monkeypatch.setattr(app, "_portal_autostart", lambda enable: True)
    from fifine_deck.model import DeckConfig
    live = DeckConfig()
    loads = []
    monkeypatch.setattr(DeckConfig, "load", classmethod(
        lambda cls, path=None: loads.append(1)))
    assert app.set_autostart(True, live) == 0
    assert live.autostart_enabled is True
    assert app.set_autostart(False, live) == 0                # the disable path
    assert live.autostart_enabled is False
    assert loads == []                                        # no disk round-trip
