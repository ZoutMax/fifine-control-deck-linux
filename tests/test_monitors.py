"""System-monitor keys: sampler, renderer, and controller tick behaviour.

The invariants pinned here come straight from issue #2's acceptance criteria:
- monitor keys repaint only when their displayed value changes
- a page without monitor keys never samples a metric at all
- VRAM degrades gracefully when no dedicated GPU exists
- a broken target (bad mount, unknown interface) yields "n/a", never a crash
"""
from collections import deque, namedtuple

import pytest

from PIL import Image

from fifine_deck import monitors
from fifine_deck.actions import ACTION_CATALOG, ACTION_TYPES, execute
from fifine_deck.model import Action
from fifine_deck.monitors import MonitorSpec, Reading, Sampler, render_monitor


# ---------------------------------------------------------------------------
# MonitorSpec parsing
# ---------------------------------------------------------------------------
def test_spec_defaults_and_validation():
    s = MonitorSpec.from_params({})
    assert (s.metric, s.style, s.interval, s.target) == ("cpu", "number", 1.0, "")
    s = MonitorSpec.from_params(
        {"metric": " DISK ", "style": "GAUGE", "interval": "2.5", "target": " / "})
    assert (s.metric, s.style, s.interval, s.target) == ("disk", "gauge", 2.5, "/")


def test_spec_rejects_garbage_without_raising():
    s = MonitorSpec.from_params(
        {"metric": "nonsense", "style": "3d", "interval": "banana"})
    assert (s.metric, s.style, s.interval) == ("cpu", "number", 1.0)
    assert MonitorSpec.from_params(None).metric == "cpu"


def test_spec_interval_is_clamped():
    assert MonitorSpec.from_params({"interval": "0.01"}).interval == 0.5
    assert MonitorSpec.from_params({"interval": "9999"}).interval == 60.0


# ---------------------------------------------------------------------------
# Sampler
# ---------------------------------------------------------------------------
def test_cpu_ram_disk_produce_sane_percentages():
    pytest.importorskip("psutil")
    s = Sampler()
    s.sample(MonitorSpec.from_params({"metric": "cpu"}))   # cpu warm-up sample
    for metric in ("cpu", "ram", "disk"):
        r = s.sample(MonitorSpec.from_params({"metric": metric}))
        assert r.ok, metric
        assert r.pct is not None and 0.0 <= r.pct <= 100.0, metric
        assert r.text.endswith("%"), metric
        assert r.sample == r.pct, metric


def test_first_cpu_sample_is_a_warmup_not_a_fake_zero():
    # psutil's first non-blocking cpu_percent() is a documented-meaningless
    # 0.0 — it must surface as a warm-up frame and a history GAP, never as 0%.
    pytest.importorskip("psutil")
    s = Sampler()
    spec = MonitorSpec.from_params({"metric": "cpu"})
    first = s.sample(spec)
    assert first.pct is None and first.text == "…"
    assert s.history(spec) == [None]
    second = s.sample(spec)
    assert second.pct is not None and second.text.endswith("%")


def test_spec_target_only_applies_to_disk_and_net():
    # A stray target on cpu/ram/vram would split the shared sample stream —
    # and cpu/net use global since-last-call state, so split streams corrupt
    # each other's deltas.
    assert MonitorSpec.from_params({"metric": "cpu", "target": "x"}).target == ""
    assert MonitorSpec.from_params({"metric": "ram", "target": "x"}).target == ""
    assert MonitorSpec.from_params({"metric": "vram", "target": "x"}).target == ""
    assert MonitorSpec.from_params({"metric": "disk", "target": "/x"}).target == "/x"
    assert MonitorSpec.from_params({"metric": "net", "target": "eth0"}).target == "eth0"


def test_failed_samples_leave_history_gaps_not_zeros():
    pytest.importorskip("psutil")
    s = Sampler()
    spec = MonitorSpec.from_params({"metric": "disk",
                                    "target": "/definitely/not/a/mount"})
    s.sample(spec)
    s.sample(spec)
    assert s.history(spec) == [None, None]   # gaps, not false dips to 0%


def test_bad_disk_target_degrades_to_na():
    pytest.importorskip("psutil")
    r = Sampler().sample(MonitorSpec.from_params(
        {"metric": "disk", "target": "/definitely/not/a/mount"}))
    assert not r.ok and r.text == "n/a"


def test_net_rate_from_counter_deltas(monkeypatch):
    psutil = pytest.importorskip("psutil")
    IO = namedtuple("IO", "bytes_recv bytes_sent")
    samples = [IO(1000, 500), IO(1_001_000, 500)]
    monkeypatch.setattr(psutil, "net_io_counters",
                        lambda pernic=False: samples.pop(0) if samples else IO(0, 0))
    # Inexhaustible fake clock: +1 s per call (leaked controller threads from
    # other tests may also call it, so it must never raise).
    t = {"v": 100.0}
    def _mono():
        t["v"] += 1.0
        return t["v"]
    monkeypatch.setattr(monitors.time, "monotonic", _mono)
    s = Sampler()
    spec = MonitorSpec.from_params({"metric": "net"})
    first = s.sample(spec)              # no previous sample -> no rate yet
    assert first.pct is None
    second = s.sample(spec)             # 1 MB in 1 s
    assert second.text == "↓ 1.0 MB/s"
    assert second.sub == "↑ 0 B/s"
    assert second.sample == pytest.approx(1_000_000.0)
    assert s.history(spec)[0] is None   # the warm-up left a gap, not a 0


