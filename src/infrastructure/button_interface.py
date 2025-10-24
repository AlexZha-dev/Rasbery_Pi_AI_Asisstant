from __future__ import annotations

import logging
import time
from typing import Optional

try:
    from gpiozero import Button as _GpioZeroButton  # type: ignore
except ImportError:  # pragma: no cover - dependency absent on dev hosts
    _GpioZeroButton = None  # type: ignore


class ButtonInterface:
    """Wrapper for a single gpiozero Button with graceful degradation.

    When gpiozero (or the underlying GPIO hardware) is unavailable the
    interface stays inert so the rest of the application can continue running.
    """

    _POLL_INTERVAL = 0.02

    def __init__(
        self,
        pin: int,
        *,
        pull_up: bool = True,
        bounce_time: float = 0.05,
        button_factory=None,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self._pin = pin
        self._pull_up = bool(pull_up)
        self._bounce_time = max(0.0, float(bounce_time))
        self._logger = logger or logging.getLogger(__name__)

        if button_factory is not None:
            self._factory = button_factory
        else:
            self._factory = _GpioZeroButton

        self._button: Optional[object] = None
        self._enabled = False
        self._configure()

    # --------------------------------------------------------------------- setup
    def _configure(self) -> None:
        factory = self._factory
        if factory is None:
            self._logger.warning(
                "gpiozero.Button not available; button on pin %s disabled.", self._pin
            )
            return

        try:
            self._button = factory(
                self._pin,
                pull_up=self._pull_up,
                bounce_time=self._bounce_time or None,
            )
            self._enabled = True
        except Exception as exc:  # pragma: no cover - defensive hardware path
            self._logger.warning(
                "Failed to initialise button on pin %s: %s", self._pin, exc
            )
            self._enabled = False
            self._button = None

    # --------------------------------------------------------------------- state
    @property
    def is_enabled(self) -> bool:
        """Return True when the underlying gpiozero button is active."""
        return self._enabled

    # ------------------------------------------------------------------- readings
    def is_pressed(self) -> bool:
        """Return True if the button is currently pressed."""
        if not self._enabled or self._button is None:
            return False
        try:
            value = getattr(self._button, "is_pressed", False)
            return bool(value)
        except Exception as exc:  # pragma: no cover - unexpected hardware error
            self._logger.debug(
                "Failed to read button state on pin %s: %s",
                self._pin,
                exc,
                exc_info=exc,
            )
            return False

    def wait_for_press(self, timeout: Optional[float] = None) -> bool:
        """Block until the button is pressed or *timeout* elapses."""
        if not self._enabled or self._button is None:
            if timeout:
                time.sleep(max(0.0, float(timeout)))
            return False

        wait_fn = getattr(self._button, "wait_for_press", None)
        if callable(wait_fn):
            try:
                return bool(wait_fn(timeout=timeout))
            except TypeError:
                # Some gpiozero versions accept positional timeout only
                try:
                    return bool(wait_fn(timeout))
                except Exception:
                    pass
            except Exception as exc:  # pragma: no cover
                self._logger.debug(
                    "gpiozero wait_for_press failed on pin %s: %s",
                    self._pin,
                    exc,
                    exc_info=exc,
                )

        deadline = (
            None if timeout is None else time.monotonic() + max(0.0, float(timeout))
        )
        while True:
            if self.is_pressed():
                return True
            if deadline is not None and time.monotonic() >= deadline:
                return False
            time.sleep(self._POLL_INTERVAL)

    def wait_for_release(self, timeout: Optional[float] = None) -> bool:
        """Block until the button is released or *timeout* elapses."""
        if not self._enabled or self._button is None:
            if timeout:
                time.sleep(max(0.0, float(timeout)))
            return False

        release_fn = getattr(self._button, "wait_for_release", None)
        if callable(release_fn):
            try:
                return bool(release_fn(timeout=timeout))
            except TypeError:
                try:
                    return bool(release_fn(timeout))
                except Exception:
                    pass
            except Exception as exc:  # pragma: no cover
                self._logger.debug(
                    "gpiozero wait_for_release failed on pin %s: %s",
                    self._pin,
                    exc,
                    exc_info=exc,
                )

        deadline = (
            None if timeout is None else time.monotonic() + max(0.0, float(timeout))
        )
        while True:
            if not self.is_pressed():
                return True
            if deadline is not None and time.monotonic() >= deadline:
                return False
            time.sleep(self._POLL_INTERVAL)

    # ------------------------------------------------------------------- cleanup
    def close(self) -> None:
        """Release hardware resources."""
        if not self._enabled or self._button is None:
            return
        try:
            close_fn = getattr(self._button, "close", None)
            if callable(close_fn):
                close_fn()
        finally:
            self._enabled = False
            self._button = None

    def __del__(self) -> None:  # pragma: no cover - best-effort cleanup
        try:
            self.close()
        except Exception:
            pass
