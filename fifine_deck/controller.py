"""
Runtime controller: connects a FifineDeck to a DeckConfig and the action engine.

- Renders the active page onto the physical keys.
- Dispatches key presses to bound actions.
- Implements the ActionContext (page/profile/brightness operations).
- Handles hotplug so unplug/replug re-applies the current page.

GUI-agnostic: optional callbacks (on_connect / on_disconnect / on_key_event /
on_page_changed) let a GUI observe state without this module importing Qt.
"""
from __future__ import annotations

import threading
import time
from typing import Callable, Optional

import os
import queue

from . import actions, rendering
from .device import FifineDeck, register, DEVICE_PROFILE
from .model import DeckConfig, Profile, Page, KeyConfig

from StreamDock.DeviceManager import DeviceManager
from StreamDock.InputTypes import EventType


class DeckController:
    def __init__(self, config: DeckConfig):
        self.config = config
        self.manager: Optional[DeviceManager] = None
        self.device: Optional[FifineDeck] = None
        self.page_index = 0
        self._lock = threading.RLock()
        self._listen_thread: Optional[threading.Thread] = None
        self._running = False
        self._gif_keys: set[int] = set()   # logical keys currently animated

        # Actions run on a dedicated worker thread so a slow action (e.g. a
        # multi-action with delays, or a blocking command) never stalls the
        # SDK reader thread that delivers key events.
        self._action_queue: queue.Queue = queue.Queue()
        self._action_thread = threading.Thread(target=self._action_worker, daemon=True)
        self._action_thread.start()

        # observer callbacks (optional)
        self.on_connect: Optional[Callable[[FifineDeck], None]] = None
        self.on_disconnect: Optional[Callable[[], None]] = None
        self.on_key_event: Optional[Callable[[int, bool], None]] = None
        self.on_page_changed: Optional[Callable[[], None]] = None

        register()

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> bool:
        """Enumerate + open the first device, then listen for hotplug."""
        self._running = True
        self.manager = DeviceManager()
        found = self.manager.enumerate()
        opened = False
        for dev in found:
            if isinstance(dev, FifineDeck):
                if self._setup_device(dev):
                    opened = True
                    break
        self._listen_thread = threading.Thread(target=self._listen, daemon=True)
        self._listen_thread.start()
        return opened

    def _listen(self):
        try:
            self.manager.listen(
                on_device_added=self._on_added,
                on_device_removed=self._on_removed,
                auto_open=False,
            )
        except Exception as e:
            print(f"[controller] hotplug listener stopped: {e}", flush=True)

    def _on_added(self, dev):
        if self._running and isinstance(dev, FifineDeck) and self.device is None:
            self._setup_device(dev)

    def _on_removed(self, dev):
        if dev is self.device:
            with self._lock:
                self.device = None
            if self.on_disconnect:
                self.on_disconnect()

    def _setup_device(self, dev: FifineDeck) -> bool:
        try:
            if not dev.open():
                print("[controller] open() failed (permissions? udev rule installed?)",
                      flush=True)
                return False
            dev.init()
            with self._lock:
                self.device = dev
                self.page_index = 0
            dev.set_key_callback(self._key_callback)
            self.apply_brightness()
            self.render_page()
            if self.on_connect:
                self.on_connect(dev)
            print(f"[controller] connected: fw={dev.firmware_version!r} "
                  f"keys={dev.KEY_COUNT}", flush=True)
            return True
        except Exception as e:
            print(f"[controller] device setup failed: {e}", flush=True)
            return False

    def stop(self):
        self._running = False
        self._action_queue.put(None)   # unblock + end the action worker
        with self._lock:
            dev = self.device
            if dev:
                try:
                    dev.set_key_callback(None)
                    time.sleep(0.05)
                    # Stop animations first so the GIF loop can't repaint the
                    # keys we are about to clear.
                    dev.stop_gif_loop()
                    self._gif_keys.clear()
                    dev.clearAllIcon()
                    dev.refresh()
                    dev.close()
                except Exception:
                    pass
            self.device = None

    @property
    def connected(self) -> bool:
        return self.device is not None

    # -- config helpers ----------------------------------------------------
    def profile(self) -> Profile:
        return self.config.active_profile()

    def page(self) -> Page:
        pages = self.profile().pages
        self.page_index = max(0, min(self.page_index, len(pages) - 1))
        return pages[self.page_index]

    # -- rendering ---------------------------------------------------------
    def render_key(self, index: int) -> None:
        # Hold the lock for the whole body: all app-initiated device writes and
        # _gif_keys mutations must be serialized across the GUI thread (edits)
        # and the action-worker thread (page/profile switches). RLock keeps
        # render_page -> render_key reentrant.
        with self._lock:
            dev = self.device
            if not dev:
                return
            kc = self.page().keys.get(index, KeyConfig())
            is_gif = kc.icon.lower().endswith(".gif") and os.path.exists(kc.icon)
            try:
                if is_gif:
                    dev.set_key_gif(index, kc.icon)
                    self._gif_keys.add(index)
                else:
                    if index in self._gif_keys:
                        dev.clear_key_gif(index)
                        self._gif_keys.discard(index)
                    img = rendering.render_key(
                        dev.KEY_PIXEL_WIDTH, kc.label, kc.icon, kc.bg_color, kc.text_color)
                    dev.set_key_image_pil(index, img)
                self._sync_gif_loop()
            except Exception as e:
                print(f"[controller] render key {index} failed: {e}", flush=True)

    def _sync_gif_loop(self) -> None:
        dev = self.device
        if not dev:
            return
        try:
            if self._gif_keys:
                dev.start_gif_loop()
            else:
                dev.stop_gif_loop()
        except Exception as e:
            print(f"[controller] gif loop sync failed: {e}", flush=True)

    def render_page(self) -> None:
        dev = self.device
        if not dev:
            return
        with self._lock:
            # drop animations from the previous page before re-rendering
            for k in list(self._gif_keys):
                try:
                    dev.clear_key_gif(k)
                except Exception:
                    pass
            self._gif_keys.clear()
            for i in range(1, dev.KEY_COUNT + 1):
                self.render_key(i)
            self._sync_gif_loop()
            try:
                dev.refresh()
            except Exception as e:
                print(f"[controller] refresh failed: {e}", flush=True)
        if self.on_page_changed:
            self.on_page_changed()

    # -- input dispatch ----------------------------------------------------
    def _dispatch(self, action):
        """Queue an action to run on the worker thread (keeps input responsive)."""
        if action and action.type != "none":
            self._action_queue.put(action)

    def _action_worker(self):
        while True:
            action = self._action_queue.get()
            if action is None:
                return
            try:
                actions.execute(action, self)
            except Exception as e:   # never let the worker die
                print(f"[controller] action worker error: {e}", flush=True)

    def flash_key(self, index: int, pressed: bool) -> None:
        """Flash a key (brightened) on the device while pressed; restore it on
        release. Skips animated (GIF) keys, which the GIF loop keeps repainting."""
        if not self.config.glow:
            return
        with self._lock:
            dev = self.device
            if not dev or index in self._gif_keys:
                return
            kc = self.page().keys.get(index, KeyConfig())
            try:
                img = rendering.render_key(
                    dev.KEY_PIXEL_WIDTH, kc.label, kc.icon, kc.bg_color,
                    kc.text_color, pressed=pressed)
                dev.set_key_image_pil(index, img)
                dev.refresh()
            except Exception as e:
                print(f"[controller] flash key {index} failed: {e}", flush=True)

    def _key_callback(self, device, event):
        if event.event_type == EventType.BUTTON:
            index = int(event.key.value)
            pressed = event.state == 1
            if self.on_key_event:
                self.on_key_event(index, pressed)
            self.flash_key(index, pressed)
            if pressed:
                kc = self.page().keys.get(index)
                if kc:
                    self._dispatch(kc.action)
        elif event.event_type in (EventType.KNOB_ROTATE, EventType.KNOB_PRESS):
            self._knob_event(event)

    def _knob_event(self, event):
        # knob index is device-specific; map knob_1.. to 1..
        try:
            kid = int(str(event.knob_id.value).split("_")[-1])
        except Exception:
            return
        kn = self.page().knobs.get(kid)
        if not kn:
            return
        if event.event_type == EventType.KNOB_PRESS and event.state == 1:
            self._dispatch(kn.press)
        elif event.event_type == EventType.KNOB_ROTATE:
            act = kn.right if getattr(event.direction, "value", "") == "right" else kn.left
            self._dispatch(act)

    # -- ActionContext implementation -------------------------------------
    def switch_profile(self, profile_id: str) -> None:
        if self.config.profile_by_id(profile_id):
            self.config.active_profile_id = profile_id
            self.page_index = 0
            self.render_page()

    def _rotate_profile(self, step: int) -> None:
        """Scene Shift: move to the next/previous profile (wrapping)."""
        profiles = self.config.profiles
        if len(profiles) < 2:
            return
        ids = [p.id for p in profiles]
        try:
            i = ids.index(self.config.active_profile_id)
        except ValueError:
            i = 0
        self.config.active_profile_id = ids[(i + step) % len(ids)]
        self.page_index = 0
        self.render_page()

    def next_profile(self) -> None:
        self._rotate_profile(1)

    def prev_profile(self) -> None:
        self._rotate_profile(-1)

    def sleep_screen(self) -> None:
        with self._lock:
            if self.device:
                try:
                    self.device.transport.sleep()
                except Exception as e:
                    print(f"[controller] sleep failed: {e}", flush=True)

    def goto_page(self, index: int) -> None:
        self.page_index = index
        self.render_page()

    def next_page(self) -> None:
        n = len(self.profile().pages)
        self.page_index = (self.page_index + 1) % n
        self.render_page()

    def prev_page(self) -> None:
        n = len(self.profile().pages)
        self.page_index = (self.page_index - 1) % n
        self.render_page()

    def apply_brightness(self) -> None:
        if self.device:
            self.device.set_brightness(self.config.brightness)

    def set_brightness(self, percent: int) -> None:
        self.config.brightness = max(0, min(100, int(percent)))
        self.apply_brightness()

    def adjust_brightness(self, delta: int) -> None:
        self.set_brightness(self.config.brightness + delta)
