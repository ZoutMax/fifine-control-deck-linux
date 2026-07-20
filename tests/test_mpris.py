"""MPRIS media control: player discovery, target choice, command mapping.

The D-Bus layer is faked, so these run headless and on machines with no
player installed. The live path is verified separately on hardware.
"""
import pytest

from fifine_deck import mpris


class _FakeBus:
    def __init__(self, players: dict):
        self.players = players          # name -> PlaybackStatus
        self.calls = []


@pytest.fixture
def fake(monkeypatch):
    """Patch the module's D-Bus helpers with in-memory fakes."""
    state = {"bus": None}

    def use(players: dict):
        bus = _FakeBus(players)
        state["bus"] = bus
        monkeypatch.setattr(mpris, "_bus", lambda: bus)
        monkeypatch.setattr(mpris, "_players", lambda b: list(b.players))
        monkeypatch.setattr(mpris, "_playback_status",
                            lambda b, n: b.players.get(n, ""))
        return bus

    return use


def test_no_player_means_no_control(fake, monkeypatch):
    fake({})
    assert mpris.available() is False
    assert mpris.control("play-pause") is False


def test_unknown_command_is_rejected(fake):
    fake({mpris.MPRIS_PREFIX + "vlc": "Playing"})
    assert mpris.control("teleport") is False


def test_playing_player_wins_over_paused(fake, monkeypatch):
    bus = fake({
        mpris.MPRIS_PREFIX + "paused_one": "Paused",
        mpris.MPRIS_PREFIX + "playing_one": "Playing",
    })
    assert mpris._target(bus, "next") == mpris.MPRIS_PREFIX + "playing_one"


def test_paused_player_wins_over_stopped(fake):
    """Play-pause with nothing playing should resume the paused player, not
    poke a stopped one: same precedence a user expects from playerctl."""
    bus = fake({
        mpris.MPRIS_PREFIX + "stopped_one": "Stopped",
        mpris.MPRIS_PREFIX + "paused_one": "Paused",
    })
    assert mpris._target(bus, "play-pause") == mpris.MPRIS_PREFIX + "paused_one"


def test_falls_back_to_any_player(fake):
    bus = fake({mpris.MPRIS_PREFIX + "stopped_one": "Stopped"})
    assert bus.players and mpris._target(bus, "next").endswith("stopped_one")


def test_commands_map_to_mpris_methods():
    assert mpris.COMMANDS == {
        "play-pause": "PlayPause",
        "next": "Next",
        "previous": "Previous",
        "stop": "Stop",
    }


def test_control_calls_the_right_method(fake, monkeypatch):
    bus = fake({mpris.MPRIS_PREFIX + "vlc": "Playing"})
    sent = []

    class _Reply:
        @staticmethod
        def errorName():
            return ""

    class _Iface:
        def __init__(self, name, path, iface, bus):
            sent.append((name, path, iface))

        def call(self, method, *args):
            sent.append(method)
            return _Reply()

    import PyQt6.QtDBus as qtdbus
    monkeypatch.setattr(qtdbus, "QDBusInterface", _Iface)
    assert mpris.control("previous") is True
    assert "Previous" in sent
    assert (mpris.MPRIS_PREFIX + "vlc", mpris.OBJECT_PATH,
            mpris.PLAYER_IFACE) in sent
