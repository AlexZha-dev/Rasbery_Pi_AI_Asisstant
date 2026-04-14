from __future__ import annotations

import asyncio
import contextlib
import logging
import signal

from application.session_runner import SessionRunner
from bootstrap.runtime import create_client_runtime
from controllers.console_controller import ConsoleController
from infrastructure.button_interface import ButtonInterface
from ui.console_view import ConsoleState
from ui.lcd_view import LCDView


class ServiceLogView:
    """Compact logger-backed view for headless service mode."""

    def __init__(self, logger: logging.Logger) -> None:
        self._logger = logger

    def render(self, state: ConsoleState) -> None:
        parts = [
            f"state={state.session_state}",
            f"session={state.session_message}",
            f"action={state.record_action}",
            f"mic={state.selected_mic}",
            f"speaker={state.selected_speaker}",
        ]
        if state.message:
            parts.append(f"message={state.message}")
        self._logger.info(" | ".join(parts))


def _install_signal_handlers(
    stop_event: asyncio.Event, logger: logging.Logger
) -> None:
    loop = asyncio.get_running_loop()

    def request_shutdown(sig: signal.Signals) -> None:
        if stop_event.is_set():
            return
        logger.info("Received %s, shutting down service.", sig.name)
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, request_shutdown, sig)


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    logger = logging.getLogger("service_main")

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
    lcd_view = LCDView(logger=logging.getLogger("ui.lcd_view"))
    controller = ConsoleController(
        microphone=microphone,
        speaker=speaker,
        registry=registry,
        session_runner=runner,
        view=ServiceLogView(logger),
        lcd_view=lcd_view,
        button=button,
    )

    stop_event = asyncio.Event()
    _install_signal_handlers(stop_event, logger)

    state = controller.get_state()
    logger.info(
        "Service ready. button_enabled=%s lcd_available=%s mic=%s speaker=%s",
        button.is_enabled,
        lcd_view.is_available,
        state.selected_mic,
        state.selected_speaker,
    )

    try:
        await controller.run_service(stop_event=stop_event)
    finally:
        with contextlib.suppress(Exception):
            button.close()
        with contextlib.suppress(Exception):
            microphone.stop_recording()
        runtime.speaker_adapter.close()
        logger.info("Service shutdown complete.")


if __name__ == "__main__":
    asyncio.run(main())