def test_net_unknown_interface_degrades_to_na(monkeypatch):
    psutil = pytest.importorskip("psutil")
    monkeypatch.setattr(psutil, "net_io_counters", lambda pernic=False: {})
    r = Sampler().sample(MonitorSpec.from_params(
        {"metric": "net", "target": "nosuch0"}))
    assert not r.ok and r.text == "n/a"


def test_vram_none_backend_degrades(monkeypatch):
    s = Sampler()
    monkeypatch.setattr(monitors, "_probe_vram", lambda: ("none",))
    r = s.sample(MonitorSpec.from_params({"metric": "vram"}))
    assert not r.ok and r.text == "n/a" and "GPU" in r.sub


def test_vram_amdgpu_sysfs_backend(tmp_path, monkeypatch):
    used = tmp_path / "mem_info_vram_used"
    total = tmp_path / "mem_info_vram_total"
    used.write_text("2000000000\n")
    total.write_text("8000000000\n")
    monkeypatch.setattr(monitors, "_probe_vram",
                        lambda: ("amdgpu", str(used), str(total)))
    r = Sampler().sample(MonitorSpec.from_params({"metric": "vram"}))
    assert r.ok and r.pct == pytest.approx(25.0)
    assert r.text == "25%"


def test_history_is_bounded_and_last_is_cached():
    s = Sampler()
    spec = MonitorSpec.from_params({"metric": "cpu"})
    monitors_psutil = monitors.psutil
    if monitors_psutil is None:
        pytest.skip("psutil not available")
    for _ in range(monitors.HISTORY_LEN + 10):
        s.sample(spec)
    assert len(s.history(spec)) == monitors.HISTORY_LEN
    assert s.last(spec).text.endswith("%")


def test_last_without_any_sample_is_the_placeholder():
    s = Sampler()
    spec = MonitorSpec.from_params({"metric": "ram"})
    r = s.last(spec)
    assert r.text == "—" and r.sub == "RAM" and r.pct is None


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("style", monitors.STYLES)
def test_render_all_styles_yield_key_sized_rgb(style):
    spec = MonitorSpec.from_params({"metric": "cpu", "style": style})
    img = render_monitor(100, spec, Reading(42.0, "42%", "8 cores"),
                         [10.0, 40.0, 42.0])
    assert isinstance(img, Image.Image)
    assert img.size == (100, 100) and img.mode == "RGB"


def test_render_gauge_without_percentage_falls_back_to_number():
    spec = MonitorSpec.from_params({"metric": "net", "style": "gauge"})
    img = render_monitor(100, spec, Reading(None, "↓ 1.0 MB/s", "↑ 0 B/s"), [])
    assert img.size == (100, 100)     # must not raise on pct=None


def test_render_graph_handles_empty_gappy_and_rate_history():
    spec = MonitorSpec.from_params({"metric": "net", "style": "graph"})
    r = Reading(None, "↓ 5 kB/s", sample=5000.0)
    assert render_monitor(100, spec, r, []).size == (100, 100)
    assert render_monitor(100, spec, r, [0.0, 5000.0, 2500.0]).size == (100, 100)
    # gaps (None) from failed samples must be skipped, not plotted
    assert render_monitor(100, spec, r, [None, 5000.0, None, 2500.0]).size == (100, 100)
    cpu = MonitorSpec.from_params({"metric": "cpu", "style": "graph"})
    assert render_monitor(100, cpu, Reading(None, "…"),
                          [50.0, None, 60.0]).size == (100, 100)


def test_render_actually_draws_something():
    spec = MonitorSpec.from_params({"metric": "cpu", "style": "gauge"})
    img = render_monitor(100, spec, Reading(80.0, "80%"), [], bg_color="#000000")
    assert len(img.getcolors(maxcolors=100000)) > 1   # not a flat fill


# ---------------------------------------------------------------------------
# Action-type integration
# ---------------------------------------------------------------------------
def test_monitor_is_a_registered_action_type():
    assert "monitor" in ACTION_TYPES
    keys = [k for _, kinds in ACTION_CATALOG for k in kinds]
    assert "monitor" in keys
    param_names = [p[0] for p in ACTION_TYPES["monitor"]["params"]]
    assert param_names == ["metric", "style", "interval", "target"]


def test_pressing_a_monitor_key_is_a_noop(caplog):
    execute(Action("monitor", {"metric": "cpu"}), context=None)
    assert "unhandled" not in caplog.text


