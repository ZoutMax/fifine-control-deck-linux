"""RemoteDesktop portal keystroke injection: keysym mapping, threading rules,
and the refusal to act without an established session.

The session itself needs a live compositor and an unlocked screen, so it is
verified on hardware rather than here.
"""
import pytest

from fifine_deck import portal_input


@pytest.fixture(autouse=True)
def _no_session(monkeypatch):
    """Every test starts with no portal session established."""
    monkeypatch.setattr(portal_input, "_SESSION", None)
    monkeypatch.setattr(portal_input, "_TRIED", False)


def test_keysym_latin1_maps_directly():
    assert portal_input._keysym_for("a") == 0x61
    assert portal_input._keysym_for("Z") == 0x5A
    assert portal_input._keysym_for("é") == 0xE9      # latin-1, direct


def test_keysym_beyond_latin1_uses_the_unicode_plane():
    """X11 reserves 0x01000000 + codepoint for anything above latin-1."""
    assert portal_input._keysym_for("€") == 0x01000000 | 0x20AC
    assert portal_input._keysym_for("\U0001F600") == 0x01000000 | 0x1F600


def test_nothing_is_sent_without_a_session():
    assert portal_input.available() is False
    assert portal_input.send_combo([29, 46]) is False
    assert portal_input.type_text("hello") is False


def test_empty_input_is_a_noop(monkeypatch):
    monkeypatch.setattr(portal_input, "_SESSION", "/session/1")
    monkeypatch.setattr(portal_input, "_notify",
                        lambda *a: pytest.fail("nothing should be sent"))
    assert portal_input.send_combo([]) is False
    assert portal_input.type_text("") is False


def test_combo_presses_in_order_then_releases_in_reverse(monkeypatch):
    """ctrl+shift+m must not release ctrl before m, or the app receiving it
    sees a bare 'm'."""
    monkeypatch.setattr(portal_input, "_SESSION", "/session/1")
    seq = []
    monkeypatch.setattr(portal_input, "_notify",
                        lambda m, v, s: (seq.append((v, s)), True)[1])
    assert portal_input.send_combo([29, 42, 50]) is True
    assert seq == [(29, 1), (42, 1), (50, 1),      # press ctrl, shift, m
                   (50, 0), (42, 0), (29, 0)]      # release m, shift, ctrl


def test_type_text_sends_press_and_release_per_character(monkeypatch):
    monkeypatch.setattr(portal_input, "_SESSION", "/session/1")
    seq = []
    monkeypatch.setattr(portal_input, "_notify",
                        lambda m, v, s: (seq.append((m, v, s)), True)[1])
    assert portal_input.type_text("hi") is True
    assert seq == [("NotifyKeyboardKeysym", 0x68, 1),
                   ("NotifyKeyboardKeysym", 0x68, 0),
                   ("NotifyKeyboardKeysym", 0x69, 1),
                   ("NotifyKeyboardKeysym", 0x69, 0)]


def test_newline_types_return(monkeypatch):
    monkeypatch.setattr(portal_input, "_SESSION", "/session/1")
    seq = []
    monkeypatch.setattr(portal_input, "_notify",
                        lambda m, v, s: (seq.append(v), True)[1])
    portal_input.type_text("\n")
    assert seq == [0xFF0D, 0xFF0D]                 # XK_Return, press+release


def test_session_is_never_created_off_the_main_thread(monkeypatch):
    """Actions dispatch on a worker thread and session creation runs a nested
    Qt event loop; doing that there would block the serial action queue."""
    import threading

    monkeypatch.setattr(portal_input, "_create_session",
                        lambda: pytest.fail("session created off main thread"))
    result = {}
    t = threading.Thread(target=lambda: result.update(ok=portal_input.prime()))
    t.start()
    t.join(5)
    assert result["ok"] is False
    assert portal_input._TRIED is False            # still primeable later


def test_prime_caches_the_session(monkeypatch):
    calls = []
    monkeypatch.setattr(portal_input, "_create_session",
                        lambda: (calls.append(1), "/session/9")[1])
    assert portal_input.prime() is True
    assert portal_input.prime() is True
    assert len(calls) == 1
    assert portal_input.available() is True
