from __future__ import annotations

import logging
import time
from typing import Callable, Dict, Optional, Tuple

from exceptions.lcd_exceptions import LCDException

try:
    from infrastructure.lcd_layer import LCDDisplay
except ImportError as import_error:  # pragma: no cover - dependency missing at runtime
    LCDDisplay = None  # type: ignore
    _LCD_IMPORT_ERROR = import_error
else:
    _LCD_IMPORT_ERROR: Optional[Exception] = None


class LCDView:
    """View-controller that mirrors session state on a 16x2 RGB LCD panel."""

    _STATE_TEMPLATES: Dict[str, Tuple[str, str]] = {
        "waiting_for_recording": ("Ready to record", "info"),
        "waiting_for_input":     ("Ready to record", "info"),
        "recording_active":      ("Recording...", "success"),
        "recording":             ("Recording...", "success"),
        "sending":               ("Sending audio", "info"),
        "waiting_for_response":  ("Waiting reply", "warning"),
        "answer_playing":        ("Playing reply", "success"),
        "answer_stopped":        ("Stopped", "warning"),
        "answer_ended":          ("Done", "success"),
        "error":                 ("Error!", "error"),
        "response_received":     ("Reply received", "success"),
    }

    def __init__(
        self,
        lcd: Optional["LCDDisplay"] = None,
        lcd_factory: Optional[Callable[[], "LCDDisplay"]] = None,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self._logger = logger or logging.getLogger(__name__)
        self._lcd = self._initialize_display(lcd, lcd_factory)

    def show_state(self, state: str) -> None:
        """Update the LCD with a short status message that matches *state*."""
        message, color = self._resolve_template(state)
        if not self._lcd:
            return
        try:
            self._lcd.display_text(message, color=color)
        except (LCDException, OSError) as exc:
            self._logger.warning("Failed to update LCD: %s", exc)

    def clear(self) -> None:
        """Clear the contents of the LCD module."""
        if not self._lcd:
            return
        try:
            self._lcd.clear()
        except (LCDException, OSError) as exc:
            self._logger.warning("Failed to clear LCD: %s", exc)

    def _initialize_display(
        self,
        lcd: Optional["LCDDisplay"],
        lcd_factory: Optional[Callable[[], "LCDDisplay"]],
    ) -> Optional["LCDDisplay"]:
        if lcd is not None:
            return lcd

        if LCDDisplay is None:
            self._warn_no_hardware(_LCD_IMPORT_ERROR)
            return None

        factory = lcd_factory or LCDDisplay

        try:
            return factory()
        except Exception as exc:  # pragma: no cover
            self._warn_no_hardware(exc)
            return None

    def _warn_no_hardware(self, exc: Optional[Exception]) -> None:
        self._logger.warning("LCD display not found. Continuing without LCD output.")
        if exc is not None:
            self._logger.debug("LCD initialization failed: %s", exc, exc_info=exc)

    def _resolve_template(self, state: str) -> Tuple[str, str]:
        template = self._STATE_TEMPLATES.get(state)
        if template:
            return template
        self._logger.warning(
            "Unknown LCD state '%s'. Falling back to generic message.", state
        )
        sanitized = state.replace("_", " ").title()[:32] or "Status update"
        return sanitized, "info"


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    view = LCDView()
    try:
        for state in (
            "waiting_for_input",
            "recording",
            "waiting_for_response",
            "response_received",
            "error",
        ):
            view.show_state(state)
            time.sleep(1.5)
    finally:
        view.clear()