# ---------------------------------------------------------------------------
# Controller tick behaviour (mock device, fake clock, stubbed sampler)
# ---------------------------------------------------------------------------
controller_mod = pytest.importorskip("fifine_deck.controller")
from fifine_deck.controller import DeckController          # noqa: E402
from fifine_deck.model import DeckConfig, Page             # noqa: E402
from tests.test_controller import MockDevice               # noqa: E402


class _ScriptedSampler(Sampler):
    """Returns a scripted series of readings and counts sample() calls."""

    def __init__(self, readings):
        super().__init__()
        self._readings = list(readings)
        self.calls = 0

    def sample(self, spec):
        self.calls += 1
        r = self._readings.pop(0) if self._readings else self._readings_last
        self._readings_last = r
        self._last[spec.key()] = r
        h = self._hist.setdefault(spec.key(), deque(maxlen=monitors.HISTORY_LEN))
        h.append(r.pct)
        return r


def _quiesce(c: DeckController):
    """Stop the background monitor thread so tests drive ticks by hand —
    otherwise a real-clock tick could race the fake-clock assertions."""
    c._monitor_stop.set()
    c._monitor_thread.join(timeout=5)
    c._monitor_state.clear()


def _monitored_controller(readings, style="number"):
    cfg = DeckConfig()
    kc = cfg.active_profile().pages[0].key(1)
    kc.action = Action("monitor", {"metric": "cpu", "style": style,
                                   "interval": "1"})
    c = DeckController(cfg)
    _quiesce(c)
    dev = MockDevice()
    assert c._setup_device(dev)
    c._sampler = _ScriptedSampler(readings)
    c._monitor_state.clear()
    dev.key_images.clear()
    dev.refreshes = 0
    return c, dev


def test_tick_paints_a_due_monitor_key_and_refreshes():
    c, dev = _monitored_controller([Reading(10.0, "10%")])
    try:
        c.monitor_tick(now=100.0)
        assert 1 in dev.key_images and dev.refreshes == 1
    finally:
        c.stop()


def test_unchanged_value_is_not_repushed_but_change_is():
    c, dev = _monitored_controller(
        [Reading(10.0, "10%"), Reading(10.0, "10%"), Reading(55.0, "55%")])
    try:
        c.monitor_tick(now=100.0)
        dev.key_images.clear()
        c.monitor_tick(now=101.0)          # same value -> no device write
        assert dev.key_images == {}
        c.monitor_tick(now=102.0)          # changed -> repainted
        assert 1 in dev.key_images
    finally:
        c.stop()


def test_graph_style_repaints_every_sample():
    c, dev = _monitored_controller(
        [Reading(10.0, "10%"), Reading(10.0, "10%")], style="graph")
    try:
        c.monitor_tick(now=100.0)
        dev.key_images.clear()
        c.monitor_tick(now=101.0)
        assert 1 in dev.key_images         # graphs always advance
    finally:
        c.stop()


def test_interval_gates_sampling():
    c, dev = _monitored_controller([Reading(10.0, "10%"), Reading(20.0, "20%")])
    try:
        c.monitor_tick(now=100.0)
        c.monitor_tick(now=100.4)          # 0.4 s < 1 s interval
        assert c._sampler.calls == 1
        c.monitor_tick(now=101.1)
        assert c._sampler.calls == 2
    finally:
        c.stop()


def test_page_without_monitor_keys_never_samples():
    cfg = DeckConfig()
    c = DeckController(cfg)
    _quiesce(c)
    dev = MockDevice()
    assert c._setup_device(dev)
    c._sampler = _ScriptedSampler([])
    try:
        c.monitor_tick(now=100.0)
        assert c._sampler.calls == 0       # acceptance: zero sampling overhead
    finally:
        c.stop()


def test_clearing_the_key_drops_monitor_state():
    c, dev = _monitored_controller([Reading(10.0, "10%")])
    try:
        c.monitor_tick(now=100.0)
        assert c._monitor_state
        c.page().keys[1].action = Action()          # key cleared by the user
        c.monitor_tick(now=101.0)
        assert not c._monitor_state
    finally:
        c.stop()


def test_render_key_paints_monitor_face_not_static_icon():
    c, dev = _monitored_controller([Reading(33.0, "33%")])
    try:
        c.monitor_tick(now=100.0)
        before = dev.key_images[1]
        c.render_key(1)                    # e.g. after a page re-render
        assert isinstance(dev.key_images[1], Image.Image)
        assert dev.key_images[1].size == before.size
        # and the next tick is forced to resample immediately
        assert 1 not in c._monitor_state
    finally:
        c.stop()


def test_flash_skips_monitor_keys():
    c, dev = _monitored_controller([Reading(33.0, "33%")])
    try:
        c.monitor_tick(now=100.0)
        painted = dev.key_images[1]
        c.flash_key(1, pressed=True)
        assert dev.key_images[1] is painted    # untouched by the flash
    finally:
        c.stop()


