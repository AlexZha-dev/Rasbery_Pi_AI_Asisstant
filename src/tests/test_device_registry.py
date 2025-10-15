from infrastructure.device_registry import DeviceRegistry


class StubSoundDevice:
    def __init__(self):
        self._devices = [
            {
                "name": "Mic Only",
                "max_input_channels": 2,
                "max_output_channels": 0,
            },
            {
                "name": "Combo",
                "max_input_channels": 1,
                "max_output_channels": 2,
            },
            {
                "name": "Speaker",
                "max_input_channels": 0,
                "max_output_channels": 2,
            },
        ]

    def query_devices(self):
        return list(self._devices)


def test_device_registry_filters_input_output():
    registry = DeviceRegistry(sounddevice_module=StubSoundDevice())

    snapshot = registry.snapshot()
    assert [d.index for d in snapshot.input_devices] == [0, 1]
    assert [d.index for d in snapshot.output_devices] == [1, 2]

    assert registry.is_valid_input(0)
    assert not registry.is_valid_input(2)
    assert registry.is_valid_output(2)
    assert registry.default_input_index() == 0
    assert registry.default_output_index() == 1
