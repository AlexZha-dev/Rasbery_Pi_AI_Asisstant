import logging

import pytest

from ui.lcd_view import LCDView


class DummyLCD:
    def __init__(self) -> None:
        self.display_calls = []
        self.cleared = False

    def display_text(self, text: str, line: int = 0, color: str = "success") -> None:
        self.display_calls.append((text, line, color))

    def clear(self) -> None:
        self.cleared = True


def test_show_state_updates_display():
    dummy = DummyLCD()
    view = LCDView(lcd=dummy)

    view.show_state("recording")

    assert dummy.display_calls == [("Recording...", 0, "success")]


def test_unknown_state_logs_warning(caplog: pytest.LogCaptureFixture):
    dummy = DummyLCD()
    view = LCDView(lcd=dummy)

    with caplog.at_level(logging.WARNING):
        view.show_state("mystery_state")

    assert "Unknown LCD state" in caplog.text
    text, _, color = dummy.display_calls[-1]
    assert text.startswith("Mystery State")
    assert color == "info"


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ("waiting_for_recording", "Ready for recording..."),
        ("recording_active", "Recording..."),
        ("sending", "Sending audio..."),
        ("waiting_for_response", "Waiting for response..."),
        ("answer_playing", "Playing response..."),
        ("answer_stopped", "Stopped"),
        ("answer_ended", "Done"),
    ],
)
def test_specific_states_render_expected_messages(state, expected):
    dummy = DummyLCD()
    view = LCDView(lcd=dummy)

    view.show_state(state)

    assert dummy.display_calls[-1][0] == expected


def test_clear_invokes_lcd_clear():
    dummy = DummyLCD()
    view = LCDView(lcd=dummy)

    view.clear()

    assert dummy.cleared is True


def test_initialization_failure_logs_warning(caplog: pytest.LogCaptureFixture):
    def failing_factory():
        raise OSError("missing device")

    with caplog.at_level(logging.WARNING):
        view = LCDView(lcd_factory=failing_factory)

    assert "LCD display not found. Continuing without LCD output." in caplog.text

    # Should not raise when the hardware is unavailable.
    view.show_state("recording")  # no exception expected
