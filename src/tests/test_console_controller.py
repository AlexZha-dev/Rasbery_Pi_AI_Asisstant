import asyncio
import json
import queue
import shutil
import threading
import time
import uuid
from pathlib import Path

import pytest

from application.session_runner import RunnerStatus
from controllers.console_controller import ConsoleController
from infrastructure.device_registry import DeviceInfo, DeviceRegistry, DeviceSnapshot


class StubMicrophone:
    def __init__(self):
        self.device = None

    def set_input_device(self, index: int):
        self.device = index

    def stop_recording(self):
        pass


class StubSpeaker:
    def __init__(self):
        self.device = None
        self._is_playing = False
        self._pending_blocks = 0
        self._pending_frames = 0
        self._recent_activity = False

    def set_output_device(self, index: int):
        self.device = index

    @property
    def is_playing(self):
        return self._is_playing

    def pending_blocks(self):
        return self._pending_blocks

    def pending_frames(self):
        return self._pending_frames

    def had_recent_activity(self, window: float = 0.75):
        return self._recent_activity


class FakeRunner:
    def __init__(self):
        self.running = False
        self.start_calls = 0
        self.stop_calls = 0
        self._status = RunnerStatus("idle", "Ready")

    async def start(self):
        if self.running:
            return False, "Session already running"
        self.running = True
        self.start_calls += 1
        self._status = RunnerStatus("recording", "Streaming audio (press 2 to stop)")
        return True, "Session starting"

    async def stop(self, *, wait_for_completion: bool = True):
        if not self.running:
            return False, "Session is not running"
        self.running = False
        self.stop_calls += 1
        self._status = RunnerStatus("idle", "Session stopped")
        return True, "Session stopped"

    def is_running(self):
        return self.running

    def get_status(self):
        return self._status


class FakeInput:
    def __init__(self):
        self._queue: "queue.Queue[str]" = queue.Queue()

    def push(self, value: str):
        self._queue.put(value)

    def __call__(self, prompt: str) -> str:
        try:
            return self._queue.get_nowait()
        except queue.Empty as exc:
            raise EOFError("No input queued") from exc


class BlockingInput:
    def __init__(self):
        self._queue: "queue.Queue[str]" = queue.Queue()

    def push(self, value: str):
        self._queue.put(value)

    def __call__(self, prompt: str) -> str:
        return self._queue.get()


class FailingInput:
    def __call__(self, prompt: str) -> str:
        raise AssertionError("input() should not be used in service mode")


class RecordingView:
    def __init__(self):
        self.states = []

    def render(self, state):
        self.states.append(state)


class FakeServiceButton:
    def __init__(self):
        self.is_enabled = True
        self.closed = False
        self._presses: "queue.Queue[bool]" = queue.Queue()

    def press(self):
        self._presses.put(True)

    def wait_for_press(self, timeout=None):
        try:
            self._presses.get(timeout=timeout)
            return True
        except queue.Empty:
            return False

    def wait_for_release(self, timeout=None):
        return True

    def close(self):
        self.closed = True
        self.is_enabled = False


class HoldServiceButton:
    def __init__(self):
        self.is_enabled = True
        self.closed = False
        self._pressed = threading.Event()

    def press(self):
        self._pressed.set()

    def release(self):
        self._pressed.clear()

    def wait_for_press(self, timeout=None):
        return self._pressed.wait(timeout)

    def wait_for_release(self, timeout=None):
        if not self._pressed.is_set():
            return True
        deadline = None if timeout is None else time.monotonic() + timeout
        while self._pressed.is_set():
            if deadline is not None and time.monotonic() >= deadline:
                return False
            time.sleep(0.01)
        return True

    def close(self):
        self.closed = True
        self.is_enabled = False
        self._pressed.clear()


class StubRegistry(DeviceRegistry):
    def __init__(self):  # type: ignore[no-untyped-def]
        self.refresh_calls = 0
        self._sd = type(
            "StubSoundDeviceModule",
            (),
            {"default": type("Defaults", (), {"device": (0, 1)})()},
        )()
        self._snapshot = DeviceSnapshot(
            input_devices=[
                DeviceInfo(
                    index=0, name="Mic A", max_input_channels=1, max_output_channels=0
                ),
                DeviceInfo(
                    index=1, name="Mic B", max_input_channels=2, max_output_channels=0
                ),
            ],
            output_devices=[
                DeviceInfo(
                    index=1,
                    name="Speaker X",
                    max_input_channels=0,
                    max_output_channels=2,
                ),
                DeviceInfo(
                    index=2,
                    name="Speaker Y",
                    max_input_channels=0,
                    max_output_channels=2,
                ),
            ],
        )

    def refresh(self):
        self.refresh_calls += 1
        return self._snapshot

    def snapshot(self):
        return self._snapshot


