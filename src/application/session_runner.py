from __future__ import annotations

import asyncio
import inspect
import logging
import threading
from dataclasses import dataclass
from typing import Callable, Optional, Tuple

from application.audio_session import AudioSession
from exceptions.audio_exceptions import AudioError
from infrastructure.websocket_client import AudioWebSocketClient

logger = logging.getLogger(__name__)

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
        self._active_session: Optional[AudioSession] = None
        self._active_client: Optional[AudioWebSocketClient] = None
        self._force_close_reason: Optional[str] = None
        self._force_close_trigger: Optional[str] = None

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

    async def stop(self, *, wait_for_completion: bool = True) -> Tuple[bool, str]:
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
        if not wait_for_completion:
            return True, "Stop requested"
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=self._join_timeout)
        except asyncio.TimeoutError:
            self._set_status("stopping", "Playback still finishing...")
            return False, "Playback still running; waiting for completion"
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
        close_reason = "session_cleanup"
        close_trigger = "session_runner._run_session"
        skip_final_wait = False
        try:
            session, client = self._factory()
            self._active_session = session
            self._active_client = client
        except Exception as exc:
            self._set_status("error", f"Failed to prepare session: {exc}")
            async with self._lock:
                self._task = None
                self._stop_requested = False
            return

        try:
            if self._stop_requested:
                self._set_status("idle", "Session stopped")
                close_reason = "stop_requested_before_start"
                close_trigger = "session_runner.stop_before_start"
                return
            self._set_status("recording", "Streaming audio (press 2 to stop)")
            await session.run_once(
                timeout=self._session_timeout, playback_timeout=self._playback_timeout
            )
            if self._stop_requested:
                self._set_status("idle", "Session stopped")
                close_reason = "stop_requested"
                close_trigger = "session_runner.stop"
            else:
                self._set_status("idle", "Session completed")
                close_reason = "session_completed"
                close_trigger = "session_runner.complete"
        except asyncio.CancelledError:  # pragma: no cover - defensive
            close_reason = self._force_close_reason or "cancelled"
            close_trigger = self._force_close_trigger or "session_runner.cancelled"
            self._force_close_reason = None
            self._force_close_trigger = None
            if close_reason == "console_close":
                self._set_status("idle", "Session closed by console")
                skip_final_wait = True
            else:
                self._set_status("idle", "Session cancelled")
                await self._await_playback_completion(session)
                skip_final_wait = True
        except AudioError as exc:
            self._set_status("error", f"Audio error: {exc}")
            close_reason = "audio_error"
            close_trigger = "session_runner.audio_error"
        except Exception as exc:
            self._set_status("error", f"Unexpected error: {exc}")
            close_reason = "unexpected_error"
            close_trigger = "session_runner.unexpected"
        finally:
            if not skip_final_wait and close_reason != "cancelled":
                await self._await_playback_completion(session)
            if client is not None:
                await self._safe_close(
                    client, reason=close_reason, trigger=close_trigger
                )
            async with self._lock:
                self._task = None
                self._stop_requested = False
            self._active_session = None
            self._active_client = None
            self._force_close_reason = None
            self._force_close_trigger = None

    async def _safe_close(
        self, client: AudioWebSocketClient, reason: str, trigger: str
    ) -> None:
        try:
            close_func = getattr(client, "close", None)
            if not callable(close_func):
                return
            note_trigger = getattr(client, "note_close_trigger", None)
            if callable(note_trigger):
                try:
                    note_trigger(trigger, detail=reason)
                except Exception:
                    pass
            kwargs = {}
            if self._close_accepts_arg(close_func, "reason"):
                kwargs["reason"] = reason
            elif self._close_accepts_arg(close_func, "cause"):
                kwargs["cause"] = reason
            if self._close_accepts_arg(close_func, "trigger"):
                kwargs["trigger"] = trigger
            result = close_func(**kwargs) if kwargs else close_func()
            if inspect.isawaitable(result):
                await result
        except TypeError as exc:
            if "reason" in str(exc) or "trigger" in str(exc):
                result = close_func()
                if inspect.isawaitable(result):
                    await result
            else:  # pragma: no cover - unexpected TypeError
                raise
        except Exception:  # pragma: no cover - defensive cleanup
            pass

    async def close_websocket(self) -> Tuple[bool, str]:
        async with self._lock:
            task = self._task
            if task is None or task.done():
                return False, "Session is not running"
            if self._force_close_reason is not None:
                return False, "Close already requested"
            self._force_close_reason = "console_close"
            self._force_close_trigger = "console.close"
            task.cancel()
            wait_task = task
        try:
            await asyncio.wait_for(
                asyncio.shield(wait_task), timeout=self._join_timeout
            )
        except asyncio.TimeoutError:
            return False, "Websocket close timed out"
        except asyncio.CancelledError:
            return True, "Websocket close requested"
        return True, "Websocket closed"

    async def _await_playback_completion(self, session: Optional[AudioSession]) -> None:
        if session is None:
            return
        wait_timeout = self._playback_timeout if self._playback_timeout else 10.0
        try:
            completed = await asyncio.shield(
                session.wait_for_playback_completion(wait_timeout)
            )
        except asyncio.CancelledError:
            raise
        except AttributeError:
            # Older session implementation without wait_for_playback_completion
            return
        except Exception:
            # Diagnostic only – swallow to avoid masking real shutdown causes
            pass
        else:
            if not completed:
                logger.warning(
                    "[SessionRunner] Playback completion wait timed out session_id=%s timeout=%.2f",
                    getattr(session, "session_id", "unknown"),
                    wait_timeout,
                )

    @staticmethod
    def _close_accepts_arg(close_func, name: str) -> bool:
        try:
            signature = inspect.signature(close_func)
        except (ValueError, TypeError):
            return False
        for param in signature.parameters.values():
            if param.kind == inspect.Parameter.VAR_KEYWORD:
                return True
            if param.name == name and param.kind in (
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                inspect.Parameter.KEYWORD_ONLY,
            ):
                return True
        return False
