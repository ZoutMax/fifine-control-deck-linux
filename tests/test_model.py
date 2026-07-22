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
    """CONFIG_DIR must follow XDG_CONFIG_HOME rather than hardcoding
    ~/.config, so a custom config location is respected."""
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


def test_load_reraises_io_error_instead_of_wiping_config(tmp_path, monkeypatch):
    """Audit fix: a transient read failure (EIO on a flaky mount, EMFILE under
    fd exhaustion) must NOT be misclassified as corruption. The good config
    must be left in place and the error propagated, never moved to .corrupt
    and replaced with a default."""
    import builtins
    import pytest
    from fifine_deck.model import DeckConfig
    p = tmp_path / "config.json"
    p.write_text('{"version": 1, "brightness": 80, "profiles": []}')
    real_open = builtins.open

    def boom(path, *a, **k):
        if str(path) == str(p):
            raise OSError(5, "EIO")
        return real_open(path, *a, **k)

    monkeypatch.setattr(builtins, "open", boom)
    with pytest.raises(OSError):
        DeckConfig.load(str(p))
    assert p.exists()                                   # good file untouched
    assert not (tmp_path / "config.json.corrupt").exists()


def test_parseable_but_wrong_shaped_config_is_preserved_not_silently_reset(tmp_path):
    """0.10.0 audit (critical, data loss): from_dict is total — _as_dict/_as_str
    coerce anything parseable — so it could never raise for a wrong SHAPE, and
    only a JSON syntax error ever reached the .corrupt recovery. A mistyped
    top-level key loaded as an empty default profile with the real config left
    in place and unbacked, and the first autosave 600 ms later overwrote the
    user's only copy of their layout."""
    import json
    from fifine_deck.model import DeckConfig
    p = tmp_path / "config.json"
    p.write_text(json.dumps({                      # capital P: a hand-edit typo
        "Profiles": [{"name": "mine", "pages": [{"keys": {}}]}],
        "brightness": 42,
    }))

    cfg = DeckConfig.load(str(p))
    cfg.save(str(p))                               # what the first autosave does

    corpse = tmp_path / "config.json.corrupt"
    assert corpse.exists(), "wrong-shaped config was destroyed, not preserved"
    assert "mine" in corpse.read_text()            # the layout is recoverable
    assert not (tmp_path / "config.json.bak").exists()   # import's backup is sacred


def test_a_top_level_json_list_is_preserved_too(tmp_path):
    """Same path via a different shape: valid JSON, not a dict at all."""
    from fifine_deck.model import DeckConfig
    p = tmp_path / "config.json"
    p.write_text("[1, 2, 3]")
    DeckConfig.load(str(p))
    assert (tmp_path / "config.json.corrupt").read_text() == "[1, 2, 3]"


def test_a_valid_config_is_never_moved_aside(tmp_path):
    """The shape gate must not fire on anything the app itself writes,
    including a profile that legitimately has no pages yet."""
    import json
    from fifine_deck.model import DeckConfig
    p = tmp_path / "config.json"
    DeckConfig().save(str(p))
    assert DeckConfig.load(str(p)).profiles[0].name == "Default"
    assert not (tmp_path / "config.json.corrupt").exists()

    p.write_text(json.dumps({"profiles": [{"name": "empty", "pages": []}]}))
    assert DeckConfig.load(str(p)).profiles[0].name == "empty"
    assert not (tmp_path / "config.json.corrupt").exists()


def test_a_newer_config_is_backed_up_before_it_is_downgraded(tmp_path, caplog):
    """0.10.0 audit: from_dict keeps only the keys this build knows and save()
    wrote the stripped result back under the NEWER version number — so every
    setting a newer build added was destroyed, and that build could not even
    tell it had been downgraded. The usual way in is syncing config.json
    between two machines on different versions."""
    import json
    import logging
    from fifine_deck.model import CONFIG_VERSION, DeckConfig
    p = tmp_path / "config.json"
    newer = CONFIG_VERSION + 1
    p.write_text(json.dumps({
        "version": newer,
        "profiles": [{"name": "mine", "pages": [{"keys": {}}]}],
        "a_setting_from_the_future": {"deep": [1, 2, 3]},
    }))

    with caplog.at_level(logging.WARNING, logger="fifine_deck.model"):
        cfg = DeckConfig.load(str(p))
    cfg.save(str(p))                                # strips the unknown setting

    keep = tmp_path / f"config.json.v{newer}"
    assert keep.exists(), "newer config was downgraded with no copy kept"
    assert json.loads(keep.read_text())["a_setting_from_the_future"] == {"deep": [1, 2, 3]}
    assert any("newer version" in r.message for r in caplog.records)
    assert "a_setting_from_the_future" not in p.read_text()   # the real downgrade


