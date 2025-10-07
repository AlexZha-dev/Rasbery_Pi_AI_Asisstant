import asyncio

from infrastructure.lcd_layer import LCDDisplay


async def main():
    lcd = LCDDisplay()
    lcd.set_backlight("green")
    lcd.write_line(0, "Привет, мир!")
    await asyncio.sleep(1)

    lcd.write_char(1, 0, "A")
    await asyncio.sleep(1)

    await lcd.write_scrolling("Это длинная строка для прокрутки", row=1, delay=1)
    lcd.scroll("up")

if __name__ == "__main__":
    asyncio.run(main())
