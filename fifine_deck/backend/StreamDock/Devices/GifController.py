import copy
import io
import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Iterable

from PIL import Image, ImageSequence

from ..ImageHelpers.PILHelper import to_native_key_format, to_native_touchscreen_format
from ..InputTypes import ButtonKey


@dataclass
class _GifStreamStatus:
    frames: list[bytes]
    delays: list[int]
    current_frame: int = 0
    accumulated_time: float = 0.0
    width: int = 0
    height: int = 0
    x: int = 0
    y: int = 0
    fb_layer: int = 0x00
    stream_type: str = "frames"
    video_capture: Any = None
    video_cv2: Any = None
    video_delay: int = 0
    video_image_format: dict[str, Any] | None = None


class GifController:
    """Controls animated key and background GIF playback for a StreamDock device."""

    _BACKGROUND_INDEX = 0
    _DEFAULT_DELAY_MS = 100
    # LOCAL PATCH: decoded-GIF cache size. Must comfortably exceed the number of
    # keys a device has, or a page animating most of them thrashes: entries are
    # evicted and re-decoded on every page render — wasted CPU, and each key
    # visibly flicks from static back to animated on every page switch.
    #
    # First set to 6 on the assumption that "frame data is large". That was
    # wrong: frames are downscaled to the key's size (112x112) and JPEG encoded,
    # so a 90-frame animation measures 73 KB, not megabytes. The 293V3 has 15
    # keys, so 6 sat BELOW the number of keys it had to serve. At 32 the worst
    # case is about 2.3 MB, and being smaller buys nothing.
    _DECODE_CACHE_MAX = 32

    def __init__(self, device):
        self._device = device
        self._gif_map: dict[int, _GifStreamStatus] = {}
        self._lock = threading.RLock()
        self._wake_event = threading.Event()
        self._running = True
        self._loop_enabled = False
        # LOCAL PATCH: see _read_gif. Its own lock, deliberately not self._lock
        # — the work loop holds that one while collecting frames, and decoding
        # must never wait on playback.
        self._decode_cache: dict = {}
        self._decode_cache_lock = threading.Lock()
        self._thread = threading.Thread(target=self._gif_work_loop, daemon=True)
        self._thread.start()

    def set_key_gif(self, path, key):
        logical_key, hardware_key = self._get_key_values(key)
        if hardware_key is None:
            return -1

        image_format = self._key_image_format(logical_key, hardware_key)
        frames, delays, _, _ = self._read_gif(path, image_format, allow_png=True)
        if not frames:
            return -1

        with self._lock:
            self._replace_stream(hardware_key, self._create_status(frames, delays))
        self._wake_event.set()
        return 0

    def set_key_gif_stream(self, frames: Iterable[bytes], delays: Iterable[int], key):
        _, hardware_key = self._get_key_values(key)
        if hardware_key is None:
            return -1

        frame_list = list(frames)
        delay_list = self._normalize_delays(list(delays), len(frame_list))
        if not frame_list:
            print("Error: GIF stream has no frames.")
            return -1

        with self._lock:
            self._replace_stream(hardware_key, self._create_status(frame_list, delay_list))
        self._wake_event.set()
        return 0

    def clear_key_gif(self, key):
        _, hardware_key = self._get_key_values(key)
        if hardware_key is None:
            return -1
        with self._lock:
            self._remove_stream(hardware_key)
        return 0

    def set_background_gif(self, path, x=0, y=0, fb_layer=0x00):
        if not self._device.feature_option.supportBackgroundGif:
            print("Error: Background GIF is only supported on N4Pro, XL, and M3.")
            return -1

        image_format = self._device.touchscreen_image_format()
        frames, delays, width, height = self._read_gif(path, image_format, allow_png=False)
        if not frames:
            return -1
        if not self._background_frame_fits(width, height, x, y):
            print("Error: Background GIF frame exceeds touchscreen bounds.")
            return -1

        with self._lock:
            self._replace_stream(
                self._BACKGROUND_INDEX,
                self._create_status(
                    frames=frames,
                    delays=delays,
                    width=width,
                    height=height,
                    x=x,
                    y=y,
                    fb_layer=fb_layer,
                ),
            )
        self._wake_event.set()
        return 0

    def set_background_mp4(self, path, x=0, y=0, fb_layer=0x00, fps=None):
        if not self._device.feature_option.supportBackgroundGif:
            print("Error: Background MP4 is only supported on N4Pro, XL, and M3.")
            return -1

        cv2 = self._load_cv2()
        if cv2 is None:
            return -1

        try:
            capture = cv2.VideoCapture(path)
        except Exception as e:
            print(f"Error: Failed to open MP4 '{path}': {e}")
            return -1

        if not capture.isOpened():
            print(f"Error: Failed to open MP4 '{path}'.")
            capture.release()
            return -1

        image_format = self._device.touchscreen_image_format()
        width, height = image_format["size"]
        if not self._background_frame_fits(width, height, x, y):
            print("Error: Background MP4 frame exceeds touchscreen bounds.")
            capture.release()
            return -1

        video_fps = self._resolve_video_fps(cv2, capture, fps)
        status = self._create_video_status(
            capture=capture,
            cv2=cv2,
            image_format=image_format,
            delay_ms=max(1, int(1000 / video_fps)),
            width=width,
            height=height,
            x=x,
            y=y,
            fb_layer=fb_layer,
        )

        with self._lock:
            self._replace_stream(self._BACKGROUND_INDEX, status)
        self._wake_event.set()
        return 0

    def set_background_gif_stream(
        self, frames: Iterable[bytes], delays: Iterable[int], x=0, y=0, fb_layer=0x00
    ):
        if not self._device.feature_option.supportBackgroundGif:
            print("Error: Background GIF is only supported on N4Pro, XL, and M3.")
            return -1

        frame_list = list(frames)
        delay_list = self._normalize_delays(list(delays), len(frame_list))
        if not frame_list:
            print("Error: GIF stream has no frames.")
            return -1

        width, height = self._device.touchscreen_image_format()["size"]
        if not self._background_frame_fits(width, height, x, y):
            print("Error: Background GIF frame exceeds touchscreen bounds.")
            return -1

        with self._lock:
            self._replace_stream(
                self._BACKGROUND_INDEX,
                self._create_status(
                    frames=frame_list,
                    delays=delay_list,
                    width=width,
                    height=height,
                    x=x,
                    y=y,
                    fb_layer=fb_layer,
                ),
            )
        self._wake_event.set()
        return 0

    def clear_background_gif(self, position=0x03):
        if not self._device.feature_option.supportBackgroundGif:
            print("Error: Background GIF is only supported on N4Pro, XL, and M3.")
            return -1
        return self.clear_background_animation(position)

    def clear_background_animation(self, position=0x03):
        if not self._device.feature_option.supportBackgroundGif:
            print("Error: Background animation is only supported on N4Pro, XL, and M3.")
            return -1
        with self._lock:
            self._remove_stream(self._BACKGROUND_INDEX)
        self._device.transport.clear_background_frame_stream(position)
        return 0

    def start_gif_loop(self):
        return self.start_animation_loop()

    def stop_gif_loop(self):
        return self.stop_animation_loop()

    def gif_loop_status(self):
        return self.animation_loop_status()

    def start_animation_loop(self):
        self._loop_enabled = True
        self._wake_event.set()
        return 0

    def stop_animation_loop(self):
        self._loop_enabled = False
        self._wake_event.set()
        return 0

    def animation_loop_status(self):
        return self._loop_enabled

    def close(self, timeout=2.0):
        """Stop the animation worker.

        Returns True once the worker has actually exited, False when the join
        timed out and it is still running. The caller MUST honour a False: this
        worker writes straight to the device transport outside every lock (see
        the tail of _gif_work_loop), so destroying the transport while it is
        still in there frees the handle underneath a live native write — a
        use-after-free in C, which kills the process instead of disconnecting
        cleanly. This return value used to not exist, and StreamDock.close()
        went on to free the transport regardless.
        """
        self._running = False
        self._loop_enabled = False
        self._wake_event.set()
        if self._thread.is_alive():
            self._thread.join(timeout=timeout)
            if self._thread.is_alive():
                # Still inside a native write; libusb blocks until its transfer
                # times out. Leave _gif_map alone — _release_status frees buffers
                # the worker may still be reading from.
                print("[WARNING] GIF worker did not exit in time", flush=True)
                return False
        with self._lock:
            for status in self._gif_map.values():
                self._release_status(status)
            self._gif_map.clear()
        return True

    def _get_key_values(self, key):
        try:
            logical_key = ButtonKey(key) if isinstance(key, int) else key
            return logical_key, self._device.get_image_key(logical_key)
        except Exception as e:
            print(f"Error: {e}")
            return None, None

    def _key_image_format(self, logical_key, hardware_key):
        if (
            hasattr(self._device, "secondscreen_image_format")
            and (
                hardware_key in range(16, 19)
                or (logical_key is not None and logical_key.value in range(11, 15))
            )
        ):
            return self._device.secondscreen_image_format()
        return self._device.key_image_format()

    def _read_gif(self, path, image_format, allow_png):
        """Decode a GIF to per-frame device-native bytes, with a small cache.

        LOCAL PATCH. This decodes AND re-encodes every frame, and it used to run
        in full on every render — so each page switch, profile switch, folder
        enter/exit and reconnect paid the whole cost again for the same
        unchanged file. It runs on the Qt thread, inside the controller lock, so
        that time is a frozen window AND a stall for the SDK reader thread
        waiting on the same lock. Measured previously at ~0.25 s for a 90-frame
        400x400 GIF, scaling with size and length and multiplying per animated
        key on the page.

        Frames are immutable bytes and nothing mutates the lists in place
        (_release_status only touches video_capture), so entries are safe to
        share between keys; the lists are copied out anyway so a caller cannot
        disturb the cached originals.

        Keyed on the file's identity AND mtime/size, so editing an icon in place
        still takes effect.
        """
        cache_key = self._decode_cache_key(path, image_format, allow_png)

        if cache_key is not None:
            with self._decode_cache_lock:
                hit = self._decode_cache.get(cache_key)
            if hit is not None:
                frames, delays, width, height = hit
                return list(frames), list(delays), width, height

        result = self._read_gif_uncached(path, image_format, allow_png)

        frames = result[0]
        if cache_key is not None and frames:
            with self._decode_cache_lock:
                self._decode_cache[cache_key] = result
                # Bounded: a deck page holds at most a handful of animated keys,
                # and frame data is large. Drop the oldest beyond the cap.
                while len(self._decode_cache) > self._DECODE_CACHE_MAX:
                    self._decode_cache.pop(next(iter(self._decode_cache)))
        f, d, w, h = result
        return list(f), list(d), w, h

    @staticmethod
    def _decode_cache_key(path, image_format, allow_png):
        """Cache identity for one decode, or None when the file cannot be
        stat'd (in which case the result is used but not cached)."""
        try:
            st = os.stat(path)
        except OSError:
            return None
        return (os.path.abspath(path), st.st_mtime_ns, st.st_size,
                bool(allow_png), repr(image_format))

    def is_key_gif_cached(self, path, key) -> bool:
        """True if set_key_gif(path, key) would be served from the cache.

        LOCAL PATCH. Lets the caller decide whether a decode is about to cost
        ~244 ms on its thread, and hand it to a worker instead.
        """
        logical_key, hardware_key = self._get_key_values(key)
        if hardware_key is None:
            return False
        ck = self._decode_cache_key(
            path, self._key_image_format(logical_key, hardware_key), True)
        if ck is None:
            return False
        with self._decode_cache_lock:
            return ck in self._decode_cache

    def warm_key_gif(self, path, key) -> bool:
        """Decode into the cache WITHOUT touching the device.

        LOCAL PATCH. Safe to call from any thread: it takes only the decode
        cache lock, never the playback lock and never the transport. Returns
        whether the file decoded to anything.
        """
        logical_key, hardware_key = self._get_key_values(key)
        if hardware_key is None:
            return False
        fmt = self._key_image_format(logical_key, hardware_key)
        frames, _, _, _ = self._read_gif(path, fmt, allow_png=True)
        return bool(frames)

    def _read_gif_uncached(self, path, image_format, allow_png):
        try:
            image = Image.open(path)
        except Exception as e:
            print(f"Error: Failed to open GIF '{path}': {e}")
            return [], [], 0, 0

        frames = []
        delays = []
        for frame in ImageSequence.Iterator(image):
            frame_image = frame.convert("RGBA")
            delay_ms = int(frame.info.get("duration") or self._DEFAULT_DELAY_MS)
            delay_ms = max(1, delay_ms)
            encoded = self._encode_frame(frame_image, image_format, allow_png)
            frames.append(encoded)
            delays.append(delay_ms)

        width, height = image_format["size"]
        return frames, self._normalize_delays(delays, len(frames)), width, height

    def _encode_frame(self, frame, image_format, allow_png):
        native_format = copy.deepcopy(image_format)
        native_format["format"] = (
            "PNG"
            if allow_png and native_format["format"].upper() == "PNG"
            else "JPEG"
        )

        if native_format["format"] == "PNG":
            native_image = to_native_key_format(self._FormatDock(native_format), frame)
        else:
            if native_format["size"] == self._device.touchscreen_image_format()["size"]:
                native_image = to_native_touchscreen_format(
                    self._FormatDock(native_format), frame
                )
            else:
                native_image = to_native_key_format(self._FormatDock(native_format), frame)
            native_image = self._to_rgb_with_background(native_image)

        buffer = io.BytesIO()
        if native_format["format"] == "PNG":
            native_image.save(buffer, "PNG")
        else:
            native_image.save(buffer, "JPEG", quality=95)
        return buffer.getvalue()

    def _normalize_delays(self, delays, frame_count):
        normalized = [max(1, int(delay or self._DEFAULT_DELAY_MS)) for delay in delays]
        if frame_count and len(normalized) < frame_count:
            normalized.extend([self._DEFAULT_DELAY_MS] * (frame_count - len(normalized)))
        return normalized[:frame_count]

    def _create_status(self, frames, delays, **kwargs):
        current_frame = len(frames) - 1 if frames else 0
        accumulated_time = delays[current_frame] if frames and delays else 0.0
        return _GifStreamStatus(
            frames=frames,
            delays=delays,
            current_frame=current_frame,
            accumulated_time=accumulated_time,
            **kwargs,
        )

    def _create_video_status(
        self, capture, cv2, image_format, delay_ms, width, height, x, y, fb_layer
    ):
        return _GifStreamStatus(
            frames=[],
            delays=[],
            accumulated_time=delay_ms,
            width=width,
            height=height,
            x=x,
            y=y,
            fb_layer=fb_layer,
            stream_type="video",
            video_capture=capture,
            video_cv2=cv2,
            video_delay=delay_ms,
            video_image_format=copy.deepcopy(image_format),
        )

    def _replace_stream(self, index, status):
        self._release_status(self._gif_map.get(index))
        self._gif_map[index] = status

    def _remove_stream(self, index):
        self._release_status(self._gif_map.pop(index, None))

    @staticmethod
    def _release_status(status):
        if status is None or status.video_capture is None:
            return
        try:
            status.video_capture.release()
        except Exception:
            pass
        status.video_capture = None

    @staticmethod
    def _load_cv2():
        try:
            import cv2
        except ImportError:
            print(
                "Error: MP4 background support requires opencv-python. "
                "Install it with `pip install opencv-python`."
            )
            return None
        return cv2

    @staticmethod
    def _resolve_video_fps(cv2, capture, fps):
        if fps is not None:
            try:
                fps_value = float(fps)
                if fps_value > 0:
                    return fps_value
            except (TypeError, ValueError):
                pass
            print("Warning: Invalid fps value; using video FPS.")

        try:
            fps_value = float(capture.get(cv2.CAP_PROP_FPS))
            if fps_value > 0:
                return fps_value
        except Exception:
            pass
        return 30.0

    def _read_video_frame(self, status):
        if status.video_capture is None or status.video_cv2 is None:
            return None

        ok, frame = status.video_capture.read()
        if not ok:
            status.video_capture.set(status.video_cv2.CAP_PROP_POS_FRAMES, 0)
            ok, frame = status.video_capture.read()
            if not ok:
                return None

        frame = status.video_cv2.cvtColor(frame, status.video_cv2.COLOR_BGR2RGB)
        image = Image.fromarray(frame)
        return self._encode_frame(image, status.video_image_format, allow_png=False)

    def _background_frame_fits(self, width, height, x, y):
        bg_width, bg_height = self._device.touchscreen_image_format()["size"]
        return x >= 0 and y >= 0 and x + width <= bg_width and y + height <= bg_height

    @staticmethod
    def _to_rgb_with_background(image, background="black"):
        rgba_image = image.convert("RGBA")
        background_image = Image.new("RGBA", rgba_image.size, background)
        return Image.alpha_composite(background_image, rgba_image).convert("RGB")

    def _gif_work_loop(self):
        last_time = time.monotonic()
        while self._running:
            if not self._loop_enabled:
                self._wake_event.wait(0.1)
                self._wake_event.clear()
                last_time = time.monotonic()
                continue

            now = time.monotonic()
            elapsed_ms = (now - last_time) * 1000.0
            last_time = now
            frames_to_update = []

            with self._lock:
                for index, gif in self._gif_map.items():
                    if gif.stream_type == "video":
                        if gif.video_delay <= 0:
                            continue
                        gif.accumulated_time += elapsed_ms
                        if gif.accumulated_time >= gif.video_delay:
                            frame_data = self._read_video_frame(gif)
                            if frame_data is not None:
                                gif.accumulated_time %= gif.video_delay
                                frames_to_update.append(
                                    (
                                        index,
                                        frame_data,
                                        gif.width,
                                        gif.height,
                                        gif.x,
                                        gif.y,
                                        gif.fb_layer,
                                    )
                                )
                        continue

                    if not gif.frames or not gif.delays:
                        continue
                    gif.accumulated_time += elapsed_ms
                    current_delay = gif.delays[gif.current_frame]
                    if gif.accumulated_time >= current_delay:
                        gif.current_frame = (gif.current_frame + 1) % len(gif.frames)
                        gif.accumulated_time -= current_delay
                        frames_to_update.append(
                            (
                                index,
                                gif.frames[gif.current_frame],
                                gif.width,
                                gif.height,
                                gif.x,
                                gif.y,
                                gif.fb_layer,
                            )
                        )

            if frames_to_update:
                for index, frame_data, width, height, x, y, fb_layer in frames_to_update:
                    if index == self._BACKGROUND_INDEX:
                        self._device.transport.set_background_frame_stream(
                            frame_data, width, height, x, y, fb_layer
                        )
                    else:
                        self._device.transport.set_key_image_stream(frame_data, index)
                self._device.refresh()

            time.sleep(0.003)

    class _FormatDock:
        def __init__(self, image_format):
            self._image_format = image_format

        def key_image_format(self):
            return copy.deepcopy(self._image_format)

        def touchscreen_image_format(self):
            return copy.deepcopy(self._image_format)