def test_a_same_version_config_is_not_backed_up(tmp_path):
    """The backup must fire only on a real downgrade, not on every load."""
    from fifine_deck.model import CONFIG_VERSION, DeckConfig
    p = tmp_path / "config.json"
    DeckConfig().save(str(p))
    DeckConfig.load(str(p))
    assert not (tmp_path / f"config.json.v{CONFIG_VERSION}").exists()
    # no sibling copy of any kind (save() also makes a cfg/ dir; ignore that)
    assert [f.name for f in tmp_path.glob("config.json.*")] == []


def test_an_empty_profile_list_loads_and_keeps_the_other_settings(tmp_path):
    """The 0.10.0 shape gate reused looks_like_config, which requires a NON-EMPTY
    profile list because it backs the import dialog. That made an empty list
    look corrupt on load, even though from_dict's `or [Profile()]` fallback has
    always handled it — so the config was moved aside and the user's brightness,
    glow and dismissed-hint settings were reset to defaults."""
    import json
    from fifine_deck.model import DeckConfig
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"version": 1, "brightness": 42, "glow": False,
                             "snap_hint_dismissed": True, "profiles": []}))

    cfg = DeckConfig.load(str(p))

    assert not (tmp_path / "config.json.corrupt").exists(), "loadable config declared corrupt"
    assert cfg.brightness == 42                  # the settings that were being lost
    assert cfg.glow is False
    assert cfg.snap_hint_dismissed is True
    assert [x.name for x in cfg.profiles] == ["Default"]   # fallback still applies


def test_the_shape_gate_still_rejects_what_it_was_added_for(tmp_path):
    """Loosening the gate must not reopen the hole it closed: a mistyped
    top-level key, a foreign JSON document, and a top-level list must all still
    be preserved rather than silently replaced."""
    import json
    from fifine_deck.model import DeckConfig
    cases = {
        "typo": {"Profiles": [{"name": "mine", "pages": [{"keys": {}}]}], "brightness": 42},
        "foreign": {"window": {"w": 800}, "theme": "dark"},
        "listy": [1, 2, 3],
        "no_profiles_key": {"version": 1, "brightness": 42},
    }
    for name, data in cases.items():
        d = tmp_path / name
        d.mkdir()
        p = d / "config.json"
        p.write_text(json.dumps(data))
        DeckConfig.load(str(p))
        assert (d / "config.json.corrupt").exists(), f"{name}: was not preserved"


def test_is_loadable_shape_is_weaker_than_looks_like_config():
    """The two checks are deliberately different; keep them from being merged.
    Import must reject an empty profile list (the user picked a useless file);
    load must accept it."""
    from fifine_deck.model import DeckConfig
    empty = {"profiles": []}
    assert DeckConfig.is_loadable_shape(empty) is True
    assert DeckConfig.looks_like_config(empty) is False


def test_corrupt_config_recovery_says_so(tmp_path, caplog):
    """0.11.1 audit: the recovery renames the user's config and hands them a
    blank one, silently. From the user's side every profile, page and key just
    vanished — indistinguishable from the data-loss bug the recovery exists to
    prevent, with no hint that their settings are sitting right next to it."""
    import logging
    from fifine_deck.model import DeckConfig
    p = tmp_path / "config.json"
    p.write_text("{ not valid json")

    with caplog.at_level(logging.WARNING, logger="fifine_deck.model"):
        DeckConfig.load(str(p))

    assert (tmp_path / "config.json.corrupt").exists()
    msgs = " ".join(r.getMessage() for r in caplog.records)
    assert ".corrupt" in msgs, "the user is never told where their config went"


def test_a_good_config_load_is_quiet(tmp_path, caplog):
    """No crying wolf: a normal load must log nothing at warning level."""
    import logging
    from fifine_deck.model import DeckConfig
    p = tmp_path / "config.json"
    DeckConfig().save(str(p))
    with caplog.at_level(logging.WARNING, logger="fifine_deck.model"):
        DeckConfig.load(str(p))
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []
