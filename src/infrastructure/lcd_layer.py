import asyncio

from exceptions.lcd_exceptions import (
    LCDCoordinateError,
    LCDException,
    LCDRowError,
    LCDScrollDirectionError,
)


class LCDDisplay:
    def __init__(self, rows: int = 2, cols: int = 16):
        self.rows = rows
        self.cols = cols
        self.buffer = [" " * cols for _ in range(rows)]
        self.backlight_color = "white"

    def clear(self):
        """Очистка дисплея"""
        self.buffer = [" " * self.cols for _ in range(self.rows)]
        self._render()

    def set_backlight(self, color: str):
        """Установка цвета подсветки"""
        try:
            if not isinstance(color, str):
                raise ValueError("Цвет должен быть строкой")
            self.backlight_color = color
            print(f"[LCD] Подсветка установлена: {color}")
        except Exception as e:
            raise LCDException(f"Ошибка установки подсветки: {e}")

    def write_char(self, row: int, col: int, char: str):
        """Пишет один символ по координатам"""
        try:
            if not (0 <= row < self.rows and 0 <= col < self.cols):
                raise LCDCoordinateError(
                    f"Недопустимые координаты row={row}, col={col}"
                )

            if not char or not isinstance(char, str):
                raise ValueError("char должен быть непустой строкой")

            line = list(self.buffer[row])
            line[col] = char[0]
            self.buffer[row] = "".join(line)
            self._render()

        except Exception as e:
            raise LCDException(f"Ошибка при записи символа: {e}")

    def write_line(self, row: int, text: str):
        """Пишет строку на выбранную строку"""
        try:
            if not (0 <= row < self.rows):
                raise LCDRowError(f"Неверная строка: {row}")
            if not isinstance(text, str):
                raise ValueError("text должен быть строкой")

            text = text[: self.cols].ljust(self.cols)
            self.buffer[row] = text
            self._render()
        except Exception as e:
            raise LCDException(f"Ошибка при записи строки: {e}")

    async def write_scrolling(self, text: str, row: int, delay: float = 0.3):
        """Асинхронно прокручивает длинный текст"""
        try:
            if not (0 <= row < self.rows):
                raise LCDRowError(f"Неверная строка: {row}")
            if not isinstance(text, str):
                raise ValueError("text должен быть строкой")

            if len(text) <= self.cols:
                self.write_line(row, text)
                return

            for i in range(len(text) - self.cols + 1):
                self.buffer[row] = text[i : i + self.cols]
                self._render()
                await asyncio.sleep(delay)

        except Exception as e:
            raise LCDException(f"Ошибка при прокрутке текста: {e}")

    def scroll(self, direction: str = "up"):
        """Прокручивает дисплей вверх/вниз"""
        try:
            if direction == "up":
                self.buffer.pop(0)
                self.buffer.append(" " * self.cols)
            elif direction == "down":
                self.buffer.pop()
                self.buffer.insert(0, " " * self.cols)
            else:
                raise LCDScrollDirectionError(f"Неверное направление: {direction}")

            self._render()
        except Exception as e:
            raise LCDException(f"Ошибка при прокрутке: {e}")

    def _render(self):
        """Отображает текущее состояние дисплея"""
        print("\n" + "=" * (self.cols + 4))
        print(f" LCD (подсветка: {self.backlight_color})")
        for line in self.buffer:
            print(f"| {line} |")
        print("=" * (self.cols + 4) + "\n")
