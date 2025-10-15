from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Tuple

from application.session_runner import RunnerStatus, SessionRunner
from config.preferences import load_preferences, save_preferences
from exceptions.audio_exceptions import AudioError
from infrastructure.device_registry import DeviceRegistry, DeviceSnapshot
from infrastructure.microphone_interface import MicrophoneInterface
from infrastructure.speaker_interface import SpeakerInterface
from ui.console_view import ConsoleState, ConsoleView

InputProvider = Callable[[str], str]


@dataclass
class ControllerConfig:
    tabs: Tuple[str, ...] = ("Record", "Microphone", "Speaker")


class ConsoleController:
    """Coordinates input loop, view rendering, and session control."""

    def __init__(
        self,
        microphone: MicrophoneInterface,
        speaker: SpeakerInterface,
        registry: DeviceRegistry,
        session_runner: SessionRunner,
        view: Optional[ConsoleView] = None,
        input_provider: InputProvider = input,
        preferences_path: Optional[Path] = None,
        config: ControllerConfig = ControllerConfig(),
    ):
        self._mic = microphone
        self._speaker = speaker
        self._registry = registry
        self._runner = session_runner
        self._view = view or ConsoleView()
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

    async def run(self) -> None:
        running = True
        try:
            while running:
                self._maybe_refresh_devices()
                state = self._build_state()
                self._view.render(state)
                try:
                    print("[Console] Awaiting command...")
                    command = await self._prompt("Command: ")
                except EOFError:
                    print("[Console] EOF received, exiting.")
                    command = "q"
                running = await self.handle_command(command)
        except KeyboardInterrupt:
            self._message = "Interrupted, shutting down."
        finally:
            if self._runner.is_running():
                await self._runner.stop()

    def get_state(self) -> ConsoleState:
        self._maybe_refresh_devices()
        return self._build_state()

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
        if self._runner.is_running():
            await self._stop_recording()
        else:
            await self._start_recording()

    async def _start_recording(self) -> None:
        success, msg = await self._runner.start()
        self._message = msg if success else f"Session error: {msg}"

    async def _stop_recording(self) -> None:
        success, msg = await self._runner.stop()
        if success:
            await self._wait_for_runner_idle()
            self._message = "Session stopped"
            return
        lowered = msg.lower()
        if "already requested" in lowered:
            completed = await self._wait_for_runner_idle()
            self._message = (
                "Session stopped" if completed else "Session stop in progress..."
            )
            return
        if "not running" in lowered:
            self._message = "Session is not running"
            return
        self._message = f"Session error: {msg}"

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
            self._persist_preferences()
            self._message = f"Speaker set to device {index}"
            self._force_refresh = True
        except AudioError as exc:
            self._message = f"Failed to set speaker: {exc}"

    def _build_state(self) -> ConsoleState:
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
        if is_input:
            is_valid = (
                self._registry.is_valid_input(pref_value)
                if pref_value is not None
                else False
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
            default_index = self._registry.default_output_index()
            apply_fn = self._speaker.set_output_device
            label = "speaker"
        target_index = pref_value if is_valid else default_index
        if target_index is None:
            self._message = f"No {label} devices detected."
            return None
        if pref_value is None or not is_valid:
            self._prefs[pref_key] = target_index
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
