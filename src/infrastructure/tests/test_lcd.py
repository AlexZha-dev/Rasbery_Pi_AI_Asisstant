import pytest

from exceptions.lcd_exceptions import LCDCoordinateError, LCDException
from infrastructure.lcd_layer import LCDDisplay


@pytest.fixture
def lcd():
    """Создаёт чистый LCD-дисплей перед каждым тестом"""
    return LCDDisplay(rows=2, cols=16)


# === Инициализация и базовые методы ===


def test_init_creates_correct_buffer(lcd):
    assert lcd.rows == 2
    assert lcd.cols == 16
    assert len(lcd.buffer) == 2
    assert all(len(line) == 16 for line in lcd.buffer)
    assert lcd.backlight_color == "white"


def test_clear_resets_buffer(lcd):
    lcd.buffer[0] = "Hello, World!   "
    lcd.clear()
    assert lcd.buffer == [" " * 16, " " * 16]


# === Подсветка ===


def test_set_backlight_valid(lcd, capsys):
    lcd.set_backlight("blue")
    captured = capsys.readouterr()
    assert "blue" in captured.out
    assert lcd.backlight_color == "blue"


def test_set_backlight_invalid_type(lcd):
    with pytest.raises(LCDException):
        lcd.set_backlight(123)  # не строка


# === Запись символов ===


def test_write_char_valid(lcd):
    lcd.write_char(0, 0, "A")
    assert lcd.buffer[0][0] == "A"


def test_write_char_invalid_coords(lcd):
    with pytest.raises(LCDException) as excinfo:
        lcd.write_char(5, 20, "B")
    assert isinstance(excinfo.value.__cause__, (LCDCoordinateError, type(None)))


def test_write_char_invalid_input(lcd):
    with pytest.raises(LCDException):
        lcd.write_char(0, 0, "")  # пустая строка


# === Запись строк ===


def test_write_line_valid(lcd):
    lcd.write_line(1, "Hello")
    assert lcd.buffer[1].startswith("Hello")
    assert len(lcd.buffer[1]) == 16


def test_write_line_invalid_row(lcd):
    with pytest.raises(LCDException):
        lcd.write_line(10, "Oops")


def test_write_line_invalid_type(lcd):
    with pytest.raises(LCDException):
        lcd.write_line(0, None)


# === Асинхронная прокрутка ===


@pytest.mark.asyncio
async def test_write_scrolling_short_text(lcd):
    """Если текст ≤ 16 символов — просто записывает строку"""
    await lcd.write_scrolling("Short text", row=0)
    assert "Short text" in lcd.buffer[0]


@pytest.mark.asyncio
async def test_write_scrolling_long_text(lcd):
    """Длинный текст должен прокручиваться"""
    text = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    await lcd.write_scrolling(text, row=1, delay=0.01)
    # После прокрутки в буфере останется последняя часть текста
    assert lcd.buffer[1].strip() in text


@pytest.mark.asyncio
async def test_write_scrolling_invalid_row(lcd):
    with pytest.raises(LCDException):
        await lcd.write_scrolling("Test", row=5)


# === Прокрутка вверх/вниз ===


def test_scroll_up(lcd):
    lcd.write_line(0, "LINE 1")
    lcd.write_line(1, "LINE 2")
    lcd.scroll("up")
    assert lcd.buffer[0].startswith("LINE 2")
    assert lcd.buffer[1].strip() == ""


def test_scroll_down(lcd):
    lcd.write_line(0, "LINE 1")
    lcd.write_line(1, "LINE 2")
    lcd.scroll("down")
    assert lcd.buffer[1].startswith("LINE 1")
    assert lcd.buffer[0].strip() == ""


def test_scroll_invalid_direction(lcd):
    with pytest.raises(LCDException):
        lcd.scroll("sideways")
