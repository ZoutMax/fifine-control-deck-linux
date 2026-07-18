"""
System-monitor keys: live CPU / RAM / VRAM / GPU / temperature / network /
disk readouts — plus a clock face — rendered onto a key's LCD (like the
monitor widgets in the official Stream Dock app).

Two halves:
- Sampler   — polls the metrics (psutil for CPU/RAM/disk/network/temps,
              per-vendor sources for VRAM and GPU load) and keeps the short
              history a sparkline needs.
- render_monitor — draws a Reading as a key image in one of three styles
              (number / gauge / graph), reusing the app font + colour helpers.

The controller owns one Sampler and ticks it on a background thread; the GUI
only ever renders placeholders (live frames arrive from the controller), so
nothing here may touch Qt.
"""
from __future__ import annotations

import glob
import logging
import os
import threading
import time
from collections import deque
from dataclasses import dataclass

from PIL import Image, ImageDraw

from .rendering import _font, _hex

log = logging.getLogger(__name__)

try:
    import psutil
except ImportError:          # packaged builds depend on it; source runs may not
    psutil = None            # type: ignore[assignment]

METRICS = {
    "cpu": "CPU",
    "ram": "RAM",
    "vram": "VRAM",
    "gpu": "GPU",
    "temp": "TEMP",
    "net": "NET",
    "disk": "DISK",
    "clock": "CLOCK",
}
STYLES = ("number", "gauge", "graph")
# Metrics on a fixed 0..100 axis (gauge + graph scale). temp is °C, not a
# percentage, but shares the axis: 0-100°C covers consumer hardware and the
# 90 warn threshold doubles as a sensible thermal alarm.
PERCENT_METRICS = frozenset({"cpu", "ram", "vram", "gpu", "disk", "temp"})
# the only metrics a target applies to (disk mount, net iface, temp sensor)
TARGETED_METRICS = frozenset({"disk", "net", "temp"})

HISTORY_LEN = 32             # sparkline points kept per metric

ACCENT = (64, 158, 255)      # matches the GUI accent #409eff
WARN = (255, 92, 92)         # gauge/graph turn red above WARN_PCT
WARN_PCT = 90.0


@dataclass
class Reading:
    """One sampled value. pct is 0..100 where the metric has a natural
    percentage (CPU/RAM/VRAM/disk), None otherwise (network rates, errors)."""
    pct: float | None
    text: str                # big value line ("37%", "1.2 MB/s")
    sub: str = ""            # small detail line ("6.2/16 GB", "↑ 340 kB/s")
    ok: bool = True
    # What the sparkline records for this sample. None = a gap (failed sample,
    # warm-up), which the graph must SKIP — recording 0.0 instead would draw a
    # false dip to zero.
    sample: float | None = None


@dataclass(frozen=True)
class MonitorSpec:
    """Validated monitor parameters for one key (parsed from Action.params)."""
    metric: str = "cpu"
    style: str = "number"
    interval: float = 1.0
    target: str = ""         # disk mount point or network interface ("" = auto)

    @classmethod
    def from_params(cls, params: dict | None) -> "MonitorSpec":
        p = params or {}
        metric = str(p.get("metric", "cpu")).strip().lower()
        if metric not in METRICS:
            metric = "cpu"
        style = str(p.get("style", "number")).strip().lower()
        if style not in STYLES:
            style = "number"
        try:
            interval = float(str(p.get("interval", "") or "1").strip())
        except (TypeError, ValueError):
            interval = 1.0
        interval = max(0.5, min(60.0, interval))
        target = str(p.get("target", "")).strip()
        if metric not in TARGETED_METRICS:
            # A stray target on cpu/ram/vram would needlessly split the shared
            # sample stream (and with it psutil's global delta state).
            target = ""
        return cls(metric=metric, style=style, interval=interval, target=target)

    def key(self) -> tuple:
        """Stream key: keys with the same metric+target share ONE sample
        stream (one reading + one history per tick). This is not just an
        optimisation — cpu_percent and net counters are since-last-call
        deltas, so sampling a stream twice in quick succession returns
        garbage (~0) to the second caller."""
        if self.metric == "clock":
            # A clock Reading bakes in its text format (seconds only under a
            # 5 s interval), so clocks in different format bands must not
            # share one Reading — a 30 s key would freeze a seconds display.
            return ("clock", "sec" if self.interval < 5 else "min")
        return (self.metric, self.target)


