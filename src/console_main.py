from __future__ import annotations

import logging
import time
import traceback
from dataclasses import dataclass
from typing import Dict, Optional

from ui.console_view import ConsoleState, ConsoleView
from ui.lcd_view import LCDView

LOGGER = logging.getLogger(__name__)


@dataclass
class SimpleState:
    session_state: str
    session_message: str = ""
    record_action: str = "record"


class ConsoleMain:
    """Entry point coordinating console output with the optional LCD view."""

    _STATE_MESSAGES: Dict[str, str] = {
        "waiting_for_input": "Ready for recording...",
        "recording": "Recording in progress...",
        "waiting_for_response": "Waiting for response...",
        "response_received": "Response received!",
        "error": "Error occurred!",
    }

    def __init__(
        self,
        console_view: Optional[ConsoleView] = None,
        lcd_view: Optional[LCDView] = None,
    ) -> None:
        self._console_view = console_view or ConsoleView()
        self._lcd_view = lcd_view or LCDView()
        self._current_state = SimpleState(session_state="waiting_for_input")

    def run(self) -> None:
        """Simulate a simple application lifecycle, updating both views."""
        logging.info("Starting ConsoleMain lifecycle demo.")
        lifecycle = [
            "waiting_for_input",
            "recording",
            "waiting_for_response",
            "response_received",
            "error",
        ]
        try:
            for state in lifecycle:
                self._set_state(state)
                time.sleep(1.5)
        except KeyboardInterrupt:
            LOGGER.info("Lifecycle interrupted by user.")
        except Exception:  # pragma: no cover - demo path
            self._handle_exception()
        finally:
            self._lcd_view.clear()
            LOGGER.info("ConsoleMain lifecycle finished.")

    def _set_state(self, state: str) -> None:
        self._current_state.session_state = state
        message = self._STATE_MESSAGES.get(state, state.replace("_", " ").title())
        self._current_state.session_message = message
        record_action = "stop" if state == "recording" else "record"
        self._current_state.record_action = record_action
        self._console_view.show_state(
            state,
            message=message,
            record_action=record_action,
        )
        self._lcd_view.show_state(state)

    def _handle_exception(self) -> None:
        LOGGER.exception("Unexpected error during lifecycle.")
        traceback.print_exc()
        self._lcd_view.show_state("error")
        error_state = ConsoleState(
            tabs=["Record", "Microphone", "Speaker"],
            active_tab=0,
            session_state="error",
            session_message="- Error occurred!",
            selected_mic=None,
            selected_speaker=None,
            mic_devices=[],
            speaker_devices=[],
            record_action=self._current_state.record_action,
            message="An unexpected error occurred. See logs.",
        )
        self._console_view.render(error_state)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
    ConsoleMain().run()
