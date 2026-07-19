"""Config model: serialization round-trips, validation, corrupt-config recovery.

All persistence uses an explicit tmp path — the real user config is never touched.
"""
import json
import os

from fifine_deck.model import Action, DeckConfig, Folder, KeyConfig


def test_action_roundtrip():
    a = Action("hotkey", {"keys": "ctrl+c"})
    assert Action.from_dict(a.to_dict()) == a
    assert Action.from_dict(None).type == "none"
    assert Action.from_dict({}).type == "none"


def test_keyconfig_is_empty():
    assert KeyConfig().is_empty()
    assert not KeyConfig(label="x").is_empty()
    assert not KeyConfig(action=Action("volume", {"cmd": "up"})).is_empty()


def test_config_roundtrip(tmp_path):
    cfg = DeckConfig(brightness=42, glow=False)
    page = cfg.profiles[0].pages[0]
    page.key(1).label = "Vol"
    page.key(1).action = Action("volume", {"cmd": "up"})
    page.key(1).bg_color = "#123456"
    p = str(tmp_path / "c.json")
    cfg.save(p)

    loaded = DeckConfig.load(p)
    assert loaded.brightness == 42
    assert loaded.glow is False
    k = loaded.profiles[0].pages[0].keys[1]
    assert k.label == "Vol"
    assert k.bg_color == "#123456"
    assert k.action.type == "volume" and k.action.params["cmd"] == "up"


def test_folder_roundtrip(tmp_path):
    cfg = DeckConfig()
    cfg.profiles[0].pages[0].key(2).folder = Folder(name="Apps")
    p = str(tmp_path / "c.json")
    cfg.save(p)
    loaded = DeckConfig.load(p)
    fld = loaded.profiles[0].pages[0].keys[2].folder
    assert fld is not None and fld.name == "Apps" and fld.pages


def test_active_profile_defaults():
    cfg = DeckConfig()
    assert cfg.active_profile_id == cfg.profiles[0].id
    assert cfg.active_profile() is cfg.profiles[0]


def test_looks_like_config():
    assert DeckConfig.looks_like_config({"profiles": [{"pages": []}]})
    assert not DeckConfig.looks_like_config("nope")
    assert not DeckConfig.looks_like_config({})
    assert not DeckConfig.looks_like_config({"profiles": []})
    assert not DeckConfig.looks_like_config({"profiles": [{"no": "pages"}]})


def test_load_missing_creates_default(tmp_path):
    p = str(tmp_path / "new.json")
    cfg = DeckConfig.load(p)
    assert os.path.exists(p)
    assert cfg.profiles


def test_load_corrupt_json_backs_up(tmp_path):
    p = tmp_path / "c.json"
    p.write_text("{ this is not valid json")
    cfg = DeckConfig.load(str(p))
    assert cfg.profiles                      # a fresh default was returned
    # preserved as .corrupt — NOT .bak, which belongs to the import flow's
    # backup of a known-good config and must never be overwritten by a corpse
    assert (tmp_path / "c.json.corrupt").exists()
    assert not (tmp_path / "c.json.bak").exists()


def test_load_recovery_never_clobbers_good_backup(tmp_path):
    p = tmp_path / "c.json"
    good = DeckConfig()
    good.profiles[0].name = "precious"
    good.save(str(p) + ".bak")               # the import flow's backup
    p.write_text("{ this is not valid json")
    DeckConfig.load(str(p))
    restored = DeckConfig.load(str(p) + ".bak")
    assert restored.profiles[0].name == "precious"


def test_load_bad_scalar_types_coerced_not_reset(tmp_path):
    # Valid JSON with wrong-typed scalars: the config must survive with the
    # bad fields defaulted — NOT be reset (that would lose every profile) and
    # NOT load raw (a null icon would crash the GUI at startup forever).
    p = tmp_path / "c.json"
    p.write_text(json.dumps({
        "profiles": [{"name": "keepme", "pages": [{"keys": {
            "1": {"icon": None, "bg_color": 101020, "label": 7,
                  "action": {"type": None, "params": "nope"}},
            "bogus": {"label": "dropped"},
        }}]}],
        "brightness": "not-an-int",
        "active_profile_id": None,
    }))
    cfg = DeckConfig.load(str(p))
    assert not (tmp_path / "c.json.corrupt").exists()
    assert cfg.profiles[0].name == "keepme"   # nothing was lost
    assert cfg.brightness == 80
    kc = cfg.profiles[0].pages[0].keys[1]
    assert kc.icon == "" and kc.bg_color == "#101020" and kc.label == ""
    assert kc.action.type == "none" and kc.action.params == {}
    assert "bogus" not in {str(k) for k in cfg.profiles[0].pages[0].keys}