class _HookedSampler(_ScriptedSampler):
    """ScriptedSampler that fires a hook after each sample — simulates GUI
    mutations landing during the unlocked sampling window of monitor_tick."""

    def __init__(self, readings, hook):
        super().__init__(readings)
        self._hook = hook

    def sample(self, spec):
        r = super().sample(spec)
        self._hook()
        return r


def test_same_metric_keys_share_one_sample_per_tick():
    """cpu_percent / net counters are since-last-call deltas: sampling once per
    KEY hands every key after the first a garbage ~0 (review finding). All keys
    of a stream must share one sample."""
    cfg = DeckConfig()
    page = cfg.active_profile().pages[0]
    page.key(1).action = Action("monitor", {"metric": "cpu", "style": "number"})
    page.key(2).action = Action("monitor", {"metric": "cpu", "style": "graph"})
    c = DeckController(cfg)
    _quiesce(c)
    dev = MockDevice()
    assert c._setup_device(dev)
    c._sampler = _ScriptedSampler([Reading(42.0, "42%")])
    c._monitor_state.clear()
    dev.key_images.clear()
    try:
        c.monitor_tick(now=100.0)
        assert c._sampler.calls == 1            # ONE sample for the shared stream
        assert 1 in dev.key_images and 2 in dev.key_images   # both keys painted
    finally:
        c.stop()


def test_first_tick_samples_even_when_uptime_is_below_interval():
    # monotonic() is seconds-since-boot: with a 0.0 never-sampled sentinel a
    # fresh key looked "recently sampled" until uptime reached its interval.
    c, dev = _monitored_controller([Reading(10.0, "10%")])
    try:
        c.monitor_tick(now=0.2)
        assert 1 in dev.key_images
    finally:
        c.stop()


def test_render_page_resets_monitor_gates():
    c, dev = _monitored_controller([Reading(10.0, "10%"), Reading(11.0, "11%")])
    try:
        c.monitor_tick(now=100.0)
        assert c._monitor_state
        c.render_page()                # page/profile switch, import, reconnect
        assert c._monitor_state == {}  # next tick resamples immediately
    finally:
        c.stop()


def test_key_retyped_mid_tick_is_not_overpainted():
    c, dev = _monitored_controller([])
    def retype():
        c.page().keys[1].action = Action("launch_app", {"command": "true"})
    c._sampler = _HookedSampler([Reading(10.0, "10%")], retype)
    try:
        c.monitor_tick(now=100.0)
        assert dev.key_images == {}            # stale frame must not paint...
        assert 1 not in c._monitor_state       # ...nor stamp a stale gate
    finally:
        c.stop()


def test_page_switched_mid_tick_frames_are_dropped():
    c, dev = _monitored_controller([])
    c.config.active_profile().pages.append(Page(name="P2"))
    got = []
    c.on_monitor_image = lambda i, img, page_id="": got.append(i)
    def switch():
        c.page_index = 1
    c._sampler = _HookedSampler([Reading(10.0, "10%")], switch)
    try:
        c.monitor_tick(now=100.0)
        assert dev.key_images == {}            # old page's slot not painted
        assert got == []                       # GUI got no stale frame either
        assert 1 not in c._monitor_state
    finally:
        c.stop()


def test_monitor_excluded_from_knob_and_step_editors(qapp):
    # As a knob gesture or macro step a monitor is a silent no-op — it must
    # not be offered there, while staying available for keys.
    from fifine_deck.gui.widgets import (ActionParamsWidget, KnobEditor,
                                         _STEP_EXCLUDE)
    from fifine_deck.model import KnobConfig
    assert "monitor" in _STEP_EXCLUDE
    ke = KnobEditor(1, KnobConfig())
    for picker in ke._pickers.values():
        assert picker.type_combo.findData("monitor") == -1
    assert ActionParamsWidget().type_combo.findData("monitor") >= 0


def test_monitor_callback_receives_frames_and_survives_errors():
    c, dev = _monitored_controller([Reading(10.0, "10%"), Reading(90.0, "90%")])
    try:
        got = []
        def cb(index, img, page_id=""):
            got.append(index)
            raise RuntimeError("GUI went away")
        c.on_monitor_image = cb
        c.monitor_tick(now=100.0)
        c.monitor_tick(now=101.0)          # callback error must not stop ticks
        assert got == [1, 1]
    finally:
        c.stop()


# ---------------------------------------------------------------------------
# Release-audit regressions (pre-0.6.0 audit round)
# ---------------------------------------------------------------------------
import threading                                            # noqa: E402


class _FlakyDevice(MockDevice):
    """MockDevice whose next N image writes fail (transient USB hiccup)."""

    def __init__(self):
        super().__init__()
        self.fail_next = 0

    def set_key_image_pil(self, index, img):
        if self.fail_next > 0:
            self.fail_next -= 1
            raise IOError("transient write failure")
        super().set_key_image_pil(index, img)


