import queue
import threading
import time
from typing import Optional

import numpy as np
import sounddevice as sd

from exceptions.audio_exceptions import AudioError


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
    ):
        self.samplerate = samplerate
        self.channels = channels
        self.blocksize = blocksize
        self.dtype = dtype

        # Сохранение исходных системных устройств
        defaults = sd.default.device  # (input, output) или одно значение
        if isinstance(defaults, tuple) and len(defaults) >= 1:
            self._initial_input_device = defaults[0]
        else:
            self._initial_input_device = defaults

        self._device = self._initial_input_device

        # Очередь для блоков сэмплов
        self._queue: "queue.Queue[np.ndarray]" = queue.Queue()

        # Управление состоянием
        self._stream: Optional[sd.InputStream] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._is_recording = False
        self._lock = threading.Lock()

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
            # Не бросаем ошибку из callback — просто логируем в очередь
            # Пользователь может читать и решить что делать
            try:
                # помещаем статус как 0-length массив с метаданными не делаем; просто игнорируем
                pass
            except Exception:
                pass
        # indata shape: (frames, channels)
        # Если blocksize отличается от frames — нормализуем/сбрасываем
        if frames != self.blocksize:
            # Попытка ресемплирования внутри callback — плохая идея.
            # Вместо этого мы просто буферизуем до blocksize в основном потоке.
            # Здесь кладём то, что пришло (возможно неполный блок).
            arr = np.asarray(indata, dtype=self.dtype).copy()
        else:
            arr = np.asarray(indata, dtype=self.dtype).copy()
        try:
            # Кладём даже неполные блоки — потребитель должен знать размер blocksize и объединять если нужно.
            self._queue.put_nowait(arr)
        except queue.Full:
            # Пропускаем если очередь забита
            pass

    def start_recording(self):
        """Запустить запись в отдельном потоке.
        Если уже запущено — выбросит AudioError.
        """
        with self._lock:
            if self._is_recording:
                raise AudioError("Recording already started")
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._thread_main, daemon=True)
            self._thread.start()
            self._is_recording = True

    def _thread_main(self):
        try:
            with self._lock:
                device = self._device
            try:
                self._stream = sd.InputStream(
                    samplerate=self.samplerate,
                    blocksize=self.blocksize,
                    dtype=self.dtype,
                    channels=self.channels,
                    callback=self._input_callback,
                    device=device,
                )
            except Exception as e:
                # Попытка открыть поток на другом устройстве может упасть
                self._stream = None
                # кладём информацию об ошибке в очередь как исключение
                raise AudioError(f"Unable to open input stream: {e}")

            # start stream и ждём stop_event
            self._stream.start()
            while not self._stop_event.is_set():
                time.sleep(0.05)
        except Exception as e:
            # В идеале: логгирование ошибки. Для простоты пробрасываем.
            # Чтобы не ломать основной поток, кладём объект исключения в очередь
            try:
                self._queue.put_nowait(e)
            except Exception:
                pass
        finally:
            if self._stream is not None:
                try:
                    self._stream.stop()
                    self._stream.close()
                except Exception:
                    pass
            with self._lock:
                self._is_recording = False

    def stop_recording(self):
        with self._lock:
            if not self._is_recording:
                return
            self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

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
