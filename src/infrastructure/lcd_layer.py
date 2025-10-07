import asyncio

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
        self.backlight_color = color
        print(f"[LCD] Подсветка установлена: {color}")

    def write_char(self, row: int, col: int, char: str):
        """Пишет один символ по координатам"""
        if not (0 <= row < self.rows and 0 <= col < self.cols):
            print("[LCD] Ошибка: координаты вне диапазона")
            return
        line = list(self.buffer[row])
        line[col] = char[0]
        self.buffer[row] = "".join(line)
        self._render()

    def write_line(self, row: int, text: str):
        """Пишет строку на выбранную строку"""
        if not (0 <= row < self.rows):
            print("[LCD] Ошибка: строка вне диапазона")
            return
        text = text[:self.cols].ljust(self.cols)
        self.buffer[row] = text
        self._render()

    async def write_scrolling(self, text: str, row: int, delay: float = 0.3):
        """Прокручивает длинный текст"""
        if not (0 <= row < self.rows):
            print("[LCD] Ошибка: строка вне диапазона")
            return

        if len(text) <= self.cols:
            self.write_line(row, text)
            return

        for i in range(len(text) - self.cols + 1):
            self.buffer[row] = text[i:i + self.cols]
            self._render()
            await asyncio.sleep(delay)

    def scroll(self, direction: str = "up"):
        """Прокручивает дисплей вверх/вниз"""
        if direction == "up":
            self.buffer.pop(0)
            self.buffer.append(" " * self.cols)
        elif direction == "down":
            self.buffer.pop()
            self.buffer.insert(0, " " * self.cols)
        else:
            print("[LCD] Неверное направление прокрутки")
            return
        self._render()

    def _render(self):
        """Отображает текущее состояние дисплея"""
        print("\n" + "=" * (self.cols + 4))
        print(f" LCD (подсветка: {self.backlight_color})")
        for line in self.buffer:
            print(f"| {line} |")
        print("=" * (self.cols + 4) + "\n")