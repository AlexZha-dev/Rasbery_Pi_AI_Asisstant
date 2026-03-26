from __future__ import annotations

import asyncio

from application.audio_session import AudioSession
from application.session_runner import SessionRunner
from controllers.console_controller import ConsoleController
from infrastructure.button_interface import ButtonInterface
from infrastructure.device_registry import DeviceRegistry
from infrastructure.microphone_interface import MicrophoneInterface
from infrastructure.sounds_adapters import (
    MicrophoneAsyncAdapter,
    SpeakerAsyncAdapter,
)
from infrastructure.speaker_interface import SpeakerInterface
from infrastructure.websocket_client import AudioWebSocketClient
from ui.lcd_view import LCDView


async def main() -> None:
    microphone = MicrophoneInterface(samplerate=16000, channels=1, blocksize=1024)
    speaker = SpeakerInterface(samplerate=16000, channels=1, blocksize=1024)
    mic_adapter = MicrophoneAsyncAdapter(microphone)
    spk_adapter = SpeakerAsyncAdapter(speaker)
    registry = DeviceRegistry()

    def session_factory():
        ws_client = AudioWebSocketClient()
        session = AudioSession(ws_client, mic_adapter, spk_adapter)
        return session, ws_client

    def request_stop():
        # Stop only microphone; keep speaker running to play server response
        microphone.stop_recording()

    runner = SessionRunner(
        session_factory,
        request_stop,
        playback_timeout=30.0,
        join_timeout=35.0,
    )
    button = ButtonInterface(pin=17)
    controller = ConsoleController(
        microphone=microphone,
        speaker=speaker,
        registry=registry,
        session_runner=runner,
        lcd_view=LCDView(),
        button=button,
    )
    print("[Console] Starting async console.")
    try:
        await controller.run()
    finally:
        button.close()
        spk_adapter.close()
    print("[Console] Console shutdown complete.")


if __name__ == "__main__":
    asyncio.run(main())
