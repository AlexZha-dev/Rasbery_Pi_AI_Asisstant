from __future__ import annotations

import asyncio
import contextlib
import functools
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal, Optional, Tuple

from application.session_runner import RunnerStatus, SessionRunner
from config.preferences import load_preferences, save_preferences
from exceptions.audio_exceptions import AudioError
from infrastructure.button_interface import ButtonInterface
from infrastructure.device_registry import DeviceRegistry, DeviceSnapshot
from infrastructure.microphone_interface import MicrophoneInterface
from infrastructure.speaker_output import SpeakerInterface
from ui.console_view import ConsoleState, ConsoleView
from ui.lcd_view import LCDView

InputProvider = Callable[[str], str]


@dataclass
class ControllerConfig:
    tabs: Tuple[str, ...] = ("Record", "Microphone", "Speaker")


RecordState = Literal["ready", "recording", "await_close"]


class ConsoleController:
    """Coordinates input loop, view rendering, and session control."""

    def __init__(
        self,
        microphone: MicrophoneInterface,
        speaker: SpeakerInterface,
        registry: DeviceRegistry,
        session_runner: SessionRunner,
        view: Optional[ConsoleView] = None,
        lcd_view: Optional[LCDView] = None,
        button: Optional[ButtonInterface] = None,
        input_provider: InputProvider = input,
        preferences_path: Optional[Path] = None,
        config: ControllerConfig = ControllerConfig(),
    ):
        self._mic = microphone
        self._speaker = speaker
        self._registry = registry
        self._runner = session_runner
        self._view = view or ConsoleView()
        self._lcd_view = lcd_view
        self._button = button
        self._lcd_state: Optional[str] = None
        self._lcd_override_state: Optional[str] = None
        self._lcd_override_until: float = 0.0
        self._lcd_last_session_state: Optional[str] = None
        self._lcd_task: Optional[asyncio.Task] = None
        self._button_task: Optional[asyncio.Task] = None
        self._button_action_lock = asyncio.Lock()
        self._input = input_provider
        self._prefs_path = preferences_path
        self._config = config

        self._tabs = list(config.tabs)
        self._active_tab = 0
        self._message: Optional[str] = None
        self._prefs = load_preferences(preferences_path)
        self._current_snapshot: DeviceSnapshot = self._registry.refresh()
        self._last_refresh = time.monotonic()
        self._refresh_interval = 5.0
        self._force_refresh = False
        self._selected_mic = self._initialise_device("mic_device", True)
        self._selected_speaker = self._initialise_device("speaker_device", False)
        self._record_state: RecordState = (
            "recording" if self._runner.is_running() else "ready"
        )
        self._update_lcd_state(force=True)

    async def run(self) -> None:
        running = True
        loop = self._start_background_tasks()
        try:
            while running:
                state = self._render_state()
                try:
                    command = await self._await_command(loop, state)
                except EOFError:
                    print("[Console] EOF received, exiting.")
                    command = "q"
                running = await self.handle_command(command)
        except KeyboardInterrupt:
            self._message = "Interrupted, shutting down."
        finally:
            await self._shutdown()

    async def run_service(
        self,
        *,
        stop_event: Optional[asyncio.Event] = None,
        poll_interval: float = 0.25,
    ) -> None:
        self._start_background_tasks()
        poll_interval = max(0.05, float(poll_interval))
        last_state = self._build_state()
        self._view.render(last_state)
        self._update_lcd_state(force=True)
        try:
            while True:
                if stop_event is not None and stop_event.is_set():
                    break
                state = self.get_state()
                if state != last_state:
                    self._view.render(state)
                    self._update_lcd_state()
                    last_state = state
                if stop_event is None:
                    await asyncio.sleep(poll_interval)
                    continue
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=poll_interval)
                except asyncio.TimeoutError:
                    continue
                break
        except KeyboardInterrupt:
            self._message = "Interrupted, shutting down."
        finally:
            await self._shutdown()

    def get_state(self) -> ConsoleState:
        self._maybe_refresh_devices()
        return self._build_state()

    def _render_state(self) -> ConsoleState:
        self._maybe_refresh_devices()
        state = self._build_state()
        self._view.render(state)
        self._update_lcd_state()
        return state

    def _start_background_tasks(self) -> asyncio.AbstractEventLoop:
        loop = asyncio.get_running_loop()
        if self._lcd_view is not None and self._lcd_task is None:
            self._lcd_task = loop.create_task(self._lcd_heartbeat())
        if (
            self._button is not None
            and self._button.is_enabled
            and self._button_task is None
        ):
            self._button_task = loop.create_task(self._button_listener())
        return loop

    async def _shutdown(self) -> None:
        if self._runner.is_running():
            await self._runner.stop()
        self._update_lcd_state(force=True)
        if self._lcd_task is not None:
            self._lcd_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._lcd_task
            self._lcd_task = None
        if self._button_task is not None:
            self._button_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._button_task
            self._button_task = None
        if self._button is not None:
            self._button.close()
            self._button = None

    async def _await_command(
        self,
        loop: asyncio.AbstractEventLoop,
        initial_state: ConsoleState,
        poll_interval: float = 0.1,
    ) -> str:
        print("[Console] Awaiting command...")
        command_future = loop.run_in_executor(None, self._input, "Command: ")
        last_state = initial_state
        while True:
            done, _ = await asyncio.wait({command_future}, timeout=poll_interval)
            if command_future in done:
                return command_future.result()
            refreshed_state = self.get_state()
            if refreshed_state != last_state:
                self._view.render(refreshed_state)
                self._update_lcd_state()
                last_state = refreshed_state

    async def handle_command(self, command: str) -> bool:
        cmd = (command or "").strip().lower()
        print(f"[Console] Received command: {cmd!r}")
        if not cmd:
            self._message = "No command entered."
            return True
        action = cmd[0]
        if action == "q":
            self._message = "Exiting console."
            return False
        if action == "1":
            self._active_tab = (self._active_tab - 1) % len(self._tabs)
            self._force_refresh = True
            return True
        if action == "3":
            self._active_tab = (self._active_tab + 1) % len(self._tabs)
            self._force_refresh = True
            return True
        if action == "2":
            await self._handle_accept()
            return True
        if action == "c":
            await self._handle_close_websocket()
            return True
        self._message = f"Unknown command: {command}"
        return True

    async def _handle_accept(self) -> None:
        tab = self._tabs[self._active_tab].lower()
        if tab == "record":
            await self._toggle_session()
        elif tab == "microphone":
            await self._choose_device(is_input=True)
        elif tab == "speaker":
            await self._choose_device(is_input=False)

    async def _toggle_session(self) -> None:
        self._sync_record_state()
        if self._record_state == "ready":
            await self._start_recording()
            return
        if self._record_state == "recording":
            await self._stop_recording()
            return
        await self._handle_close_websocket()

    async def _start_recording(self) -> None:
        success, msg = await self._runner.start()
        if success:
            self._record_state = "recording"
            self._message = msg
            self._queue_lcd_state("recording_active", hold_for=1.0)
        else:
            self._record_state = "ready"
            self._message = f"Session error: {msg}"
            self._queue_lcd_state("error")

    async def _stop_recording(self) -> None:
        success, msg = await self._runner.stop(wait_for_completion=False)
        if success:
            self._record_state = "await_close"
            lowered = (msg or "").lower()
            if "stop requested" in lowered or not msg:
                self._message = "Recording stopped; press 2 again to end the session."
            else:
                self._message = msg
            self._queue_lcd_state("sending", hold_for=1.0)
            return
        lowered = (msg or "").lower()
        if "already requested" in lowered:
            self._record_state = "await_close"
            self._message = "Stop already requested; press 2 again to end the session."
            self._queue_lcd_state("waiting_for_response", hold_for=1.0)
            return
        if "playback still running" in lowered:
            self._record_state = "await_close"
            self._message = "Playback still running; waiting to finish..."
            self._queue_lcd_state("waiting_for_response", hold_for=1.0)
            return
        if "not running" in lowered:
            self._record_state = "ready"
            self._message = "Session is not running"
            self._queue_lcd_state("answer_stopped", hold_for=2.0)
            return
        self._record_state = "ready"
        self._message = f"Session error: {msg}"
        self._queue_lcd_state("error")

    async def _handle_close_websocket(self) -> bool:
        success, msg = await self._runner.close_websocket()
        if success:
            await self._wait_for_runner_idle()
        self._message = msg
        lowered = msg.lower() if msg else ""
        if success or "not running" in lowered or "already requested" in lowered:
            self._record_state = "ready"
        if success:
            self._queue_lcd_state("answer_stopped", hold_for=2.0)
        else:
            if "not running" in lowered:
                self._queue_lcd_state("answer_stopped", hold_for=2.0)
            elif "already requested" in lowered:
                self._queue_lcd_state("waiting_for_response", hold_for=1.0)
            else:
                self._queue_lcd_state("error")
        return success

    async def _choose_device(self, *, is_input: bool) -> None:
        prompt = (
            "Enter input device index: " if is_input else "Enter output device index: "
        )
        try:
            raw = await self._prompt(prompt)
        except EOFError:
            self._message = "Selection cancelled."
            return
        try:
            index = int(raw.strip())
        except ValueError:
            self._message = f"Invalid index: {raw}"
            return
        if is_input:
            if not self._registry.is_valid_input(index):
                self._message = f"No input device at index {index}"
                return
            await self._apply_input_device(index)
        else:
            if not self._registry.is_valid_output(index):
                self._message = f"No output device at index {index}"
                return
            await self._apply_output_device(index)

    async def _apply_input_device(self, index: int) -> None:
        if self._runner.is_running():
            await self._runner.stop()
        try:
            self._mic.set_input_device(index)
            self._selected_mic = index
            self._prefs["mic_device"] = index
            signature = self._registry.input_signature(index)
            if signature:
                self._prefs["mic_signature"] = signature
            self._persist_preferences()
            self._message = f"Microphone set to device {index}"
            self._force_refresh = True
        except AudioError as exc:
            self._message = f"Failed to set microphone: {exc}"

    async def _apply_output_device(self, index: int) -> None:
        if self._runner.is_running():
            await self._runner.stop()
        try:
            self._speaker.set_output_device(index)
            self._selected_speaker = index
            self._prefs["speaker_device"] = index
            signature = self._registry.output_signature(index)
            if signature:
                self._prefs["speaker_signature"] = signature
            self._persist_preferences()
            self._message = f"Speaker set to device {index}"
            self._force_refresh = True
        except AudioError as exc:
            self._message = f"Failed to set speaker: {exc}"

    def _build_state(self) -> ConsoleState:
        self._sync_record_state()
        status = self._runner.get_status()
        return ConsoleState(
            tabs=self._tabs,
            active_tab=self._active_tab,
            session_state=status.state,
            session_message=status.message,
            selected_mic=self._selected_mic,
            selected_speaker=self._selected_speaker,
            mic_devices=self._current_snapshot.input_devices,
            speaker_devices=self._current_snapshot.output_devices,
            message=self._resolved_message(status),
            record_action=self._record_action_label(),
        )

    def _resolved_message(self, status: RunnerStatus) -> Optional[str]:
        if self._message:
            return self._message
        msg = status.message.strip()
        if status.state == "idle" and msg.lower() == "ready":
            return None
        return msg

    def _initialise_device(self, pref_key: str, is_input: bool) -> Optional[int]:
        pref_value = self._prefs.get(pref_key)
        signature_key = "mic_signature" if is_input else "speaker_signature"
        signature_value = self._prefs.get(signature_key)
        if is_input:
            is_valid = (
                self._registry.is_valid_input(pref_value)
                if pref_value is not None
                else False
            )
            resolved_from_signature = self._registry.resolve_input_signature(
                signature_value
            )
            default_index = self._registry.default_input_index()
            apply_fn = self._mic.set_input_device
            label = "microphone"
        else:
            is_valid = (
                self._registry.is_valid_output(pref_value)
                if pref_value is not None
                else False
            )
            resolved_from_signature = self._registry.resolve_output_signature(
                signature_value
            )
            default_index = self._registry.default_output_index()
            apply_fn = self._speaker.set_output_device
            label = "speaker"
        target_index = pref_value if is_valid else resolved_from_signature
        if target_index is None:
            target_index = default_index
        if target_index is None:
            self._message = f"No {label} devices detected."
            return None
        if pref_value is None or not is_valid:
            self._prefs[pref_key] = target_index
            if is_input:
                resolved_signature = self._registry.input_signature(target_index)
            else:
                resolved_signature = self._registry.output_signature(target_index)
            if resolved_signature:
                self._prefs[signature_key] = resolved_signature
            self._persist_preferences()
        try:
            apply_fn(target_index)
        except AudioError as exc:
            self._message = f"Failed to set {label}: {exc}"
        return target_index

    def _maybe_refresh_devices(self) -> None:
        now = time.monotonic()
        active_label = self._tabs[self._active_tab].lower()
        should_refresh = self._force_refresh
        if not should_refresh:
            if active_label != "record":
                should_refresh = True
            elif (
                not self._runner.is_running()
                and (now - self._last_refresh) >= self._refresh_interval
            ):
                should_refresh = True
        if should_refresh:
            self._current_snapshot = self._registry.refresh()
            self._last_refresh = now
            self._force_refresh = False

    def _sync_record_state(self) -> None:
        if (
            self._record_state in {"recording", "await_close"}
            and not self._runner.is_running()
        ):
            self._record_state = "ready"

    def _record_action_label(self) -> str:
        if self._record_state == "ready":
            return "start recording"
        if self._record_state == "recording":
            return "stop recording"
        return "close the session"

    async def _prompt(self, prompt: str) -> str:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._input, prompt)

    def _persist_preferences(self) -> None:
        save_preferences(self._prefs, self._prefs_path)

    async def _wait_for_runner_idle(self, timeout: float = 5.0) -> bool:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while self._runner.is_running() and loop.time() < deadline:
            await asyncio.sleep(0.1)
        return not self._runner.is_running()

    def _queue_lcd_state(self, state: str, *, hold_for: float = 0.0) -> None:
        if self._lcd_view is None:
            return
        now = time.monotonic()
        self._lcd_override_state = state
        self._lcd_override_until = now + hold_for if hold_for > 0 else 0.0
        self._update_lcd_state(force=True)

    def _update_lcd_state(self, *, force: bool = False) -> None:
        if self._lcd_view is None:
            return
        now = time.monotonic()
        state: Optional[str] = None
        if self._lcd_override_state is not None:
            if self._lcd_override_until == 0.0 or now < self._lcd_override_until:
                state = self._lcd_override_state
            else:
                self._lcd_override_state = None
                self._lcd_override_until = 0.0
        if state is None:
            state = self._determine_lcd_state()
            if (
                state in {"answer_ended", "answer_stopped"}
                and self._lcd_override_state is None
            ):
                self._lcd_override_state = state
                self._lcd_override_until = now + 2.0
        if force or state != self._lcd_state:
            self._lcd_view.show_state(state)
            self._lcd_state = state
        self._lcd_last_session_state = state

    def _determine_lcd_state(self) -> str:
        self._sync_record_state()
        status = self._runner.get_status()
        pending_audio = self._speaker_has_pending_audio()
        recent_activity = self._speaker_recent_activity()

        if status.state == "error":
            return "error"

        if (
            self._record_state == "recording"
            or status.state in {"connecting", "recording"}
        ):
            return "recording_active"

        speaker_active = pending_audio or recent_activity
        if speaker_active and self._record_state != "recording":
            return "answer_playing"

        if status.state == "stopping":
            return "waiting_for_response"

        if self._record_state == "await_close":
            if not self._runner.is_running():
                return "waiting_for_recording"
            return "waiting_for_response"

        if status.state == "idle":
            lowered = (status.message or "").lower()
            if "completed" in lowered:
                if self._lcd_last_session_state == "answer_ended":
                    return "waiting_for_recording"
                return "answer_ended"
            if "stopped" in lowered:
                if self._lcd_last_session_state == "answer_stopped":
                    return "waiting_for_recording"
                return "answer_stopped"
            return "waiting_for_recording"

        return "waiting_for_recording"

    def _speaker_has_pending_audio(self) -> bool:
        get_pending_frames = getattr(self._speaker, "pending_frames", None)
        if callable(get_pending_frames):
            try:
                if int(get_pending_frames()) > 0:
                    return True
            except Exception:
                pass

        get_pending = getattr(self._speaker, "pending_blocks", None)
        if callable(get_pending):
            try:
                return int(get_pending()) > 0
            except Exception:
                return False
        return False

    async def _button_listener(self) -> None:
        button = self._button
        if button is None or not button.is_enabled:
            return
        loop = asyncio.get_running_loop()
        wait_press = functools.partial(button.wait_for_press, 0.1)
        wait_release = functools.partial(button.wait_for_release, 0.3)
        try:
            while True:
                pressed = await loop.run_in_executor(None, wait_press)
                if not pressed:
                    continue
                async with self._button_action_lock:
                    await self._toggle_session()
                await loop.run_in_executor(None, wait_release)
        except asyncio.CancelledError:  # pragma: no cover - cancellation path
            pass

    def _speaker_recent_activity(self, window: float = 0.75) -> bool:
        activity_fn = getattr(self._speaker, "had_recent_activity", None)
        if not callable(activity_fn):
            return False
        try:
            return bool(activity_fn(window=window))
        except TypeError:
            try:
                return bool(activity_fn(window))
            except Exception:
                return False
        except Exception:
            return False

    async def _lcd_heartbeat(self) -> None:
        try:
            while True:
                await asyncio.sleep(0.5)
                self._update_lcd_state()
        except asyncio.CancelledError:  # pragma: no cover - cancellation path
            pass