def test_monitor_spec_edited_mid_tick_is_not_overpainted():
    """The GUI mutates KeyConfig IN PLACE, so identity checks alone let a
    stale frame overpaint a key whose metric was just changed — and its gate
    then suppressed the new spec for a full interval (audit finding)."""
    c, dev = _monitored_controller([])
    def edit():
        c.page().keys[1].action = Action("monitor",
                                         {"metric": "ram", "style": "number"})
    c._sampler = _HookedSampler([Reading(37.0, "37%")], edit)
    try:
        c.monitor_tick(now=100.0)
        assert dev.key_images == {}          # stale cpu frame must not paint
        assert 1 not in c._monitor_state     # and must not gate the new spec
        c._sampler = _ScriptedSampler([Reading(62.0, "62%")])
        c.monitor_tick(now=100.5)            # next tick serves the NEW spec
        assert 1 in dev.key_images
    finally:
        c.stop()


def test_failed_write_is_retried_even_with_a_stable_value():
    """A failed device write used to be stamped as painted; the unchanged-sig
    fast path then suppressed every retry while the value was stable — the
    key face stayed stale forever (audit finding). The GUI must not receive
    the frame either, or preview and device silently diverge."""
    cfg = DeckConfig()
    cfg.active_profile().pages[0].key(1).action = Action("monitor",
                                                         {"metric": "cpu"})
    c = DeckController(cfg)
    _quiesce(c)
    dev = _FlakyDevice()
    assert c._setup_device(dev)
    c._sampler = _ScriptedSampler([Reading(42.0, "42%")])
    c._monitor_state.clear()
    dev.key_images.clear()
    got = []
    c.on_monitor_image = lambda i, img, page_id="": got.append(i)
    dev.fail_next = 1
    try:
        c.monitor_tick(now=100.0)
        assert dev.key_images == {} and got == []     # failed + no GUI frame
        assert 1 not in c._monitor_state              # no gate left behind
        c.monitor_tick(now=100.5)                     # same stable value
        assert 1 in dev.key_images and got == [1]     # retried successfully
    finally:
        c.stop()


def test_one_bad_key_does_not_block_the_others(monkeypatch):
    cfg = DeckConfig()
    page = cfg.active_profile().pages[0]
    page.key(1).action = Action("monitor", {"metric": "cpu"})
    page.key(2).action = Action("monitor", {"metric": "ram"})
    c = DeckController(cfg)
    _quiesce(c)
    dev = MockDevice()
    assert c._setup_device(dev)
    c._sampler = _ScriptedSampler([Reading(10.0, "10%"), Reading(20.0, "20%")])
    c._monitor_state.clear()
    dev.key_images.clear()
    dev.refreshes = 0
    real = monitors.render_monitor
    def flaky(size, spec, reading, history=None,
              bg_color="#101020", text_color="#ffffff"):
        if spec.metric == "cpu":
            raise RuntimeError("boom")
        return real(size, spec, reading, history, bg_color, text_color)
    monkeypatch.setattr(monitors, "render_monitor", flaky)
    try:
        c.monitor_tick(now=100.0)
        assert 2 in dev.key_images and 1 not in dev.key_images
        assert dev.refreshes == 1            # final refresh still ran
    finally:
        c.stop()


def test_editor_preserves_persisted_excluded_action_type(qapp):
    """A stored action whose type the editor excludes (old monitor knob/step
    bindings) used to snap to the combo's first entry and get silently
    rewritten on the next unrelated edit (audit finding)."""
    from fifine_deck.gui.widgets import ActionParamsWidget
    w = ActionParamsWidget(exclude={"monitor"})
    w.set_action(Action("monitor", {"metric": "cpu", "style": "gauge"}))
    a = w.get_action()
    assert a.type == "monitor"
    assert a.params.get("metric") == "cpu"


def test_cpu_priming_is_per_thread():
    """psutil keys its cpu_percent baseline per thread; a process-wide primed
    flag would let another thread's first (garbage) reading through as real."""
    pytest.importorskip("psutil")
    s = Sampler()
    spec = MonitorSpec.from_params({"metric": "cpu"})
    assert s.sample(spec).pct is None            # this thread's warm-up
    assert s.sample(spec).pct is not None
    result = {}
    t = threading.Thread(target=lambda: result.update(first=s.sample(spec)))
    t.start()
    t.join()
    assert result["first"].pct is None           # new thread warms up separately


def test_vram_backend_reprobes_after_backend_death(tmp_path, monkeypatch):
    used, total = tmp_path / "u", tmp_path / "t"
    used.write_text("1000")
    total.write_text("4000")
    s = Sampler()
    monkeypatch.setattr(monitors, "_probe_vram",
                        lambda: ("amdgpu", str(used), str(total)))
    spec = MonitorSpec.from_params({"metric": "vram"})
    assert s.sample(spec).pct == pytest.approx(25.0)
    used.unlink()
    total.unlink()                               # GPU "hot-removed"
    assert not s.sample(spec).ok                 # degrades, doesn't crash
    assert s._vram_backend is None               # cache dropped -> re-probe
    monkeypatch.setattr(monitors, "_probe_vram", lambda: ("none",))
    assert not s.sample(spec).ok
    assert s._vram_backend == ("none",)          # quiet steady-state, no spam


