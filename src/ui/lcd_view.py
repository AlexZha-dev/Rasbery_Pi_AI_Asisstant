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
        reconnect_interval: float = 5.0,
    ) -> None:
        self._logger = logger or logging.getLogger(__name__)
        self._lcd_factory = lcd_factory or LCDDisplay
        self._reconnect_interval = max(0.0, float(reconnect_interval))
        self._last_init_attempt: float = 0.0
        self._availability_warned = False
        self._lcd = lcd
        if self._lcd is None:
            self._ensure_display(force=True)

    @property
    def is_available(self) -> bool:
        return self._lcd is not None

    def show_state(self, state: str) -> None:
        """Update the LCD with a short status message that matches *state*."""
        message, color = self._resolve_template(state)
        if not self._ensure_display():
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

    def close(self) -> None:
        lcd = self._lcd
        if lcd is None:
            return
        try:
            close_fn = getattr(lcd, "close", None)
            if callable(close_fn):
                close_fn()
        except Exception as exc:
            self._logger.warning("Failed to close LCD: %s", exc)
        finally:
            self._lcd = None

    def _ensure_display(self, *, force: bool = False) -> bool:
        if self._lcd is not None:
            return True
        if self._lcd_factory is None:
            self._warn_no_hardware(_LCD_IMPORT_ERROR)
            return False

        now = time.monotonic()
        if (
            not force
            and self._last_init_attempt > 0.0
            and (now - self._last_init_attempt) < self._reconnect_interval
        ):
            return False
        self._last_init_attempt = now

        try:
            self._lcd = self._lcd_factory()
        except Exception as exc:  # pragma: no cover
            self._warn_no_hardware(exc)
            return False

        if self._availability_warned:
            self._logger.info("LCD display became available.")
            self._availability_warned = False
        return True

    def _warn_no_hardware(self, exc: Optional[Exception]) -> None:
        if not self._availability_warned:
            self._logger.warning(
                "LCD display not found. Continuing without LCD output."
            )
            self._availability_warned = True
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
