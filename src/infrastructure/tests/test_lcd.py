import pytest

from exceptions.lcd_exceptions import LCDException, LCDRowError
from infrastructure.lcd_layer import LCDDisplay


class FakeSMBus:
    """Minimal SMBus stub that records commands destined for the LCD."""

    COLS = 16

    def __init__(self) -> None:
        self.lines = [" " * self.COLS, " " * self.COLS]
        self.pointer = 0
        self.rgb = (0, 0, 0)
        self.windows = []

        self._current_window = None
        self._color_registers = {0x04: 0, 0x03: 0, 0x02: 0}

    def write_byte_data(self, address: int, register: int, value: int) -> None:
        if address == LCDDisplay.DISPLAY_RGB_ADDR:
            self._handle_rgb(register, value)
            return

        if address != LCDDisplay.DISPLAY_TEXT_ADDR:
            raise AssertionError(f"Unexpected device address {address}")

        if register == 0x80:
            self._handle_command(value)
        elif register == 0x40:
            self._handle_data(value)
        else:
            raise AssertionError(f"Unexpected register {register:#x}")

    def close(self) -> None:
        return

    def _handle_rgb(self, register: int, value: int) -> None:
        if register in (0x04, 0x03, 0x02):
            self._color_registers[register] = value & 0xFF
            self.rgb = (
                self._color_registers[0x04],
                self._color_registers[0x03],
                self._color_registers[0x02],
            )

    def _handle_command(self, command: int) -> None:
        if command == 0x01:
            self.lines = [" " * self.COLS, " " * self.COLS]
            self.pointer = 0
            self._current_window = None
            return

        if command == 0x80:
            self.pointer = 0
            self._current_window = []
            return

        if command == 0xC0:
            self.pointer = self.COLS
            return

    def _handle_data(self, value: int) -> None:
        char = chr(value & 0xFF)
        idx = max(self.pointer, 0)
        line_idx = 0 if idx < self.COLS else 1
        col_idx = idx % self.COLS

        row_chars = list(self.lines[line_idx])
        row_chars[col_idx] = char
        self.lines[line_idx] = "".join(row_chars)

        if self._current_window is not None:
            self._current_window.append(char)
            if len(self._current_window) == self.COLS * 2:
                self.windows.append("".join(self._current_window))
                self._current_window = None

        self.pointer += 1


def test_display_text_sets_color_and_wraps():
    bus = FakeSMBus()
    lcd = LCDDisplay(bus=bus)

    lcd.display_text("Hello from Raspberry Pi!", line=0, color="warning")

    assert bus.lines[0] == "Hello from Raspb"
    assert bus.lines[1].startswith("erry Pi!")
    assert bus.rgb == (255, 255, 0)


def test_display_text_line_validation():
    bus = FakeSMBus()
    lcd = LCDDisplay(bus=bus)

    with pytest.raises(LCDRowError):
        lcd.display_text("Invalid", line=3)


def test_display_text_unknown_color():
    bus = FakeSMBus()
    lcd = LCDDisplay(bus=bus)

    with pytest.raises(LCDException):
        lcd.display_text("Color", color="magenta")


def test_scroll_creates_expected_windows():
    bus = FakeSMBus()
    lcd = LCDDisplay(bus=bus)

    payload = "This message is definitely longer than thirty two characters."
    lcd.display_text_with_scroll(payload, color="info", delay=0.0)

    expected_windows = len(payload) + LCDDisplay.COLS - (
        LCDDisplay.COLS * LCDDisplay.ROWS
    ) + 1

    assert len(bus.windows) == expected_windows
    assert all(len(window) == LCDDisplay.COLS * LCDDisplay.ROWS for window in bus.windows)
    assert bus.rgb == (128, 0, 255)


def test_clear_blanks_the_display():
    bus = FakeSMBus()
    lcd = LCDDisplay(bus=bus)

    lcd.display_text("Occupied line", line=0, color="success")
    lcd.clear()

    assert bus.lines == [" " * LCDDisplay.COLS, " " * LCDDisplay.COLS]
