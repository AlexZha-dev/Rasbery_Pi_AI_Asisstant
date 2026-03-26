import asyncio
import queue
import threading
from typing import Optional

import numpy as np


class MicrophoneAsyncAdapter:
    """Async adapter for microphone frame polling."""

    def __init__(self, microphone):
        self.mic = microphone

    async def get_samples(self):
        """Poll microphone queue and return one frame when available."""
        samples = self.mic.get_samples(blocking=False)
        if samples is not None:
            return samples
        await asyncio.sleep(0.01)
        return None


class SpeakerAsyncAdapter:
    """Async adapter for speaker playback with one background worker thread.

    Instead of creating an executor task per frame, this adapter keeps one
    worker that forwards frames to `SpeakerInterface.play()`.
    """

    def __init__(
        self,
        speaker,
        *,
        max_pending_blocks: int = 64,
        poll_interval: float = 0.005,
    ):
        self.spk = speaker
        self._poll_interval = max(0.001, float(poll_interval))
        self._queue: "queue.Queue[object]" = queue.Queue(
            maxsize=max(1, int(max_pending_blocks))
        )
        self._worker: Optional[threading.Thread] = None
        self._worker_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._pending_lock = threading.Lock()
        self._pending_blocks = 0
        self._last_error: Optional[Exception] = None
        self._stop_sentinel = object()

    def _ensure_worker(self) -> None:
        with self._worker_lock:
            if self._worker is not None and self._worker.is_alive():
                return
            self._stop_event.clear()
            self._worker = threading.Thread(target=self._worker_main, daemon=True)
            self._worker.start()

    def _worker_main(self) -> None:
        while not self._stop_event.is_set():
            try:
                item = self._queue.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                if item is self._stop_sentinel:
                    return
                self.spk.play(item)
            except Exception as exc:
                self._last_error = exc
            finally:
                if item is not self._stop_sentinel:
                    with self._pending_lock:
                        if self._pending_blocks > 0:
                            self._pending_blocks -= 1
                self._queue.task_done()

    def reset(self) -> None:
        """Clear worker errors before a new playback session."""
        self._last_error = None
        self._ensure_worker()

    async def play(self, samples: np.ndarray):
        """Enqueue one frame for worker playback with backpressure."""
        if samples is None:
            return
        self._ensure_worker()
        if self._last_error is not None:
            raise self._last_error
        payload = np.asarray(samples).copy()
        while True:
            if self._last_error is not None:
                raise self._last_error
            try:
                with self._pending_lock:
                    self._pending_blocks += 1
                self._queue.put_nowait(payload)
                return
            except queue.Full:
                with self._pending_lock:
                    if self._pending_blocks > 0:
                        self._pending_blocks -= 1
                await asyncio.sleep(self._poll_interval)

    async def flush(self, timeout: float = 1.5) -> bool:
        """Wait until queued frames are handed off to the speaker."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, float(timeout))
        while True:
            if self._last_error is not None:
                raise self._last_error
            with self._pending_lock:
                remaining = self._pending_blocks
            if remaining == 0:
                return True
            if loop.time() >= deadline:
                return False
            await asyncio.sleep(self._poll_interval)

    def close(self, join_timeout: float = 1.0) -> None:
        self._stop_event.set()
        worker = self._worker
        if worker is None:
            return
        while True:
            try:
                self._queue.put_nowait(self._stop_sentinel)
                break
            except queue.Full:
                if not worker.is_alive():
                    break
                # Give worker time to consume one item.
                threading.Event().wait(self._poll_interval)
        worker.join(timeout=max(0.1, float(join_timeout)))

    def __del__(self) -> None:  # pragma: no cover - best effort cleanup
        try:
            self.close()
        except Exception:
            pass