def placeholder(spec: MonitorSpec) -> Reading:
    """What a monitor key shows before its first sample arrives."""
    return Reading(None, "—", METRICS.get(spec.metric, ""))


def _fmt_bytes(n: float) -> str:
    for unit in ("B", "kB", "MB", "GB", "TB"):
        if abs(n) < 1000 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("B", "kB") else f"{n:.1f} {unit}"
        n /= 1000.0
    return f"{n:.1f} TB"


def _fmt_rate(bps: float) -> str:
    return _fmt_bytes(bps) + "/s"


class Sampler:
    """Stateful metric poller. One instance per controller; not thread-safe by
    design — only the controller's monitor thread calls sample(), while other
    threads only read the last-reading cache (atomic dict ops under the GIL)."""

    def __init__(self):
        self._hist: dict[tuple, deque] = {}
        self._last: dict[tuple, Reading] = {}
        self._net_prev: dict[str, tuple[float, int, int]] = {}
        self._vram_backend = None      # probed lazily; ("none",) when absent
        self._gpu_backend = None       # same lifecycle as _vram_backend
        # psutil keys its cpu_percent since-last-call baseline PER THREAD, so
        # priming must be per thread too — a flag primed on one thread would
        # let another thread's first (garbage) reading through as real.
        self._cpu_primed_threads: set[int] = set()

    # -- public ------------------------------------------------------------
    def sample(self, spec: MonitorSpec) -> Reading:
        """Take ONE sample of this spec's stream. The caller must call this at
        most once per stream per tick (see MonitorSpec.key) — every key of the
        stream then shares the returned reading."""
        fn = getattr(self, f"_sample_{spec.metric}", None)
        try:
            reading = fn(spec) if fn else Reading(None, "n/a", ok=False)
        except Exception as e:                      # a bad mount/iface must not
            log.warning("monitor %s failed: %s", spec.metric, e)   # kill ticks
            reading = Reading(None, "n/a", METRICS.get(spec.metric, ""), ok=False)
        k = spec.key()
        self._last[k] = reading
        hist = self._hist.setdefault(k, deque(maxlen=HISTORY_LEN))
        hist.append(reading.sample)
        return reading

    def last(self, spec: MonitorSpec) -> Reading:
        return self._last.get(spec.key()) or placeholder(spec)

    def history(self, spec: MonitorSpec) -> list[float | None]:
        return list(self._hist.get(spec.key(), ()))

    # -- metrics -----------------------------------------------------------
    def _sample_cpu(self, spec: MonitorSpec) -> Reading:
        if psutil is None:
            return _NO_PSUTIL
        pct = psutil.cpu_percent(interval=None)
        tid = threading.get_ident()
        if tid not in self._cpu_primed_threads:
            # psutil documents the first non-blocking cpu_percent() as a
            # meaningless 0.0 (no since-last-call window yet) — show a
            # warm-up frame instead of a fake 0%.
            self._cpu_primed_threads.add(tid)
            return Reading(None, "…", METRICS["cpu"])
        return Reading(pct, f"{pct:.0f}%", f"{os.cpu_count() or '?'} cores",
                       sample=pct)

    def _sample_ram(self, spec: MonitorSpec) -> Reading:
        if psutil is None:
            return _NO_PSUTIL
        vm = psutil.virtual_memory()
        return Reading(vm.percent, f"{vm.percent:.0f}%",
                       f"{_fmt_bytes(vm.used)} / {_fmt_bytes(vm.total)}",
                       sample=vm.percent)

    def _sample_disk(self, spec: MonitorSpec) -> Reading:
        if psutil is None:
            return _NO_PSUTIL
        du = psutil.disk_usage(spec.target or "/")
        return Reading(du.percent, f"{du.percent:.0f}%",
                       f"{_fmt_bytes(du.free)} free", sample=du.percent)

    def _sample_net(self, spec: MonitorSpec) -> Reading:
        if psutil is None:
            return _NO_PSUTIL
        if spec.target:
            per = psutil.net_io_counters(pernic=True)
            io = per.get(spec.target)
            if io is None:
                return Reading(None, "n/a", f"no iface {spec.target}", ok=False)
        else:
            io = psutil.net_io_counters()
        now = time.monotonic()
        prev = self._net_prev.get(spec.target)
        self._net_prev[spec.target] = (now, io.bytes_recv, io.bytes_sent)
        if prev is None or now <= prev[0]:
            return Reading(None, "…", METRICS["net"])
        dt = now - prev[0]
        down = max(0.0, (io.bytes_recv - prev[1]) / dt)
        up = max(0.0, (io.bytes_sent - prev[2]) / dt)
        # the graph plots the download rate
        return Reading(None, f"↓ {_fmt_rate(down)}", f"↑ {_fmt_rate(up)}",
                       sample=down)

    def _sample_vram(self, spec: MonitorSpec) -> Reading:
        b = self._vram_backend
        if b is None:
            b = _probe_vram()
            if b[0] != "retry":
                # "retry" (NVML installed but not ready — driver still
                # loading?) is deliberately NOT cached: probe again next
                # sample instead of freezing on n/a forever.
                self._vram_backend = b
        if b[0] not in ("nvml", "amdgpu"):
            return Reading(None, "n/a", "no dedicated GPU", ok=False)
        try:
            if b[0] == "nvml":
                info = b[1].nvmlDeviceGetMemoryInfo(b[2])
                used, total = info.used, info.total
            else:
                with open(b[1]) as f:
                    used = int(f.read())
                with open(b[2]) as f:
                    total = int(f.read())
        except Exception:
            # The backend died under us (driver unload, GPU hot-remove):
            # drop the cache so the next sample re-probes instead of
            # warning every interval forever.
            self._vram_backend = None
            raise
        pct = 100.0 * used / total if total else 0.0
        return Reading(pct, f"{pct:.0f}%",
                       f"{_fmt_bytes(used)} / {_fmt_bytes(total)}", sample=pct)

    def _sample_gpu(self, spec: MonitorSpec) -> Reading:
        b = self._gpu_backend
        if b is None:
            b = _probe_gpu()
            if b[0] != "retry":
                # "retry" is NOT cached — probe again next sample (see vram).
                self._gpu_backend = b
        if b[0] not in ("nvml", "amdgpu"):
            return Reading(None, "n/a", "no dedicated GPU", ok=False)
        try:
            if b[0] == "nvml":
                pct = float(b[1].nvmlDeviceGetUtilizationRates(b[2]).gpu)
            else:
                with open(b[1]) as f:
                    pct = float(f.read())
        except Exception:
            # backend died (driver unload / hot-remove): re-probe next sample
            self._gpu_backend = None
            raise
        return Reading(pct, f"{pct:.0f}%", "load", sample=pct)

    def _sample_temp(self, spec: MonitorSpec) -> Reading:
        if psutil is None:
            return _NO_PSUTIL
        temps: dict = getattr(psutil, "sensors_temperatures", lambda: {})() or {}
        picked = _pick_temp(temps, spec.target)
        if picked is None:
            return Reading(None, "n/a",
                           f"no sensor {spec.target}" if spec.target
                           else "no temp sensors", ok=False)
        label, val = picked
        # °C on the shared 0..100 gauge axis; graph records the raw value
        pct = max(0.0, min(100.0, val))
        return Reading(pct, f"{val:.0f}°C", label, sample=val)

    def _sample_clock(self, spec: MonitorSpec) -> Reading:
        # No psutil needed. Seconds only at fast refresh — at slow intervals a
        # seconds display would just show a stale value between pushes.
        now = time.localtime()
        fmt = "%H:%M:%S" if spec.interval < 5 else "%H:%M"
        return Reading(None, time.strftime(fmt, now),
                       time.strftime("%a %d %b", now))


