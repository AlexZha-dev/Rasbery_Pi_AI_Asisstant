from __future__ import annotations

import time
from typing import Dict, Optional, Tuple

from smbus2 import SMBus

from exceptions.lcd_exceptions import LCDException, LCDRowError


class LCDDisplay:
    """Driver for 16x2 RGB backlit LCD modules that use the Grove I2C protocol."""

    DISPLAY_TEXT_ADDR = 0x3E
    DISPLAY_RGB_ADDR = 0x62
    COLS = 16
    ROWS = 2

    _COLOR_MAP: Dict[str, Tuple[int, int, int]] = {
        "error": (255, 0, 0),
        "warning": (255, 255, 0),
        "success": (0, 255, 0),
        "info": (128, 0, 255),
    }

    def __init__(self, bus: Optional[SMBus] = None) -> None:
        """
        Create an LCD driver and power up the controller.

        By default SMBus(1) is opened which is the I2C bus exposed on Raspberry Pi
        headers. Tests may pass a fake bus implementation via the *bus* argument.
        """
        self._bus = bus or SMBus(1)
        self._init_controller()

    def display_text(self, text: str, line: int = 0, color: str = "success") -> None:
        """
        Render *text* starting at *line* (0 or 1). The backlight is set according to
        the named *color*. Text longer than 16 characters continues on the following
        line. Anything past the physical screen (32 characters total) is discarded.
        """
        if line not in (0, 1):
            raise LCDRowError(f"Line index must be 0 or 1, got {line}")

        safe_text = (text or "").replace("\r", " ").replace("\n", " ")
        self._apply_color(color)

        visible_chars = self.COLS * (self.ROWS - line)
        truncated = safe_text[:visible_chars]

        # Pad to full lines so stale characters are not left on screen.
        padded = truncated.ljust(self.COLS * (self.ROWS - line))

        try:
            for offset in range(self.ROWS - line):
                start = offset * self.COLS
                chunk = padded[start : start + self.COLS]
                if line + offset >= self.ROWS:
                    break

                ddram_address = 0x80 if line + offset == 0 else 0xC0
                self._text_command(ddram_address)
                for char in chunk:
                    self._write_char(char)
        except OSError as exc:
            raise LCDException(f"Failed to render text: {exc}") from exc

    def display_text_with_scroll(
        self, text: str, color: str = "info", delay: float = 0.3
    ) -> None:
        """
        Show *text* and scroll it horizontally when the payload exceeds 32 symbols.
        The marquee moves left with *delay* seconds between shifts.
        """
        safe_text = (text or "").replace("\r", " ").replace("\n", " ")
        self._apply_color(color)

        window_size = self.COLS * self.ROWS
        if len(safe_text) <= window_size:
            self.display_text(safe_text, line=0, color=color)
            return

        # Keep a trailing gap so the final characters scroll fully off screen.
        padded = safe_text + " " * self.COLS

        try:
            for index in range(len(padded) - window_size + 1):
                window = padded[index : index + window_size]
                self._write_window(window)
                time.sleep(max(delay, 0.0))
        except OSError as exc:
            raise LCDException(f"Failed to scroll text: {exc}") from exc

    def clear(self) -> None:
        """Clear both lines of the display."""
        try:
            self._text_command(0x01)
            time.sleep(0.002)
        except OSError as exc:
            raise LCDException(f"Failed to clear LCD: {exc}") from exc

    def close(self) -> None:
        """Release the underlying SMBus handle."""
        if self._bus is not None:
            try:
                self._bus.close()
            finally:
                self._bus = None

    def _init_controller(self) -> None:
        """Send the standard Grove LCD power-up sequence."""
        self._text_command(0x01)  # clear
        time.sleep(0.002)
        self._text_command(0x08 | 0x04)  # display on, no cursor
        self._text_command(0x28)  # 2 lines, 5x8 font
        self._set_rgb(0, 255, 0)

    def _apply_color(self, name: str) -> None:
        """Translate a symbolic color name into RGB values."""
        try:
            rgb = self._COLOR_MAP[name.lower()]
        except KeyError as exc:
            raise LCDException(f"Unsupported color keyword: {name}") from exc
        self._set_rgb(*rgb)

    def _write_window(self, payload: str) -> None:
        """Write exactly 32 characters across both display lines."""
        upper = payload[: self.COLS].ljust(self.COLS)
        lower = payload[self.COLS : self.COLS * 2].ljust(self.COLS)

        self._text_command(0x80)
        for char in upper:
            self._write_char(char)

        self._text_command(0xC0)
        for char in lower:
            self._write_char(char)

    def _set_rgb(self, red: int, green: int, blue: int) -> None:
        """Program the RGB backlight driver."""
        self._bus.write_byte_data(self.DISPLAY_RGB_ADDR, 0x00, 0x00)
        self._bus.write_byte_data(self.DISPLAY_RGB_ADDR, 0x01, 0x00)
        self._bus.write_byte_data(self.DISPLAY_RGB_ADDR, 0x08, 0xAA)
        self._bus.write_byte_data(self.DISPLAY_RGB_ADDR, 0x04, red & 0xFF)
        self._bus.write_byte_data(self.DISPLAY_RGB_ADDR, 0x03, green & 0xFF)
        self._bus.write_byte_data(self.DISPLAY_RGB_ADDR, 0x02, blue & 0xFF)

    def _text_command(self, command: int) -> None:
        """Send a command to the LCD controller."""
        self._bus.write_byte_data(self.DISPLAY_TEXT_ADDR, 0x80, command & 0xFF)

    def _write_char(self, char: str) -> None:
        """Write a single character to the current DDRAM position."""
        encoded = ord(char) if char else 0x20
        self._bus.write_byte_data(self.DISPLAY_TEXT_ADDR, 0x40, encoded & 0xFF)


if __name__ == "__main__":
    lcd = LCDDisplay()
    try:
        lcd.display_text("System ready", line=0, color="success")
        time.sleep(2)
        lcd.display_text_with_scroll(
            "Raspberry Pi LCD demo - scrolling text showcase.", color="info", delay=0.2
        )
        time.sleep(2)
    finally:
        lcd.clear()
        lcd.close()
