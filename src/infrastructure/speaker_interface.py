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

    Реализация:
      - OutputStream с callback, который читает из очереди и заполняет выходной буфер.
      - Если данных не хватает — воспроизводит тишину.
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

        defaults = sd.default.device
        if isinstance(defaults, tuple) and len(defaults) >= 2:
            self._initial_output_device = defaults[1]
        else:
            # если нет отдельного значения — сохраняем как есть
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

    def set_output_device(self, device: int):
        with self._lock:
            self._device = device

    def reset_to_default_device(self):
        with self._lock:
            self._device = self._initial_output_device

    def play(
        self, samples: np.ndarray, block: bool = False, timeout: Optional[float] = None
    ):
        """Добавляет блок сэмплов в очередь воспроизведения.
        Ожидается, что samples shape == (blocksize, channels) или (blocksize,) для моно.
        """
        if samples is None:
            return
        arr = np.asarray(samples, dtype=self.dtype)
        # Нормализация формы
        if arr.ndim == 1:
            arr = arr.reshape(-1, 1)
        if arr.shape[0] != self.blocksize:
            # Если длина не соответствует blocksize — пробуем разбить/дополнить
            # Для простоты: либо обрезаем, либо дополняем нулями
            if arr.shape[0] > self.blocksize:
                arr = arr[: self.blocksize, ...]
            else:
                pad = np.zeros(
                    (self.blocksize - arr.shape[0], arr.shape[1]), dtype=self.dtype
                )
                arr = np.concatenate([arr, pad], axis=0)

        try:
            if block:
                self._play_queue.put(arr, block=True, timeout=timeout)
            else:
                self._play_queue.put_nowait(arr)
        except queue.Full:
            # Выбрасываем исключение или игнорируем — в зависимости от политики
            raise AudioError("Play queue is full")

    def _output_callback(self, outdata, frames, time_info, status):
        # Заполняем outdata из очереди; если нет данных — заполняем нулями
        try:
            item = self._play_queue.get_nowait()
            # item shape (blocksize, channels)
            # Если frames != blocksize — обрезаем/заполняем
            if item.shape[0] >= frames:
                out_chunk = item[:frames]
                # Если item длиннее frames — кусочек потеряется (потом следующий блок придёт)
            else:
                # недостаточно данных — заполняем оставшуюся часть нулями
                pad = np.zeros(
                    (frames - item.shape[0], item.shape[1]), dtype=self.dtype
                )
                out_chunk = np.concatenate([item, pad], axis=0)
        except queue.Empty:
            out_chunk = np.zeros((frames, self.channels), dtype=self.dtype)
        # Приводим к форме (frames, channels)
        if out_chunk.shape[1] != self.channels:
            # Подгоняем количество каналов (повторяем или обрезаем)
            if out_chunk.shape[1] < self.channels:
                # дополняем нулями
                pad = np.zeros(
                    (out_chunk.shape[0], self.channels - out_chunk.shape[1]),
                    dtype=self.dtype,
                )
                out_chunk = np.concatenate([out_chunk, pad], axis=1)
            else:
                out_chunk = out_chunk[:, : self.channels]

        # Копируем в outdata
        outdata[:] = out_chunk

    def start_output(self):
        with self._lock:
            if self._is_playing:
                raise AudioError("Output already started")
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._thread_main, daemon=True)
            self._thread.start()
            self._is_playing = True

    def _thread_main(self):
        try:
            with self._lock:
                device = self._device
            try:
                self._stream = sd.OutputStream(
                    samplerate=self.samplerate,
                    blocksize=self.blocksize,
                    dtype=self.dtype,
                    channels=self.channels,
                    callback=self._output_callback,
                    device=device,
                )
            except Exception as e:
                self._stream = None
                raise AudioError(f"Unable to open output stream: {e}")

            self._stream.start()
            while not self._stop_event.is_set():
                time.sleep(0.05)
        except Exception as e:
            try:
                self._play_queue.put_nowait(e)
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
                self._is_playing = False

    def stop_output(self):
        with self._lock:
            if not self._is_playing:
                return
            self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