def test_save_fsyncs_before_replace(tmp_path, monkeypatch):
    # Durability: the data must hit disk before the rename commits it, or a
    # power cut can leave a zero-length config (all profiles lost).
    order = []
    real_fsync, real_replace = os.fsync, os.replace
    monkeypatch.setattr(os, "fsync", lambda fd: (order.append("fsync"), real_fsync(fd))[1])
    monkeypatch.setattr(os, "replace", lambda a, b: (order.append("replace"), real_replace(a, b))[1])
    DeckConfig().save(str(tmp_path / "c.json"))
    assert "fsync" in order and order.index("fsync") < order.index("replace")


def test_save_is_private(tmp_path):
    p = str(tmp_path / "c.json")
    DeckConfig().save(p)
    mode = os.stat(p).st_mode & 0o777
    assert mode == 0o600


def test_save_is_private_from_the_first_byte(tmp_path, monkeypatch):
    """The config can hold a plaintext password (the fallback when no keyring
    is available). Creating it 0644 and chmod'ing to 0600 afterwards leaves a
    window in which any local user can read the secret, so the temp file must
    be created private — not fixed up later."""
    modes = []
    real_open = os.open

    def spy(path, flags, mode=0o777, *a, **kw):
        if str(path).endswith(".tmp"):
            modes.append(mode)
        return real_open(path, flags, mode, *a, **kw)

    monkeypatch.setattr(os, "open", spy)
    DeckConfig().save(str(tmp_path / "c.json"))
    assert modes, "save() no longer creates its temp file via os.open"
    assert all(m == 0o600 for m in modes), f"temp file created {modes!r}, not 0600"


def test_save_fixes_the_mode_of_a_stale_temp_file(tmp_path):
    """O_CREAT's mode applies only when the file is CREATED. An older version
    that crashed mid-save leaves config.json.tmp at umask-default 0644; without
    an explicit fchmod, the reused tmp keeps 0644 and os.replace carries that
    onto config.json — with a plaintext password inside on the no-keyring
    fallback."""
    p = tmp_path / "c.json"
    stale = tmp_path / "c.json.tmp"
    stale.write_text("{}")
    os.chmod(stale, 0o644)

    DeckConfig().save(str(p))
    assert os.stat(p).st_mode & 0o777 == 0o600


def test_default_config_path_is_resolved_at_call_time(tmp_path, monkeypatch):
    """conftest keeps tests off the real ~/.config by redirecting
    model.CONFIG_PATH. That only works if save()/load() read the module global
    when called: `def save(self, path=CONFIG_PATH)` binds at import, so the
    redirect would be ignored and a default-path save would hit the user's own
    configuration. This test is the tripwire for that regression."""
    from fifine_deck import model

    target = tmp_path / "redirected" / "config.json"
    monkeypatch.setattr(model, "CONFIG_DIR", str(target.parent))
    monkeypatch.setattr(model, "CONFIG_PATH", str(target))
    monkeypatch.setattr(model, "ICONS_DIR", str(target.parent / "icons"))

    cfg = DeckConfig()
    cfg.active_profile().name = "Redirected"
    cfg.save()                                   # no path -> must use the global

    assert target.exists(), "save() ignored the redirected CONFIG_PATH"
    assert DeckConfig.load().active_profile().name == "Redirected"


def test_config_dir_honors_xdg_config_home(monkeypatch):
    """Under Flatpak XDG_CONFIG_HOME points into ~/.var/app/<id>/; hardcoding
    ~/.config there writes into the sandbox's throwaway home and the config
    vanishes on restart (found during Flathub packaging)."""
    import importlib
    from fifine_deck import model as m
    monkeypatch.setenv("XDG_CONFIG_HOME", "/xdg/base")
    importlib.reload(m)
    try:
        assert m.CONFIG_DIR == "/xdg/base/fifine-control-deck"
        monkeypatch.delenv("XDG_CONFIG_HOME")
        importlib.reload(m)
        assert m.CONFIG_DIR == os.path.expanduser("~/.config/fifine-control-deck")
    finally:
        monkeypatch.undo()
        importlib.reload(m)


# -- 0.8.0: hold_action ------------------------------------------------------

def test_key_hold_action_round_trips():
    from fifine_deck.model import Action, KeyConfig
    kc = KeyConfig(action=Action("launch_app", {"command": "x"}),
                   hold_action=Action("hotkey", {"keys": "ctrl+l"}))
    d = kc.to_dict()
    back = KeyConfig.from_dict(d)
    assert back.hold_action.type == "hotkey"
    assert back.hold_action.params == {"keys": "ctrl+l"}


def test_key_without_hold_action_omits_it_and_loads_old_configs():
    from fifine_deck.model import Action, KeyConfig
    kc = KeyConfig(action=Action("launch_app", {"command": "x"}))
    assert "hold_action" not in kc.to_dict()      # old readers stay happy
    old = KeyConfig.from_dict({"label": "L", "action": {"type": "none"}})
    assert old.hold_action.type == "none"         # pre-0.8.0 config loads


def test_key_with_only_hold_action_is_not_empty():
    from fifine_deck.model import Action, KeyConfig
    kc = KeyConfig(hold_action=Action("hotkey", {"keys": "a"}))
    assert not kc.is_empty()