_NO_PSUTIL = Reading(None, "n/a", "psutil missing", ok=False)

# Chips whose first matching entry is the CPU-package temperature, in
# preference order (Intel, AMD, AMD-alt, ARM SBCs, ACPI fallback).
_TEMP_PREFERRED = ("coretemp", "k10temp", "zenpower", "cpu_thermal", "acpitz")


def _pick_temp(temps: dict, target: str) -> tuple[str, float] | None:
    """Pick one (label, current °C) from psutil.sensors_temperatures().

    target "" = auto: prefer a CPU package sensor, else the first chip that
    reports anything. Explicit targets select "chip" or "chip:label"
    (case-insensitive, label matched by prefix — "nvme:comp" hits Composite).
    """
    if target:
        chip, _, want = target.partition(":")
        chip, want = chip.strip().lower(), want.strip().lower()
        for name, entries in temps.items():
            if name.lower() != chip:
                continue
            for e in entries:
                lbl = (getattr(e, "label", "") or "").lower()
                if not want or lbl.startswith(want):
                    return (getattr(e, "label", "") or name, float(e.current))
        return None
    for chip in _TEMP_PREFERRED:
        for name, entries in temps.items():
            if name.lower() == chip and entries:
                e = _pkg_entry(entries)
                return (getattr(e, "label", "") or name, float(e.current))
    for name, entries in temps.items():
        if entries:
            e = entries[0]
            return (getattr(e, "label", "") or name, float(e.current))
    return None


