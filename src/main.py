import asyncio

from infrastructure.async_button import AsyncButton


async def main():
    button = AsyncButton(2)
    await button.wait_for_press()
    print("Button Pressed")
    await button.wait_for_release()
    print("Button Released")


if __name__ == "__main__":
    asyncio.run(main())
