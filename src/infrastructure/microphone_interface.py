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


class MicrophoneInterface:
    """Неблокирующий интерфейс для записи фиксированных блоков сэмплов.

    API:
      - start_recording()
      - stop_recording()
      - get_samples(blocking: bool = False, timeout: Optional[float] = None) -> Optional[np.ndarray]
      - set_input_device(device) / reset_to_default_device()

    Реализация:
      - Использует sd.InputStream с callback, который кладёт блоки в очередь.
      - Размер блока фиксирован (blocksize).
      - Поток нужен для управления жизненным циклом стрима (start/stop) без блокировки основного потока.
    """

    def __init__(
        self,
        samplerate: int = 16000,
        channels: int = 1,
        blocksize: int = 1024,
        dtype: str = "float32",
        queue_max_blocks: int = 50,
    ):
        self.samplerate = samplerate
        self.channels = channels
        self.blocksize = blocksize
        self.dtype = dtype

        # Сохранение исходных системных устройств
        defaults = sd.default.device  # (input, output) или одно значение
        self._initial_input_device = _resolve_default_device_index(defaults, 0)

        self._device = self._initial_input_device

        # Очередь для блоков сэмплов
        self._queue: "queue.Queue[np.ndarray]" = queue.Queue(maxsize=queue_max_blocks)

        # Управление состоянием
        self._stream: Optional[sd.InputStream] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._is_recording = False
        self._lock = threading.Lock()
        self._started_event = threading.Event()
        self._start_error: Optional[Exception] = None
        self._stream_samplerate: Optional[float] = None

    # ----- управление устройствами -----
    def set_input_device(self, device: int):
        """Установить устройство ввода (не перезаписывает начальное значение).
        device — индекс устройства или имя, как понимает sounddevice.
        """
        with self._lock:
            self._device = device

    def reset_to_default_device(self):
        with self._lock:
            self._device = self._initial_input_device

    # ----- callback для InputStream -----
    def _input_callback(self, indata, frames, time_info, status):
        if status:
            self._signal_error(AudioError(f"Input stream status: {status}"))
            return
        # indata shape: (frames, channels)
        # Если blocksize отличается от frames — нормализуем/сбрасываем
        arr = np.asarray(indata, dtype=self.dtype).copy()
        stream_rate = self._stream_samplerate or self.samplerate
        if stream_rate != self.samplerate and arr.size:
            arr = self._resample(arr, stream_rate, self.samplerate)
        try:
            # Кладём даже неполные блоки — потребитель должен знать размер blocksize и объединять если нужно.
            self._queue.put_nowait(arr)
        except queue.Full:
            # Дропаем самый старый блок, чтобы не расти по памяти
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(arr)
            except queue.Empty:
                pass

    def start_recording(self):
        """Запустить запись в отдельном потоке.
        Если уже запущено — выбросит AudioError.
        """
        with self._lock:
            if self._is_recording:
                raise AudioError("Recording already started")
            self._stop_event.clear()
            self._started_event.clear()
            self._start_error = None
            self._drain_queue()
            self._thread = threading.Thread(target=self._thread_main, daemon=True)
            self._thread.start()
        # Ждём запуска потока, чтобы отлавливать ошибки открытия устройства
        if not self._started_event.wait(timeout=2.0):
            self._request_stop(join_timeout=2.0)
            raise AudioError("Microphone stream did not start in time")
        if not self.is_recording:
            if isinstance(self._start_error, Exception):
                self._request_stop(join_timeout=2.0)
                raise self._start_error
            raise AudioError("Failed to initialise microphone stream")

    def _thread_main(self):
        try:
            with self._lock:
                device = self._device
            try:
                self._stream = self._open_stream(device, self.samplerate)
            except Exception as e:
                # Попытка открыть поток на другом устройстве может упасть
                self._stream = None
                # кладём информацию об ошибке в очередь как исключение
                err = AudioError(f"Unable to open input stream: {e}")
                self._start_error = err
                self._signal_error(err)
                self._started_event.set()
                return
            # start stream и ждём stop_event
            self._stream.start()
            with self._lock:
                self._is_recording = True
                self._stream_samplerate = getattr(
                    self._stream, "samplerate", self.samplerate
                )
            self._started_event.set()
            while not self._stop_event.is_set():
                time.sleep(0.05)
        except Exception as e:
            # В идеале: логгирование ошибки. Для простоты пробрасываем.
            # Чтобы не ломать основной поток, кладём объект исключения в очередь
            if self._start_error is None:
                self._start_error = e
            self._signal_error(e)
        finally:
            if self._stream is not None:
                try:
                    self._stream.stop()
                    self._stream.close()
                except Exception:
                    pass
            with self._lock:
                self._is_recording = False
                self._stream_samplerate = None
            self._started_event.set()

    def stop_recording(self):
        with self._lock:
            if not self._is_recording:
                return
            self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        self._started_event.clear()
        with self._lock:
            self._stream_samplerate = None

    def get_samples(
        self, blocking: bool = False, timeout: Optional[float] = None
    ) -> Optional[np.ndarray]:
        """Вернёт один блок сэмплов (np.ndarray) или None если нет данных.

        Если очередь содержит объект Exception — будет выброшено это исключение.
        """
        try:
            if blocking:
                item = self._queue.get(block=True, timeout=timeout)
            else:
                item = self._queue.get_nowait()
        except queue.Empty:
            return None

        # Если в очереди лежит Exception — пробросим
        if isinstance(item, Exception):
            raise item

        return item

    @property
    def is_recording(self) -> bool:
        with self._lock:
            return self._is_recording

    def _drain_queue(self) -> None:
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                return

    def _signal_error(self, err: Exception) -> None:
        try:
            self._queue.put_nowait(err)
        except queue.Full:
            # очередь забита данными, важно донести ошибку
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(err)
            except queue.Empty:
                pass

    def _resolve_default_samplerate(self, device) -> Optional[float]:
        try:
            info = sd.query_devices(device, "input")
            rate = info.get("default_samplerate")
            return float(rate) if rate else None
        except Exception:
            return None

    def _open_stream(self, device, samplerate: float) -> sd.InputStream:
        try:
            stream = sd.InputStream(
                samplerate=samplerate,
                blocksize=self.blocksize,
                dtype=self.dtype,
                channels=self.channels,
                callback=self._input_callback,
                device=device,
            )
            return stream
        except sd.PortAudioError as exc:
            if "Invalid sample rate" in str(exc):
                fallback = self._resolve_default_samplerate(device)
                if fallback and fallback != samplerate:
                    stream = sd.InputStream(
                        samplerate=fallback,
                        blocksize=self.blocksize,
                        dtype=self.dtype,
                        channels=self.channels,
                        callback=self._input_callback,
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

    def _request_stop(self, join_timeout: float) -> None:
        with self._lock:
            self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=join_timeout)
        self._started_event.clear()
        with self._lock:
            self._stream_samplerate = None
