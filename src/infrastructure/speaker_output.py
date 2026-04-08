import queue
import threading
import time
from typing import Optional

import numpy as np
import sounddevice as sd

from exceptions.audio_exceptions import AudioError


def _resolve_default_device_index(defaults, position: int):
    if defaults is None:
        return None
    candidate = defaults
    try:
        if not isinstance(defaults, (str, bytes)) and len(defaults) > position:
            candidate = defaults[position]
    except TypeError:
        candidate = defaults
    return candidate


class SpeakerInterface:
    """Non-blocking speaker output that preserves contiguous PCM playback."""

    def __init__(
        self,
        samplerate: int = 16000,
        channels: int = 1,
        blocksize: int = 1024,
        dtype: str = "float32",
        max_queue_blocks: int = 50,
    ):
        self.samplerate = samplerate
        self.channels = channels
        self.blocksize = blocksize
        self.dtype = dtype
        self._last_play_activity: float = 0.0

        defaults = sd.default.device
        self._initial_output_device = _resolve_default_device_index(defaults, 1)

        self._device = self._initial_output_device
        self._play_queue: "queue.Queue[np.ndarray]" = queue.Queue(
            maxsize=max_queue_blocks
        )

        self._stream: Optional[sd.OutputStream] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._is_playing = False
        self._lock = threading.Lock()
        self._queue_state_lock = threading.Lock()
        self._started_event = threading.Event()
        self._start_error: Optional[Exception] = None
        self._stream_samplerate: Optional[float] = None
        self._stream_channels: Optional[int] = None
        self._carryover: Optional[np.ndarray] = None
        self._pending_frames: int = 0

    def set_output_device(self, device: int):
        with self._lock:
            self._device = device

    def reset_to_default_device(self):
        with self._lock:
            self._device = self._initial_output_device

    def play(self, samples: np.ndarray):
        if samples is None:
            return
        if not self.is_playing:
            raise AudioError("Output stream is not started")
        arr = self._normalize_chunk(samples)
        if arr.size == 0:
            return
        with self._queue_state_lock:
            self._pending_frames += int(arr.shape[0])
        self._play_queue.put(arr, block=True)
        self._last_play_activity = time.monotonic()

    def _output_callback(self, outdata, frames, time_info, status):
        stream_channels = self._stream_channels or self.channels
        out_chunk = np.zeros((frames, stream_channels), dtype=self.dtype)
        filled = 0

        while filled < frames:
            item = self._carryover
            if item is None or item.size == 0:
                try:
                    item = self._play_queue.get_nowait()
                except queue.Empty:
                    self._carryover = None
                    break

            if item.ndim == 1:
                item = item.reshape(-1, 1)
            available = int(item.shape[0])
            if available <= 0:
                self._carryover = None
                continue

            take = min(frames - filled, available)
            out_chunk[filled : filled + take] = item[:take]
            filled += take
            with self._queue_state_lock:
                self._pending_frames = max(0, self._pending_frames - take)

            if take < available:
                self._carryover = item[take:]
            else:
                self._carryover = None

        outdata[:] = out_chunk

    def start_output(self):
        with self._lock:
            if self._is_playing:
                raise AudioError("Output already started")
            self._stop_event.clear()
            self._started_event.clear()
            self._start_error = None
            self._drain_queue()
            self._thread = threading.Thread(target=self._thread_main, daemon=True)
            self._thread.start()
        if not self._started_event.wait(timeout=2.0):
            self._request_stop(join_timeout=2.0)
            raise AudioError("Speaker stream did not start in time")
        if not self.is_playing:
            if isinstance(self._start_error, Exception):
                self._request_stop(join_timeout=2.0)
                raise self._start_error
            raise AudioError("Failed to initialise speaker stream")

    def _thread_main(self):
        try:
            with self._lock:
                device = self._device
            try:
                self._stream = self._open_stream(device, self.samplerate)
            except Exception as exc:
                self._stream = None
                self._start_error = AudioError(f"Unable to open output stream: {exc}")
                self._started_event.set()
                return

            self._stream.start()
            with self._lock:
                self._is_playing = True
                self._stream_samplerate = getattr(
                    self._stream, "samplerate", self.samplerate
                )
                self._stream_channels = int(
                    getattr(self._stream, "channels", self.channels) or self.channels
                )
            self._started_event.set()
            while not self._stop_event.is_set():
                time.sleep(0.05)
        finally:
            if self._stream is not None:
                try:
                    self._stream.stop()
                    self._stream.close()
                except Exception:
                    pass
            with self._lock:
                self._is_playing = False
            if self._start_error is None and not self._stop_event.is_set():
                self._start_error = AudioError("Speaker stream stopped unexpectedly")
            self._started_event.set()
            with self._lock:
                self._stream_samplerate = None
                self._stream_channels = None

    def stop_output(self):
        with self._lock:
            if not self._is_playing:
                return
            self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        self._started_event.clear()
        self._drain_queue()

    @property
    def is_playing(self) -> bool:
        with self._lock:
            return self._is_playing

    def _drain_queue(self) -> None:
        self._carryover = None
        with self._queue_state_lock:
            self._pending_frames = 0
        while True:
            try:
                self._play_queue.get_nowait()
            except queue.Empty:
                return

    def pending_blocks(self) -> int:
        carry = 1 if self._carryover is not None and self._carryover.size else 0
        return self._play_queue.qsize() + carry

    def pending_frames(self) -> int:
        with self._queue_state_lock:
            return max(0, int(self._pending_frames))

    def had_recent_activity(self, window: float = 0.75) -> bool:
        if self._last_play_activity == 0.0:
            return False
        return (time.monotonic() - self._last_play_activity) <= max(window, 0.0)

    def _request_stop(self, join_timeout: float) -> None:
        with self._lock:
            self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=join_timeout)
        self._started_event.clear()
        self._drain_queue()
        with self._lock:
            self._stream_samplerate = None
            self._stream_channels = None

    def _resolve_default_samplerate(self, device) -> Optional[float]:
        try:
            info = sd.query_devices(device, "output")
            rate = info.get("default_samplerate")
            return float(rate) if rate else None
        except Exception:
            return None

    def _resolve_output_channels(self, device) -> int:
        target = max(1, int(self.channels or 1))
        try:
            info = sd.query_devices(device, "output")
            max_output_channels = int(info.get("max_output_channels") or 0)
        except Exception:
            return target
        if max_output_channels <= 0:
            return target
        if target == 1 and max_output_channels >= 2:
            return 2
        return max(1, min(target, max_output_channels))

    def _open_stream(self, device, samplerate: float) -> sd.OutputStream:
        channels = self._resolve_output_channels(device)
        try:
            stream = sd.OutputStream(
                samplerate=samplerate,
                blocksize=self.blocksize,
                dtype=self.dtype,
                channels=channels,
                callback=self._output_callback,
                device=device,
            )
            return stream
        except sd.PortAudioError as exc:
            if "Invalid sample rate" in str(exc):
                fallback = self._resolve_default_samplerate(device)
                if fallback and fallback != samplerate:
                    stream = sd.OutputStream(
                        samplerate=fallback,
                        blocksize=self.blocksize,
                        dtype=self.dtype,
                        channels=channels,
                        callback=self._output_callback,
                        device=device,
                    )
                    return stream
            raise

    def _resample(
        self, arr: np.ndarray, src_rate: float, dst_rate: float
    ) -> np.ndarray:
        if src_rate == dst_rate:
            return arr
        ratio = dst_rate / src_rate
        dst_len = max(1, int(round(arr.shape[0] * ratio)))
        orig_positions = np.linspace(0.0, 1.0, arr.shape[0], endpoint=False)
        new_positions = np.linspace(0.0, 1.0, dst_len, endpoint=False)
        resampled = np.empty((dst_len, arr.shape[1]), dtype=np.float32)
        for idx in range(arr.shape[1]):
            resampled[:, idx] = np.interp(new_positions, orig_positions, arr[:, idx])
        return resampled.astype(self.dtype)

    def _normalize_chunk(self, samples: np.ndarray) -> np.ndarray:
        arr = np.asarray(samples, dtype=self.dtype)
        if arr.ndim == 1:
            arr = arr.reshape(-1, 1)
        stream_rate = self._stream_samplerate or self.samplerate
        stream_channels = self._stream_channels or self.channels
        if stream_rate != self.samplerate and arr.size:
            arr = self._resample(arr, self.samplerate, stream_rate)
        if arr.shape[1] != stream_channels:
            if arr.shape[1] == 1 and stream_channels > 1:
                arr = np.repeat(arr, stream_channels, axis=1)
            elif arr.shape[1] < stream_channels:
                pad = np.zeros(
                    (arr.shape[0], stream_channels - arr.shape[1]),
                    dtype=self.dtype,
                )
                arr = np.concatenate([arr, pad], axis=1)
            else:
                arr = arr[:, :stream_channels]
        return arr
