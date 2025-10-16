from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass
from typing import Callable, Optional, Tuple

from application.audio_session import AudioSession
from exceptions.audio_exceptions import AudioError
from infrastructure.websocket_client import AudioWebSocketClient

SessionFactory = Callable[[], Tuple[AudioSession, AudioWebSocketClient]]
StopCallback = Callable[[], None]


@dataclass
class RunnerStatus:
    state: str
    message: str = ""


class SessionRunner:
    """Orchestrates AudioSession as an asyncio task to keep the console responsive."""

    def __init__(
        self,
        session_factory: SessionFactory,
        request_stop: StopCallback,
        session_timeout: Optional[float] = None,
        playback_timeout: Optional[float] = None,
        join_timeout: float = 5.0,
        loop: Optional[asyncio.AbstractEventLoop] = None,
    ):
        self._factory = session_factory
        self._request_stop = request_stop
        self._session_timeout = session_timeout
        self._playback_timeout = playback_timeout
        self._join_timeout = join_timeout
        self._loop = loop

        self._status = RunnerStatus("idle", "Ready")
        self._status_lock = threading.Lock()
        self._task: Optional[asyncio.Task[None]] = None
        self._lock = asyncio.Lock()
        self._stop_requested = False

    async def start(self) -> Tuple[bool, str]:
        async with self._lock:
            if self._task and not self._task.done():
                return False, "Session already running"
            if self._task and self._task.done():
                self._task = None
            self._set_status("connecting", "Connecting to audio server...")
            self._stop_requested = False
            loop = self._event_loop()
            self._task = loop.create_task(self._run_session())
        return True, "Session starting"

    async def stop(self) -> Tuple[bool, str]:
        async with self._lock:
            task = self._task
            if task is None:
                return False, "Session is not running"
            if task.done():
                self._task = None
                return False, "Session is not running"
            if self._stop_requested:
                return False, "Stop already requested"
            self._stop_requested = True
        self._set_status("stopping", "Stopping session...")
        loop = self._event_loop()
        try:
            await loop.run_in_executor(None, self._request_stop)
        except Exception as exc:  # pragma: no cover - defensive hardware failure
            self._set_status("error", f"Failed to stop session: {exc}")
            async with self._lock:
                self._stop_requested = False
            return False, f"Failed to stop session: {exc}"
        try:
            await asyncio.wait_for(task, timeout=self._join_timeout)
        except asyncio.TimeoutError:
            self._set_status("error", "Session task did not exit in time")
            return False, "Session task did not exit in time"
        return True, "Session stopped"

    def is_running(self) -> bool:
        task = self._task
        return bool(task and not task.done())

    def get_status(self) -> RunnerStatus:
        with self._status_lock:
            return RunnerStatus(self._status.state, self._status.message)

    # --- internal helpers ---

    def _event_loop(self) -> asyncio.AbstractEventLoop:
        if self._loop is not None:
            return self._loop
        return asyncio.get_running_loop()

    def _set_status(self, state: str, message: str) -> None:
        with self._status_lock:
            self._status = RunnerStatus(state, message)

    async def _run_session(self) -> None:
        session: Optional[AudioSession] = None
        client: Optional[AudioWebSocketClient] = None
        try:
            session, client = self._factory()
        except Exception as exc:
            self._set_status("error", f"Failed to prepare session: {exc}")
            async with self._lock:
                self._task = None
                self._stop_requested = False
            return

        try:
            if self._stop_requested:
                self._set_status("idle", "Session stopped")
                return
            self._set_status("recording", "Streaming audio (press 2 to stop)")
            await session.run_once(
                timeout=self._session_timeout, playback_timeout=self._playback_timeout
            )
            if self._stop_requested:
                self._set_status("idle", "Session stopped")
            else:
                self._set_status("idle", "Session completed")
        except asyncio.CancelledError:  # pragma: no cover - defensive
            self._set_status("idle", "Session cancelled")
            raise
        except AudioError as exc:
            self._set_status("error", f"Audio error: {exc}")
        except Exception as exc:
            self._set_status("error", f"Unexpected error: {exc}")
        finally:
            if client is not None:
                await self._safe_close(client)
            async with self._lock:
                self._task = None
                self._stop_requested = False

    async def _safe_close(self, client: AudioWebSocketClient) -> None:
        try:
            await client.close()
        except Exception:  # pragma: no cover - defensive cleanup
            pass