def test_vram_retry_probe_is_not_cached(monkeypatch):
    """NVML installed but not ready (driver loading) must not freeze VRAM on
    n/a forever — the probe is retried on the next sample."""
    s = Sampler()
    monkeypatch.setattr(monitors, "_probe_vram", lambda: ("retry",))
    spec = MonitorSpec.from_params({"metric": "vram"})
    assert not s.sample(spec).ok
    assert s._vram_backend is None


# ---------------------------------------------------------------------------
# 0.7.0 metrics: GPU load, temperatures, clock (issue #3)
# ---------------------------------------------------------------------------
def test_gpu_none_backend_degrades(monkeypatch):
    monkeypatch.setattr(monitors, "_probe_gpu", lambda: ("none",))
    r = Sampler().sample(MonitorSpec.from_params({"metric": "gpu"}))
    assert not r.ok and r.text == "n/a"


def test_gpu_amdgpu_sysfs_backend(tmp_path, monkeypatch):
    busy = tmp_path / "gpu_busy_percent"
    busy.write_text("37\n")
    monkeypatch.setattr(monitors, "_probe_gpu", lambda: ("amdgpu", str(busy)))
    r = Sampler().sample(MonitorSpec.from_params({"metric": "gpu"}))
    assert r.ok and r.pct == pytest.approx(37.0)
    assert r.text == "37%" and r.sample == pytest.approx(37.0)


def test_gpu_nvml_backend(monkeypatch):
    rates = namedtuple("rates", "gpu memory")(62, 40)

    class _NVML:
        def nvmlDeviceGetUtilizationRates(self, handle):
            assert handle == "h0"
            return rates

    monkeypatch.setattr(monitors, "_probe_gpu", lambda: ("nvml", _NVML(), "h0"))
    r = Sampler().sample(MonitorSpec.from_params({"metric": "gpu"}))
    assert r.ok and r.pct == pytest.approx(62.0) and r.text == "62%"


def test_gpu_backend_death_reprobes_next_sample(tmp_path, monkeypatch):
    """Mirror of the VRAM lifecycle: a dead backend degrades to n/a, drops the
    cache, and the NEXT sample re-probes instead of warning forever."""
    busy = tmp_path / "gpu_busy_percent"
    busy.write_text("10")
    s = Sampler()
    monkeypatch.setattr(monitors, "_probe_gpu", lambda: ("amdgpu", str(busy)))
    spec = MonitorSpec.from_params({"metric": "gpu"})
    assert s.sample(spec).ok
    busy.unlink()                                # driver unloaded under us
    assert not s.sample(spec).ok
    assert s._gpu_backend is None                # cache dropped -> re-probe
    busy.write_text("55")
    assert s.sample(spec).pct == pytest.approx(55.0)


def test_gpu_retry_probe_is_not_cached(monkeypatch):
    calls = []
    def probe():
        calls.append(1)
        return ("retry",)
    monkeypatch.setattr(monitors, "_probe_gpu", probe)
    s = Sampler()
    spec = MonitorSpec.from_params({"metric": "gpu"})
    assert not s.sample(spec).ok
    assert not s.sample(spec).ok
    assert len(calls) == 2 and s._gpu_backend is None


_shw = namedtuple("shwtemp", "label current high critical")


def _fake_temps(monkeypatch, mapping):
    class _P:
        @staticmethod
        def sensors_temperatures():
            return mapping
    monkeypatch.setattr(monitors, "psutil", _P)


def test_temp_auto_prefers_cpu_package(monkeypatch):
    _fake_temps(monkeypatch, {
        "nvme": [_shw("Composite", 33.0, None, None)],
        "coretemp": [_shw("Core 0", 50.0, None, None),
                     _shw("Package id 0", 40.0, None, None)],
    })
    r = Sampler().sample(MonitorSpec.from_params({"metric": "temp"}))
    assert r.ok and r.text == "40°C" and r.sub == "Package id 0"
    assert r.pct == pytest.approx(40.0) and r.sample == pytest.approx(40.0)


def test_temp_auto_amd_tctl_when_no_coretemp(monkeypatch):
    _fake_temps(monkeypatch, {
        "acpitz": [_shw("", 27.8, None, None)],
        "k10temp": [_shw("Tctl", 48.5, None, None)],
    })
    r = Sampler().sample(MonitorSpec.from_params({"metric": "temp"}))
    assert r.text == "48°C" and r.sub == "Tctl"


