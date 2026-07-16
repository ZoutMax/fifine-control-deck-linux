"""Helpers that build the actual command lines: volume, hotkeys, typing, media.

These are the last hop before a real process runs, so the exact argv matters —
a wrong flag here silently does the wrong thing to the user's audio or types
into the wrong window. Every test stubs _run; nothing here touches the machine.
"""
from __future__ import annotations

import logging
import subprocess

import pytest

from fifine_deck import actions


@pytest.fixture
def ran(monkeypatch):
    """Capture argv lists handed to _run instead of executing them."""
    calls = []
    monkeypatch.setattr(actions, "_run", lambda args, **kw: calls.append(list(args)))
    return calls


@pytest.fixture
def ran_full(monkeypatch):
    """Capture both argv and stdin payload handed to _run."""
    calls = []
    monkeypatch.setattr(actions, "_run",
                        lambda args, input_text=None, **kw:
                            calls.append((list(args), input_text)))
    return calls


# -- volume -----------------------------------------------------------------

def test_pipewire_volume(ran, monkeypatch):
    monkeypatch.setattr(actions, "AUDIO", "pipewire")
    actions._volume("up", "10")
    actions._volume("down", "10")
    actions._volume("mute", "")
    assert ran == [
        # -l 1.5 caps the boost so a held key can't blow out the ears
        ["wpctl", "set-volume", "-l", "1.5", actions.SINK, "10%+"],
        ["wpctl", "set-volume", actions.SINK, "10%-"],
        ["wpctl", "set-mute", actions.SINK, "toggle"],
    ]


def test_pulseaudio_volume(ran, monkeypatch):
    monkeypatch.setattr(actions, "AUDIO", "pulseaudio")
    actions._volume("up", "7")
    actions._volume("down", "7")
    actions._volume("mute", "")
    assert ran == [
        ["pactl", "set-sink-volume", "@DEFAULT_SINK@", "+7%"],
        ["pactl", "set-sink-volume", "@DEFAULT_SINK@", "-7%"],
        ["pactl", "set-sink-mute", "@DEFAULT_SINK@", "toggle"],
    ]


def test_volume_without_a_backend_does_nothing(ran, monkeypatch):
    monkeypatch.setattr(actions, "AUDIO", "")
    actions._volume("up", "5")
    assert ran == []


@pytest.mark.parametrize("step, expected", [
    ("10", "10%+"),
    ("10%", "10%+"),        # a user typing the % sign must not break it
    ("  10  ", "10%+"),
    ("", "5%+"),            # blank -> documented default
    (None, "5%+"),
    ("abc", "5%+"),         # garbage -> default, never a crash
])
def test_volume_step_parsing(ran, monkeypatch, step, expected):
    monkeypatch.setattr(actions, "AUDIO", "pipewire")
    actions._volume("up", step)
    assert ran[0][-1] == expected


def test_unknown_volume_command_is_ignored(ran, monkeypatch):
    monkeypatch.setattr(actions, "AUDIO", "pipewire")
    actions._volume("sideways", "5")
    assert ran == []


# -- hotkeys ----------------------------------------------------------------

def test_hotkey_needs_a_tool(ran, monkeypatch):
    monkeypatch.setattr(actions, "KEY_TOOL", "")
    actions._send_hotkey("ctrl+c")
    assert ran == []


def test_hotkey_blank_is_a_noop(ran, monkeypatch):
    monkeypatch.setattr(actions, "KEY_TOOL", "xdotool")
    actions._send_hotkey("   ")
    assert ran == []


def test_hotkey_xdotool(ran, monkeypatch):
    monkeypatch.setattr(actions, "KEY_TOOL", "xdotool")
    actions._send_hotkey(" ctrl+shift+m ")
    assert ran == [["xdotool", "key", "--clearmodifiers", "ctrl+shift+m"]]


def test_hotkey_ydotool_presses_down_then_releases_in_reverse(ran, monkeypatch):
    """Modifiers must be released after the key, or they leak into whatever the
    user types next."""
    monkeypatch.setattr(actions, "KEY_TOOL", "ydotool")
    actions._send_hotkey("ctrl+shift+m")
    assert ran == [["ydotool", "key", "29:1", "42:1", "50:1", "50:0", "42:0", "29:0"]]


def test_hotkey_ydotool_unknown_key_sends_nothing(ran, monkeypatch):
    """Better to do nothing than to send a wrong keycode into the focused app."""
    monkeypatch.setattr(actions, "KEY_TOOL", "ydotool")
    actions._send_hotkey("ctrl+nosuchkey")
    assert ran == []


def test_hotkey_wtype_wraps_modifiers_around_the_key(ran, monkeypatch):
    monkeypatch.setattr(actions, "KEY_TOOL", "wtype")
    actions._send_hotkey("ctrl+shift+m")
    assert ran == [["wtype", "-M", "ctrl", "-M", "shift", "-k", "m",
                    "-m", "ctrl", "-m", "shift"]]


def test_hotkey_wtype_maps_super_to_logo(ran, monkeypatch):
    monkeypatch.setattr(actions, "KEY_TOOL", "wtype")
    actions._send_hotkey("super+l")
    assert ran == [["wtype", "-M", "logo", "-k", "l", "-m", "logo"]]


# -- typing -----------------------------------------------------------------

