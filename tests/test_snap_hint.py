"""snap_usb_hint(): None outside a snap, actionable guidance inside one."""
from fifine_deck import actions


def test_no_hint_outside_snap(monkeypatch):
    monkeypatch.setattr(actions, "IN_SNAP", False)
    assert actions.snap_usb_hint() is None


def test_hint_inside_snap(monkeypatch):
    monkeypatch.setattr(actions, "IN_SNAP", True)
    monkeypatch.setenv("SNAP_NAME", "fifine-control-deck")
    hint = actions.snap_usb_hint()
    assert hint is not None
    # names the snap and the interfaces the user must connect
    assert "fifine-control-deck" in hint
    assert "raw-usb" in hint
    assert "hardware-observe" in hint


def test_environment_summary_marks_snap(monkeypatch):
    monkeypatch.setattr(actions, "IN_SNAP", True)
    assert "[snap]" in actions.environment_summary()
    monkeypatch.setattr(actions, "IN_SNAP", False)
    assert "[snap]" not in actions.environment_summary()
