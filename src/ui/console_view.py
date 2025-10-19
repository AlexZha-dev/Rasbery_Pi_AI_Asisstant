from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from infrastructure.device_registry import DeviceInfo


@dataclass
class ConsoleState:
    tabs: List[str]
    active_tab: int
    session_state: str
    session_message: str
    selected_mic: Optional[int]
    selected_speaker: Optional[int]
    mic_devices: List[DeviceInfo]
    speaker_devices: List[DeviceInfo]
    record_action: str
    message: Optional[str] = None


class ConsoleView:
    def render(self, state: ConsoleState) -> None:
        print("\n" + "=" * 60)
        print(" Raspberry Pi Audio Console ")
        print("=" * 60)
        self._render_tabs(state.tabs, state.active_tab)
        print(f"Session: {state.session_state} {state.session_message}".rstrip())
        if state.message:
            print(f"Status: {state.message}")
        instructions = "Keys: 1=Prev Tab, 3=Next Tab, 2=Accept/Toggle, q=Quit"
        print(instructions)
        print("-" * 60)
        active_tab = state.tabs[state.active_tab].lower()
        if active_tab == "record":
            self._render_record(state)
        elif active_tab == "microphone":
            self._render_devices(
                "Input Devices",
                state.mic_devices,
                state.selected_mic,
                column="max_input_channels",
            )
        elif active_tab == "speaker":
            self._render_devices(
                "Output Devices",
                state.speaker_devices,
                state.selected_speaker,
                column="max_output_channels",
            )
        print("=" * 60)

    def _render_tabs(self, tabs: List[str], active_tab: int) -> None:
        labels = []
        for idx, tab in enumerate(tabs):
            if idx == active_tab:
                labels.append(f"[{tab.upper()}]")
            else:
                labels.append(f" {tab.lower()} ")
        print(" | ".join(labels))

    def _render_record(self, state: ConsoleState) -> None:
        print(f"Press 2 to {state.record_action} with the current devices.")
        print(f"Microphone device index: {state.selected_mic}")
        print(f"Speaker device index:    {state.selected_speaker}")

    def _render_devices(
        self,
        title: str,
        devices: List[DeviceInfo],
        selected: Optional[int],
        column: str,
    ) -> None:
        print(f"{title}:")
        if not devices:
            print("  No devices available.")
            return
        header = f"{'Idx':>4}  {'Name':30}  {'Channels':>8}  Current"
        print(header)
        for dev in devices:
            channels = getattr(dev, column)
            current_marker = "<--" if selected == dev.index else ""
            name = (dev.name[:27] + "...") if len(dev.name) > 30 else dev.name
            print(f"{dev.index:>4}  {name:30}  {channels:>8}  {current_marker}")
        print("Press 2 and enter a device index to select.")
