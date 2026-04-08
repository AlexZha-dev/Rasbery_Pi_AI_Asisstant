from __future__ import annotations

import asyncio

from application.session_runner import SessionRunner
from bootstrap.runtime import create_client_runtime
from controllers.console_controller import ConsoleController
from infrastructure.button_interface import ButtonInterface
from ui.lcd_view import LCDView


async def main() -> None:
    runtime = create_client_runtime(sample_rate=16000, channels=1, blocksize=1024)
    microphone = runtime.microphone
    speaker = runtime.speaker
    registry = runtime.registry

    def session_factory():
        return runtime.create_session()

    def request_stop():
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
        runtime.speaker_adapter.close()
    print("[Console] Console shutdown complete.")


if __name__ == "__main__":
    asyncio.run(main())
