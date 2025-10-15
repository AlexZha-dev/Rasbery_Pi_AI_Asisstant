import json
import queue

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

    def set_output_device(self, index: int):
        self.device = index


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

    async def stop(self):
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


class StubRegistry(DeviceRegistry):
    def __init__(self):  # type: ignore[no-untyped-def]
        self.refresh_calls = 0
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
async def test_controller_toggle_session(tmp_path):
    controller, mic, spk, runner, fake_input, registry = build_controller(tmp_path)
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
async def test_controller_selects_microphone(tmp_path):
    fake_input = FakeInput()
    controller, mic, spk, runner, fake_input, registry = build_controller(
        tmp_path, fake_input
    )
    await controller.handle_command("3")  # to microphone tab
    fake_input.push("1")
    await controller.handle_command("2")
    assert mic.device == 1
    state = controller.get_state()
    assert state.selected_mic == 1
    assert "device 1" in (state.message or "").lower()
    saved = json.loads((tmp_path / "prefs.json").read_text())
    assert saved["mic_device"] == 1


@pytest.mark.asyncio
async def test_controller_rejects_invalid_device(tmp_path):
    fake_input = FakeInput()
    controller, mic, spk, runner, fake_input, registry = build_controller(
        tmp_path, fake_input
    )
    await controller.handle_command("3")  # microphone tab
    fake_input.push("99")
    await controller.handle_command("2")
    state = controller.get_state()
    assert "no input device" in (state.message or "").lower()
    assert mic.device == 0  # unchanged


@pytest.mark.asyncio
async def test_controller_refresh_skips_on_record_tab(tmp_path):
    controller, mic, spk, runner, fake_input, registry = build_controller(tmp_path)
    initial_calls = registry.refresh_calls
    await controller.handle_command("2")  # start session
    controller.get_state()
    assert (
        registry.refresh_calls == initial_calls
    )  # no refresh while recording tab active
    await controller.handle_command("3")  # move to microphone tab
    controller.get_state()
    assert registry.refresh_calls > initial_calls