def test_temp_target_selects_chip_and_label(monkeypatch):
    _fake_temps(monkeypatch, {
        "coretemp": [_shw("Package id 0", 40.0, None, None)],
        "nvme": [_shw("Composite", 33.0, None, None),
                 _shw("Sensor 2", 41.0, None, None)],
    })
    s = Sampler()
    r = s.sample(MonitorSpec.from_params({"metric": "temp", "target": "nvme"}))
    assert r.text == "33°C" and r.sub == "Composite"
    r = s.sample(MonitorSpec.from_params({"metric": "temp",
                                          "target": "NVMe:sen"}))
    assert r.text == "41°C" and r.sub == "Sensor 2"    # case-insensitive TRUE prefix
    r = s.sample(MonitorSpec.from_params({"metric": "temp",
                                          "target": "nvme:comp"}))
    assert r.text == "33°C" and r.sub == "Composite"


def test_temp_unknown_target_and_no_sensors_degrade(monkeypatch):
    _fake_temps(monkeypatch, {"coretemp": [_shw("Package id 0", 40.0, None, None)]})
    s = Sampler()
    r = s.sample(MonitorSpec.from_params({"metric": "temp", "target": "gpu0"}))
    assert not r.ok and r.text == "n/a"
    _fake_temps(monkeypatch, {})
    r = s.sample(MonitorSpec.from_params({"metric": "temp"}))
    assert not r.ok and "no temp sensors" in r.sub


def test_temp_gauge_pct_is_clamped_but_sample_is_raw(monkeypatch):
    _fake_temps(monkeypatch, {"coretemp": [_shw("Package id 0", 105.7, None, None)]})
    r = Sampler().sample(MonitorSpec.from_params({"metric": "temp"}))
    assert r.pct == 100.0                        # gauge axis is 0..100
    assert r.sample == pytest.approx(105.7)      # graph keeps the real value
    assert r.text == "106°C"


def test_temp_keeps_its_target_in_the_spec():
    spec = MonitorSpec.from_params({"metric": "temp", "target": "nvme"})
    assert spec.target == "nvme"
    assert spec.key() == ("temp", "nvme")        # separate stream per sensor


def test_clock_formats_follow_the_interval(monkeypatch):
    import time as _time
    fixed = _time.struct_time((2026, 7, 18, 13, 57, 4, 5, 199, 1))
    monkeypatch.setattr(monitors.time, "localtime", lambda: fixed)
    s = Sampler()
    r = s.sample(MonitorSpec.from_params({"metric": "clock"}))
    assert r.text == "13:57:04"                  # fast refresh shows seconds
    assert r.pct is None and r.sample is None and "18" in r.sub
    r = s.sample(MonitorSpec.from_params({"metric": "clock", "interval": "30"}))
    assert r.text == "13:57"                     # slow refresh drops them


def test_clock_needs_no_psutil(monkeypatch):
    monkeypatch.setattr(monitors, "psutil", None)
    r = Sampler().sample(MonitorSpec.from_params({"metric": "clock"}))
    assert r.ok and r.text


def test_new_metrics_are_offered_in_the_editor_choice():
    spec = dict(ACTION_TYPES)["monitor"]["params"]
    metric_kinds = [k for key, k, _ in spec if key == "metric"]
    for m in ("gpu", "temp", "clock"):
        assert m in metric_kinds[0]


@pytest.mark.parametrize("metric", ["gpu", "temp", "clock"])
@pytest.mark.parametrize("style", ["number", "gauge", "graph"])
def test_new_metrics_render_in_every_style(metric, style):
    spec = MonitorSpec.from_params({"metric": metric, "style": style})
    reading = Reading(41.0, "41", "x", sample=41.0) if metric != "clock" \
        else Reading(None, "13:57", "Sat 18 Jul")
    img = render_monitor(96, spec, reading, history=[10.0, None, 41.0])
    assert img.size == (96, 96) and img.mode == "RGB"
    # and it actually drew the reading — a blank key must not pass
    bg = img.load()[0, 0]
    assert any(img.load()[x, y] != bg
               for x in range(0, 96, 2) for y in range(0, 96, 2))


def test_gauge_value_text_is_arm_length_readable():
    """0.6.0's gauge value (20% of key size) read small on the deck — issue #3
    asks for ~26%. Measure the rendered glyph height of the pure-white value
    text: at size 100 the taller face must span >= 17 rows (the old face
    peaked around 14). Fails on the 0.6.2 renderer."""
    spec = MonitorSpec(metric="cpu", style="gauge")
    img = render_monitor(100, spec, Reading(50.0, "50%", sample=50.0))
    px = img.load()
    white_rows = [y for y in range(100)
                  if any(px[x, y] == (255, 255, 255) for x in range(100))]
    assert white_rows, "value text missing from the gauge face"
    span = max(white_rows) - min(white_rows) + 1
    assert span >= 17, f"gauge value text only spans {span} rows"


def test_gauge_label_moved_into_the_bottom_opening():
    """The metric label now sits in the 270°-arc's bottom gap (y ~= 0.84),
    not inside the arc where it crowded the value."""
    spec = MonitorSpec(metric="cpu", style="gauge")
    img = render_monitor(100, spec, Reading(50.0, "50%", sample=50.0))
    px = img.load()
    bg, fg = (16, 16, 32), (255, 255, 255)
    dim = tuple(int(bg[i] + (fg[i] - bg[i]) * 0.55) for i in range(3))
    # single dim pixels also appear as antialias blends of the big white
    # value text — the label proper produces DENSE dim rows (>= 4 px)
    dim_rows = [y for y in range(100)
                if sum(px[x, y] == dim for x in range(100)) >= 4]
    assert dim_rows and min(dim_rows) >= 80, f"label rows: {dim_rows[:5]}..."