def _pkg_entry(entries):
    """The package/whole-die entry of a CPU chip, else its first entry."""
    for e in entries:
        lbl = (getattr(e, "label", "") or "").lower()
        if lbl.startswith(("package", "tctl", "tdie")):
            return e
    return entries[0]


def _probe_vram():
    """Find a VRAM source: NVIDIA via NVML, AMD via sysfs, else none.
    Intel iGPUs share system RAM — there is nothing meaningful to report.

    NVML failing does NOT mean no GPU: pynvml is pure Python (the deb
    Recommends it, the snap bundles it), so on AMD-only machines the import
    succeeds and nvmlInit() raises library-not-found forever. The amdgpu
    sysfs probe therefore runs on ANY NVML failure — ("retry",) is only
    returned when NVML failed AND no amdgpu node exists (e.g. an NVIDIA
    driver still loading), so the caller re-probes instead of caching a
    permanent failure."""
    nvml_present = False
    try:
        import pynvml
        nvml_present = True
        pynvml.nvmlInit()
        return ("nvml", pynvml, pynvml.nvmlDeviceGetHandleByIndex(0))
    except Exception:
        pass
    for total in sorted(glob.glob("/sys/class/drm/card*/device/mem_info_vram_total")):
        used = os.path.join(os.path.dirname(total), "mem_info_vram_used")
        if os.path.exists(used):
            return ("amdgpu", used, total)
    return ("retry",) if nvml_present else ("none",)


