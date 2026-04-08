from infrastructure.device_registry import DeviceRegistry


class StubSoundDevice:
    class Defaults:
        def __init__(self, device):
            self.device = device

    def __init__(self, default_device=(0, 1)):
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
        self.default = self.Defaults(default_device)

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


def test_device_registry_prefers_system_default_devices():
    registry = DeviceRegistry(sounddevice_module=StubSoundDevice(default_device=[1, 2]))

    assert registry.default_input_index() == 1
    assert registry.default_output_index() == 2


def test_device_registry_falls_back_when_system_default_is_invalid():
    registry = DeviceRegistry(sounddevice_module=StubSoundDevice(default_device=[99, 98]))

    assert registry.default_input_index() == 0
    assert registry.default_output_index() == 1


def test_device_registry_resolves_devices_by_stable_signature():
    registry = DeviceRegistry(sounddevice_module=StubSoundDevice(default_device=[99, 98]))

    mic_signature = registry.input_signature(1)
    speaker_signature = registry.output_signature(2)

    assert mic_signature is not None
    assert speaker_signature is not None
    assert registry.resolve_input_signature(mic_signature) == 1
    assert registry.resolve_output_signature(speaker_signature) == 2