# ---------------------------------------------------------------------------
# 0.7.0 audit findings (regressions pinned)
# ---------------------------------------------------------------------------
class _DeadNVML:
    """pynvml imports fine (it is pure Python) but init fails — the permanent
    state of every AMD-only machine with the recommended package installed."""
    @staticmethod
    def nvmlInit():
        raise RuntimeError("NVML library not found")


def test_probe_gpu_falls_back_to_amdgpu_when_nvml_is_dead(tmp_path, monkeypatch):
    """deb Recommends / snap bundles pynvml unconditionally, so on AMD systems
    the import succeeds and nvmlInit() fails forever — the sysfs backend must
    still be found (the buggy probe returned ("retry",) forever instead)."""
    import sys as _sys
    monkeypatch.setitem(_sys.modules, "pynvml", _DeadNVML)
    busy = tmp_path / "gpu_busy_percent"
    busy.write_text("55\n")
    monkeypatch.setattr(monitors.glob, "glob",
                        lambda pat: [str(busy)] if "gpu_busy_percent" in pat else [])
    assert monitors._probe_gpu() == ("amdgpu", str(busy))
    r = Sampler().sample(MonitorSpec.from_params({"metric": "gpu"}))
    assert r.ok and r.pct == pytest.approx(55.0)


def test_probe_gpu_retry_only_when_nvml_present_and_no_amdgpu(monkeypatch):
    import sys as _sys
    monkeypatch.setattr(monitors.glob, "glob", lambda pat: [])
    monkeypatch.setitem(_sys.modules, "pynvml", _DeadNVML)
    assert monitors._probe_gpu() == ("retry",)
    monkeypatch.setitem(_sys.modules, "pynvml", None)     # import -> ImportError
    assert monitors._probe_gpu() == ("none",)


def test_probe_vram_falls_back_to_amdgpu_when_nvml_is_dead(tmp_path, monkeypatch):
    """_probe_vram shipped the same NVML-shadows-amdgpu flaw since 0.6.0."""
    import sys as _sys
    monkeypatch.setitem(_sys.modules, "pynvml", _DeadNVML)
    total = tmp_path / "mem_info_vram_total"
    used = tmp_path / "mem_info_vram_used"
    total.write_text("1000")
    used.write_text("250")
    monkeypatch.setattr(monitors.glob, "glob",
                        lambda pat: [str(total)] if "vram_total" in pat else [])
    assert monitors._probe_vram() == ("amdgpu", str(used), str(total))
    monkeypatch.setattr(monitors.glob, "glob", lambda pat: [])
    assert monitors._probe_vram() == ("retry",)


def test_clock_format_bands_do_not_share_a_stream(monkeypatch):
    """The clock Reading bakes in its format (seconds under 5 s), so clocks in
    different bands must have different stream keys — with a shared stream a
    30 s key froze another key's seconds display on its LCD."""
    fast = MonitorSpec.from_params({"metric": "clock"})               # 1 s
    slow = MonitorSpec.from_params({"metric": "clock", "interval": "30"})
    same_band = MonitorSpec.from_params({"metric": "clock", "interval": "2"})
    assert fast.key() != slow.key()
    assert fast.key() == same_band.key()                  # cheap sharing kept
    import time as _time
    fixed = _time.struct_time((2026, 7, 18, 13, 57, 4, 5, 199, 1))
    monkeypatch.setattr(monitors.time, "localtime", lambda: fixed)
    s = Sampler()
    assert s.sample(fast).text == "13:57:04"
    assert s.last(slow).text != "13:57:04"    # slow stream untouched
    assert s.sample(slow).text == "13:57"


def test_gauge_value_never_overdraws_the_arc():
    """Audit finding: at 26% a 4+ glyph value ("100%", any temp reading) was
    wider than the arc's inner opening and painted across the stroke on both
    sides. The value text (pure white) must stay inside the inner opening —
    long values shrink, they don't collide."""
    for text, pct in (("100%", 100.0), ("100°C", 100.0), ("78°C", 78.0)):
        spec = MonitorSpec(metric="temp", style="gauge")
        img = render_monitor(100, spec, Reading(pct, text, sample=pct))
        px = img.load()
        white_cols = [x for x in range(100)
                      if any(px[x, y] == (255, 255, 255) for y in range(100))]
        assert white_cols, text
        # inner opening at size 100: margin 10 + stroke 9 per side -> 19..81
        assert min(white_cols) >= 19 and max(white_cols) <= 81, \
            f"{text}: value spans columns {min(white_cols)}..{max(white_cols)}"