@pytest.mark.parametrize("tool, expected_argv", [
    ("xdotool", ["xdotool", "type", "--clearmodifiers", "--file", "-"]),
    ("wtype", ["wtype", "-"]),
    ("ydotool", ["ydotool", "type", "--file", "-"]),
])
def test_type_text_feeds_the_text_on_stdin(ran_full, monkeypatch, tool, expected_argv):
    """Every backend must read the text from stdin. Text starting with '-' also
    can't be mistaken for a flag this way."""
    monkeypatch.setattr(actions, "KEY_TOOL", tool)
    actions._type_text("-hi there")
    assert ran_full == [(expected_argv, b"-hi there")]


@pytest.mark.parametrize("tool", ["xdotool", "wtype", "ydotool"])
def test_typed_text_never_reaches_argv(ran_full, monkeypatch, tool):
    """The security property, stated directly: the 'type password' action goes
    through here, and /proc/<pid>/cmdline is world-readable — so the text must
    appear nowhere in the argv of any backend."""
    monkeypatch.setattr(actions, "KEY_TOOL", tool)
    secret = "correct horse battery staple"
    actions._type_text(secret)
    argv, stdin = ran_full[0]
    assert all(secret not in a for a in argv)
    assert stdin == secret.encode()


def test_type_text_needs_a_tool(ran, monkeypatch):
    monkeypatch.setattr(actions, "KEY_TOOL", "")
    actions._type_text("hello")
    assert ran == []


# -- media ------------------------------------------------------------------

def test_media_uses_playerctl(ran, monkeypatch):
    monkeypatch.setattr(actions, "HAS_PLAYERCTL", True)
    actions._media("play-pause")
    assert ran == [["playerctl", "play-pause"]]


def test_media_without_playerctl_does_nothing(ran, monkeypatch):
    monkeypatch.setattr(actions, "HAS_PLAYERCTL", False)
    actions._media("play-pause")
    assert ran == []


# -- closing apps -----------------------------------------------------------

def test_close_app_prefers_wmctrl(ran, monkeypatch):
    """wmctrl closes a window politely; pkill is the blunt fallback."""
    monkeypatch.setattr(actions, "_has", lambda c: c == "wmctrl")
    actions._close_app(" Firefox ")
    assert ran == [["wmctrl", "-c", "Firefox"]]


def test_close_app_falls_back_to_pkill(ran, monkeypatch):
    monkeypatch.setattr(actions, "_has", lambda c: c == "pkill")
    actions._close_app("firefox")
    assert ran == [["pkill", "-f", "firefox"]]


def test_close_app_without_any_tool_does_nothing(ran, monkeypatch):
    monkeypatch.setattr(actions, "_has", lambda c: False)
    actions._close_app("firefox")
    assert ran == []


# -- process launching ------------------------------------------------------

@pytest.fixture
def popen(monkeypatch):
    seen = {}
    monkeypatch.setattr(actions.subprocess, "Popen",
                        lambda args, **kw: seen.update(args=args, **kw))
    return seen


def test_popen_detaches_so_children_outlive_the_app(popen, monkeypatch):
    """start_new_session keeps a launched app alive after the deck app exits."""
    monkeypatch.setattr(actions, "IN_FLATPAK", False)
    actions._popen_detached(["xdg-open", "https://example.com"])
    assert popen["args"] == ["xdg-open", "https://example.com"]
    assert popen["start_new_session"] is True


def test_popen_routes_shell_commands_to_the_host_in_flatpak(popen, monkeypatch):
    """Inside the sandbox the user's real apps live on the host, so a shell
    command must be handed to flatpak-spawn rather than run with shell=True."""
    monkeypatch.setattr(actions, "IN_FLATPAK", True)
    actions._popen_detached("gimp ~/a.png", shell=True, host=True)
    assert popen["args"] == ["flatpak-spawn", "--host", "sh", "-c", "gimp ~/a.png"]
    assert popen["shell"] is False


def test_popen_leaves_non_host_calls_alone_in_flatpak(popen, monkeypatch):
    monkeypatch.setattr(actions, "IN_FLATPAK", True)
    actions._popen_detached(["xdg-open", "u"])          # host=False
    assert popen["args"] == ["xdg-open", "u"]


# -- _run itself ------------------------------------------------------------

def test_run_swallows_helper_failures(monkeypatch):
    """A hung or missing helper must never escape into the reader thread."""
    def boom(*a, **k):
        raise FileNotFoundError("no such tool")
    monkeypatch.setattr(actions.subprocess, "run", boom)
    actions._run(["definitely-not-a-real-tool"])       # must not raise


def test_run_applies_a_timeout_by_default(monkeypatch):
    seen = {}
    monkeypatch.setattr(actions.subprocess, "run",
                        lambda args, **kw: seen.update(kw))
    actions._run(["true"])
    assert seen["timeout"] == 8


def test_run_forwards_the_stdin_payload(monkeypatch):
    seen = {}
    monkeypatch.setattr(actions.subprocess, "run",
                        lambda args, **kw: seen.update(args=args, **kw))
    actions._run(["cat"], input_text=b"payload")
    assert seen["input"] == b"payload"


def test_run_failure_log_cannot_leak_the_stdin_payload(monkeypatch, caplog):
    """A hung helper raises TimeoutExpired, whose message carries argv, and the
    guard logs it — straight into the systemd journal. That is exactly why the
    secret goes on stdin: there is nothing to redact."""
    def boom(args, **kw):
        raise subprocess.TimeoutExpired(args, 8)

    monkeypatch.setattr(actions.subprocess, "run", boom)
    with caplog.at_level(logging.WARNING):
        actions._run(["ydotool", "type", "--file", "-"], input_text=b"hunter2")
    assert caplog.text                      # it did log the failure…
    assert "hunter2" not in caplog.text     # …but the secret isn't in it
