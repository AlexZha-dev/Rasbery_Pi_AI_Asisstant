import asyncio

from exceptions.lcd_exceptions import LCDException
from infrastructure.lcd_layer import LCDDisplay


async def main():
    lcd = LCDDisplay()
    try:
        lcd.set_backlight("blue")
        lcd.write_line(0, "Привет, мир!")
        await asyncio.sleep(1)

        lcd.write_char(1, 0, "A")
        await asyncio.sleep(1)

        await lcd.write_scrolling("Это длинная строка для прокрутки", row=1, delay=0.2)
        lcd.scroll("up")
    except LCDException as e:
        print(f"[ОШИБКА LCD] {e}")


if __name__ == "__main__":
    asyncio.run(main())
