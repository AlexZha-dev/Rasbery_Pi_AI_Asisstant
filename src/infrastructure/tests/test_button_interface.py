import time

import pytest

from infrastructure import button_interface as button_module
from infrastructure.button_interface import ButtonInterface


class FakeButton:
    instances = []

    def __init__(self, pin, pull_up=True, bounce_time=None):
        self.pin = pin
        self.pull_up = pull_up
        self.bounce_time = bounce_time
        self.closed = False
        self.is_pressed = False
        self.wait_calls = []
        self.wait_results = []
        self.release_calls = []
        self.release_results = []
        FakeButton.instances.append(self)

    def wait_for_press(self, timeout=None):
        self.wait_calls.append(timeout)
        if self.wait_results:
            return self.wait_results.pop(0)
        return False

    def wait_for_release(self, timeout=None):
        self.release_calls.append(timeout)
        if self.release_results:
            return self.release_results.pop(0)
        return True

    def close(self):
        self.closed = True


class FakePolledButton:
    def __init__(self, pin, pull_up=True, bounce_time=None):
        self.pin = pin
        self.pull_up = pull_up
        self.bounce_time = bounce_time
        self.closed = False
        self._press_sequence = [False, False, True]
        self._index = 0

    @property
    def is_pressed(self):
        value = self._press_sequence[min(self._index, len(self._press_sequence) - 1)]
        self._index = min(self._index + 1, len(self._press_sequence) - 1)
        return value

    def wait_for_release(self, timeout=None):
        start = time.monotonic()
        while True:
            if not self.is_pressed:
                return True
            if timeout is not None and time.monotonic() - start >= timeout:
                return False
            time.sleep(0.0)

    def close(self):
        self.closed = True


def test_button_disabled_when_gpio_missing(monkeypatch):
    monkeypatch.setattr(button_module, "_GpioZeroButton", None)

    button = ButtonInterface(pin=17)

    assert button.is_enabled is False
    assert button.is_pressed() is False

    start = time.monotonic()
    assert button.wait_for_press(timeout=0.05) is False
    duration = time.monotonic() - start
    assert duration >= 0.05


def test_button_reports_press_with_fake_button():
    FakeButton.instances.clear()
    button = ButtonInterface(pin=18, button_factory=FakeButton)

    assert button.is_enabled is True
    fake = FakeButton.instances[-1]
    assert fake.pin == 18
    assert fake.pull_up is True
    assert fake.bounce_time == pytest.approx(0.05)

    fake.is_pressed = True
    assert button.is_pressed() is True

    fake.wait_results.append(True)
    assert button.wait_for_press(timeout=0.1) is True
    assert fake.wait_calls[-1] == pytest.approx(0.1)

    fake.release_results.append(True)
    assert button.wait_for_release(timeout=0.05) is True
    assert fake.release_calls[-1] == pytest.approx(0.05)

    button.close()
    assert fake.closed is True


def test_polling_wait_for_press(monkeypatch):
    button = ButtonInterface(pin=22, button_factory=FakePolledButton)

    monkeypatch.setattr(ButtonInterface, "_POLL_INTERVAL", 0.0)

    assert button.wait_for_press(timeout=0.05) is True
    button.close()
