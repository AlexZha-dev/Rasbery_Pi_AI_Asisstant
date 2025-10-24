import queue
import threading
import time
from typing import Optional

import numpy as np
import sounddevice as sd

from exceptions.audio_exceptions import AudioError


class SpeakerInterface:
    """Неблокирующий интерфейс для воспроизведения фиксированных блоков сэмплов.

    API:
      - play(samples)         # добавляет сэмплы в очередь
      - start_output()
      - stop_output()
      - set_output_device(device) / reset_to_default_device()
    """

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
        if isinstance(defaults, tuple) and len(defaults) >= 2:
            self._initial_output_device = defaults[1]
        else:
            self._initial_output_device = defaults

        self._device = self._initial_output_device
        self._play_queue: "queue.Queue[np.ndarray]" = queue.Queue(
            maxsize=max_queue_blocks
        )

        self._stream: Optional[sd.OutputStream] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._is_playing = False
        self._lock = threading.Lock()
        self._started_event = threading.Event()
        self._start_error: Optional[Exception] = None
        self._stream_samplerate: Optional[float] = None

    def set_output_device(self, device: int):
        with self._lock:
            self._device = device

    def reset_to_default_device(self):
        with self._lock:
            self._device = self._initial_output_device

    def play(self, samples: np.ndarray):
        """Ставим фрейм в очередь воспроизведения. Асинхронное ожидание места реализуется через адаптер."""
        if samples is None:
            return
        if not self.is_playing:
            raise AudioError("Output stream is not started")
        arr = np.asarray(samples, dtype=self.dtype)
        if arr.ndim == 1:
            arr = arr.reshape(-1, 1)
        stream_rate = self._stream_samplerate or self.samplerate
        if stream_rate != self.samplerate and arr.size:
            arr = self._resample(arr, self.samplerate, stream_rate)
        if arr.shape[0] != self.blocksize:
            if arr.shape[0] > self.blocksize:
                arr = arr[: self.blocksize, ...]
            else:
                pad = np.zeros(
                    (self.blocksize - arr.shape[0], arr.shape[1]), dtype=self.dtype
                )
                arr = np.concatenate([arr, pad], axis=0)
        # Блокирующий put в очереди (не дропаем)
        self._play_queue.put(arr, block=True)
        self._last_play_activity = time.monotonic()

    def _output_callback(self, outdata, frames, time_info, status):
        try:
            item = self._play_queue.get_nowait()
            if item.shape[0] >= frames:
                out_chunk = item[:frames]
            else:
                pad = np.zeros(
                    (frames - item.shape[0], item.shape[1]), dtype=self.dtype
                )
                out_chunk = np.concatenate([item, pad], axis=0)
        except queue.Empty:
            out_chunk = np.zeros((frames, self.channels), dtype=self.dtype)

        if out_chunk.shape[1] != self.channels:
            if out_chunk.shape[1] < self.channels:
                pad = np.zeros(
                    (out_chunk.shape[0], self.channels - out_chunk.shape[1]),
                    dtype=self.dtype,
                )
                out_chunk = np.concatenate([out_chunk, pad], axis=1)
            else:
                out_chunk = out_chunk[:, : self.channels]

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
            except Exception as e:
                self._stream = None
                self._start_error = AudioError(f"Unable to open output stream: {e}")
                self._started_event.set()
                return

            self._stream.start()
            with self._lock:
                self._is_playing = True
                self._stream_samplerate = getattr(
                    self._stream, "samplerate", self.samplerate
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
        while True:
            try:
                self._play_queue.get_nowait()
            except queue.Empty:
                return

    def pending_blocks(self) -> int:
        """Approximate number of audio blocks pending in output queue."""
        return self._play_queue.qsize()

    def had_recent_activity(self, window: float = 0.75) -> bool:
        """Return True if play() was invoked within the last *window* seconds."""
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

    def _resolve_default_samplerate(self, device) -> Optional[float]:
        try:
            info = sd.query_devices(device, "output")
            rate = info.get("default_samplerate")
            return float(rate) if rate else None
        except Exception:
            return None

    def _open_stream(self, device, samplerate: float) -> sd.OutputStream:
        try:
            stream = sd.OutputStream(
                samplerate=samplerate,
                blocksize=self.blocksize,
                dtype=self.dtype,
                channels=self.channels,
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
                        channels=self.channels,
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
        # quick linear interpolation per channel
        orig_positions = np.linspace(0.0, 1.0, arr.shape[0], endpoint=False)
        new_positions = np.linspace(0.0, 1.0, dst_len, endpoint=False)
        resampled = np.empty((dst_len, arr.shape[1]), dtype=np.float32)
        for idx in range(arr.shape[1]):
            resampled[:, idx] = np.interp(new_positions, orig_positions, arr[:, idx])
        return resampled.astype(self.dtype)
