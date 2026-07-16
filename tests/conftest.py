"""Shared test fixtures.

CRITICAL: no test may ever write the real ~/.config/fifine-control-deck config.
This autouse fixture redirects the model's config directories to a per-test tmp
location, so even a stray ensure_dirs()/save() lands in the sandbox.
"""
import os

import pytest

# Must be set before Qt is imported: the GUI tests build real widgets, and
# without this they would need a display and hang or fail in CI.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="session")
def qapp():
    """One QApplication for the whole session — Qt allows only one."""
    QApplication = pytest.importorskip("PyQt6.QtWidgets").QApplication
    yield QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _isolate_config(tmp_path, monkeypatch):
    from fifine_deck import model
    cfgdir = tmp_path / "cfg"
    monkeypatch.setattr(model, "CONFIG_DIR", str(cfgdir))
    monkeypatch.setattr(model, "CONFIG_PATH", str(cfgdir / "config.json"))
    monkeypatch.setattr(model, "ICONS_DIR", str(cfgdir / "icons"))
    # This only works because save()/load() resolve CONFIG_PATH at call time.
    # If either goes back to `path: str = CONFIG_PATH`, the default binds at
    # import and every default-path save escapes this sandbox — onto the real
    # ~/.config/fifine-control-deck. test_model.py pins that; don't remove it.
    yield