def _probe_gpu():
    """Find a GPU-load source: NVIDIA via NVML utilization rates, AMD via the
    sysfs gpu_busy_percent file. Same fallback/retry semantics as
    _probe_vram (see there for why amdgpu must run after an NVML failure)."""
    nvml_present = False
    try:
        import pynvml
        nvml_present = True
        pynvml.nvmlInit()
        return ("nvml", pynvml, pynvml.nvmlDeviceGetHandleByIndex(0))
    except Exception:
        pass
    for busy in sorted(glob.glob("/sys/class/drm/card*/device/gpu_busy_percent")):
        return ("amdgpu", busy)
    return ("retry",) if nvml_present else ("none",)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def _mix(a, b, t: float):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def render_monitor(size: int, spec: MonitorSpec, reading: Reading,
                   history: list[float | None] | None = None,
                   bg_color: str = "#101020",
                   text_color: str = "#ffffff") -> Image.Image:
    """Draw one monitor frame as an upright RGB key image."""
    bg = _hex(bg_color)
    fg = _hex(text_color, (255, 255, 255))
    dim = _mix(bg, fg, 0.55)                       # muted label colour
    img = Image.new("RGB", (size, size), bg)
    draw = ImageDraw.Draw(img)
    style = spec.style if reading.pct is not None or spec.style == "graph" \
        else "number"                              # gauge needs a percentage

    label = METRICS.get(spec.metric, spec.metric.upper())
    accent = WARN if (reading.pct or 0) >= WARN_PCT else ACCENT

    if style == "gauge":
        _draw_gauge(draw, size, reading.pct or 0.0, accent, _mix(bg, fg, 0.18))
        # Value text fills the arc (26% of key size, up from 20%) — the 0.6.0
        # face read small at arm's length on the physical deck. The label
        # moves into the gauge's bottom opening instead of crowding the arc.
        # Width is capped to the arc's INNER opening (0.62·size: margin 0.10 +
        # stroke 0.09 per side), not the key width — at 26% a 4+ glyph value
        # like "100%" or any "…°C" reading would overdraw the arc stroke.
        _center_text(draw, reading.text, size, size * 0.36, int(size * 0.26),
                     fg, max_w=int(size * 0.62))
        _center_text(draw, label, size, size * 0.84, int(size * 0.11), dim)
    elif style == "graph":
        _center_text(draw, label, size, size * 0.06, int(size * 0.11), dim)
        _center_text(draw, reading.text, size, size * 0.18, int(size * 0.17), fg)
        _draw_graph(draw, size, history or [], spec.metric in PERCENT_METRICS,
                    accent, _mix(bg, fg, 0.12))
    else:                                          # number
        _center_text(draw, label, size, size * 0.08, int(size * 0.12), dim)
        _center_text(draw, reading.text, size, size * 0.34, int(size * 0.24), fg)
        if reading.sub:
            _center_text(draw, reading.sub, size, size * 0.72, int(size * 0.11), dim)
    return img


def _center_text(draw, text: str, size: int, y: float, fs: int, fill,
                 max_w: int | None = None):
    if not text:
        return
    fs = max(8, fs)
    limit = max_w if max_w is not None else size - 6
    while fs > 8 and draw.textlength(text, font=_font(fs)) > limit:
        fs -= 1
    font = _font(fs)
    bb = draw.textbbox((0, 0), text, font=font)
    x = int((size - (bb[2] - bb[0])) // 2 - bb[0])
    draw.text((x, int(y)), text, font=font, fill=fill)


def _draw_gauge(draw, size: int, pct: float, accent, track):
    """270° arc gauge, opening at the bottom."""
    m = int(size * 0.10)
    box = (m, m, size - m, size - m)
    width = max(4, int(size * 0.09))
    start, span = 135, 270
    draw.arc(box, start, start + span, fill=track, width=width)
    frac = max(0.0, min(1.0, pct / 100.0))
    if frac > 0:
        draw.arc(box, start, start + int(span * frac), fill=accent, width=width)


def _draw_graph(draw, size: int, history: list[float | None], is_percent: bool,
                accent, grid):
    """Sparkline over the lower part of the key. Percent metrics are scaled to
    0..100; rate metrics normalize to the window's maximum. None entries are
    gaps (failed samples / warm-up) and are skipped, never drawn as zero."""
    top, bottom = int(size * 0.42), int(size * 0.94)
    left, right = int(size * 0.06), int(size * 0.94)
    draw.rectangle((left, top, right, bottom), outline=grid, width=1)
    pts = [v for v in history if v is not None]
    if len(pts) < 2:
        return
    scale = 100.0 if is_percent else max(max(pts), 1.0)
    h, w = bottom - top, right - left
    n = len(pts)
    xy = []
    for i, v in enumerate(pts):
        x = left + int(w * i / (n - 1))
        y = bottom - int(h * max(0.0, min(1.0, v / scale)))
        xy.append((x, y))
    # soft fill under the line, then the line itself
    poly = xy + [(xy[-1][0], bottom), (xy[0][0], bottom)]
    draw.polygon(poly, fill=_mix(grid, accent, 0.35))
    draw.line(xy, fill=accent, width=2)
