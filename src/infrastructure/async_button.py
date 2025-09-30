import asyncio

from gpiozero import Button as GPIOButton


class AsyncButton:
    def __init__(self, pin: int):
        self.button = GPIOButton(pin)
        self.__pressed_event = asyncio.Event()
        self.__released_event = asyncio.Event()

        self.button.when_activated = self.__on_pressed
        self.button.when_deactivated = self.__on_released

    def _on_pressed(self):
        self.__pressed_event.set()
        self.__released_event.clear()

    def _on_released(self):
        self.__pressed_event.clear()
        self.__released_event.set()

    async def wait_for_press(self):
        return await self.__pressed_event.wait()

    async def wait_for_release(self):
        return await self.__released_event.wait()

    def is_pressed(self) -> bool:
        return self.button.is_pressed