@pytest.fixture
def workspace_tmp_path():
    root = Path(__file__).resolve().parent / "_tmp_console"
    root.mkdir(parents=True, exist_ok=True)
    path = root / uuid.uuid4().hex
    path.mkdir(parents=True, exist_ok=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def build_controller(tmp_path, fake_input=None):
    mic = StubMicrophone()
    spk = StubSpeaker()
    registry = StubRegistry()
    runner = FakeRunner()
    fake_input = fake_input or FakeInput()
    controller = ConsoleController(
        microphone=mic,
        speaker=spk,
        registry=registry,
        session_runner=runner,
        input_provider=fake_input,
        preferences_path=tmp_path / "prefs.json",
    )
    return controller, mic, spk, runner, fake_input, registry


@pytest.mark.asyncio
async def test_controller_toggle_session(workspace_tmp_path):
    controller, mic, spk, runner, fake_input, registry = build_controller(
        workspace_tmp_path
    )
    assert mic.device == 0
    assert spk.device == 1

    await controller.handle_command("2")  # start
    assert runner.running is True
    assert controller.get_state().message == "Session starting"

    await controller.handle_command("2")  # stop
    assert runner.running is False
    assert runner.stop_calls == 1
    assert controller.get_state().message == "Session stopped"


@pytest.mark.asyncio
async def test_controller_selects_microphone(workspace_tmp_path):
    fake_input = FakeInput()
    controller, mic, spk, runner, fake_input, registry = build_controller(
        workspace_tmp_path, fake_input
    )
    await controller.handle_command("3")  # to microphone tab
    fake_input.push("1")
    await controller.handle_command("2")
    assert mic.device == 1
    state = controller.get_state()
    assert state.selected_mic == 1
    assert "device 1" in (state.message or "").lower()
    saved = json.loads((workspace_tmp_path / "prefs.json").read_text())
    assert saved["mic_device"] == 1


@pytest.mark.asyncio
async def test_controller_rejects_invalid_device(workspace_tmp_path):
    fake_input = FakeInput()
    controller, mic, spk, runner, fake_input, registry = build_controller(
        workspace_tmp_path, fake_input
    )
    await controller.handle_command("3")  # microphone tab
    fake_input.push("99")
    await controller.handle_command("2")
    state = controller.get_state()
    assert "no input device" in (state.message or "").lower()
    assert mic.device == 0  # unchanged


@pytest.mark.asyncio
async def test_controller_restores_device_from_signature_when_index_is_stale(
    workspace_tmp_path,
):
    registry = StubRegistry()
    prefs_path = workspace_tmp_path / "prefs.json"
    prefs_path.write_text(
        json.dumps(
            {
                "mic_device": 99,
                "mic_signature": registry.input_signature(1),
                "speaker_device": 98,
                "speaker_signature": registry.output_signature(2),
            }
        )
    )

    controller = ConsoleController(
        microphone=StubMicrophone(),
        speaker=StubSpeaker(),
        registry=registry,
        session_runner=FakeRunner(),
        input_provider=FakeInput(),
        preferences_path=prefs_path,
    )

    state = controller.get_state()

    assert state.selected_mic == 1
    assert state.selected_speaker == 2


@pytest.mark.asyncio
async def test_controller_refresh_skips_on_record_tab(workspace_tmp_path):
    controller, mic, spk, runner, fake_input, registry = build_controller(
        workspace_tmp_path
    )
    initial_calls = registry.refresh_calls
    await controller.handle_command("2")  # start session
    controller.get_state()
    assert (
        registry.refresh_calls == initial_calls
    )  # no refresh while recording tab active
    await controller.handle_command("3")  # move to microphone tab
    controller.get_state()
    assert registry.refresh_calls > initial_calls


@pytest.mark.asyncio
async def test_controller_rerenders_after_runner_finishes_while_waiting_for_input(
    workspace_tmp_path,
):
    mic = StubMicrophone()
    spk = StubSpeaker()
    registry = StubRegistry()
    runner = FakeRunner()
    runner.running = True
    runner._status = RunnerStatus("recording", "Streaming audio (press 2 to stop)")
    blocking_input = BlockingInput()
    view = RecordingView()
    controller = ConsoleController(
        microphone=mic,
        speaker=spk,
        registry=registry,
        session_runner=runner,
        view=view,
        input_provider=blocking_input,
        preferences_path=workspace_tmp_path / "prefs.json",
    )

    task = asyncio.create_task(controller.run())
    try:
        for _ in range(20):
            if view.states:
                break
            await asyncio.sleep(0.05)
        assert view.states
        assert view.states[-1].session_state == "recording"

        runner.running = False
        runner._status = RunnerStatus("idle", "Session completed")

        for _ in range(20):
            if any(
                state.session_state == "idle"
                and "completed" in state.session_message.lower()
                for state in view.states
            ):
                break
            await asyncio.sleep(0.05)

        assert any(
            state.session_state == "idle"
            and "completed" in state.session_message.lower()
            for state in view.states
        )
    finally:
        blocking_input.push("q")
        await asyncio.wait_for(task, timeout=2.0)


@pytest.mark.asyncio
async def test_service_mode_uses_button_without_console_input(workspace_tmp_path):
    mic = StubMicrophone()
    spk = StubSpeaker()
    registry = StubRegistry()
    runner = FakeRunner()
    button = FakeServiceButton()
    view = RecordingView()
    controller = ConsoleController(
        microphone=mic,
        speaker=spk,
        registry=registry,
        session_runner=runner,
        view=view,
        button=button,
        input_provider=FailingInput(),
        preferences_path=workspace_tmp_path / "prefs.json",
    )
    stop_event = asyncio.Event()

    task = asyncio.create_task(
        controller.run_service(stop_event=stop_event, poll_interval=0.01)
    )
    try:
        await asyncio.sleep(0.05)

        button.press()
        for _ in range(20):
            if runner.start_calls == 1:
                break
            await asyncio.sleep(0.05)

        assert runner.running is True
        assert runner.start_calls == 1

        button.press()
        for _ in range(20):
            if runner.stop_calls == 1:
                break
            await asyncio.sleep(0.05)

        assert runner.running is False
        assert runner.stop_calls == 1
        assert any(state.session_state == "recording" for state in view.states)
    finally:
        stop_event.set()
        await asyncio.wait_for(task, timeout=2.0)

    assert button.closed is True


@pytest.mark.asyncio
async def test_service_mode_waits_for_button_release_before_next_toggle(
    workspace_tmp_path,
):
    controller, mic, spk, runner, fake_input, registry = build_controller(
        workspace_tmp_path, FailingInput()
    )
    button = HoldServiceButton()
    view = RecordingView()
    controller = ConsoleController(
        microphone=mic,
        speaker=spk,
        registry=registry,
        session_runner=runner,
        view=view,
        button=button,
        input_provider=FailingInput(),
        preferences_path=workspace_tmp_path / "prefs.json",
    )
    stop_event = asyncio.Event()

    task = asyncio.create_task(
        controller.run_service(stop_event=stop_event, poll_interval=0.01)
    )
    try:
        await asyncio.sleep(0.05)

        button.press()
        await asyncio.sleep(0.4)

        assert runner.start_calls == 1
        assert runner.stop_calls == 0
        assert runner.running is True

        button.release()
        await asyncio.sleep(0.15)

        button.press()
        for _ in range(20):
            if runner.stop_calls == 1:
                break
            await asyncio.sleep(0.05)

        assert runner.stop_calls == 1
    finally:
        button.release()
        stop_event.set()
        await asyncio.wait_for(task, timeout=2.0)

    assert button.closed is True


def test_lcd_state_uses_idle_result_when_record_state_is_stale(workspace_tmp_path):
    controller, mic, spk, runner, fake_input, registry = build_controller(
        workspace_tmp_path
    )
    controller._record_state = "await_close"
    runner.running = False
    runner._status = RunnerStatus("idle", "Session stopped")

    assert controller._determine_lcd_state() == "answer_stopped"


def test_lcd_state_does_not_treat_open_output_stream_as_active_playback(
    workspace_tmp_path,
):
    controller, mic, spk, runner, fake_input, registry = build_controller(
        workspace_tmp_path
    )
    controller._record_state = "await_close"
    runner.running = True
    runner._status = RunnerStatus("stopping", "Stopping session...")
    spk._is_playing = True
    spk._pending_blocks = 0
    spk._pending_frames = 0
    spk._recent_activity = False

    assert controller._determine_lcd_state() == "waiting_for_response"


def test_lcd_state_respects_pending_frames_when_audio_is_still_draining(
    workspace_tmp_path,
):
    controller, mic, spk, runner, fake_input, registry = build_controller(
        workspace_tmp_path
    )
    controller._record_state = "ready"
    runner.running = False
    runner._status = RunnerStatus("idle", "Session stopped")
    spk._pending_frames = 256

    assert controller._determine_lcd_state() == "answer_playing"
