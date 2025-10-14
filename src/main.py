import time

import sounddevice as sd

from exceptions.audio_exceptions import AudioError
from infrastructure.microphone_interface import MicrophoneInterface
from infrastructure.speaker_interface import SpeakerInterface

if __name__ == "__main__":
    # Демонстрация "инъекции" зависимостей и базовой работы.
    # Этот main предназначен как тестовый сценарий и не выполняет продолжительной записи/воспроизведения.

    mic = MicrophoneInterface(samplerate=16000, channels=1, blocksize=1024)
    spk = SpeakerInterface(samplerate=16000, channels=1, blocksize=1024)

    print("Initial system devices (input, output):", sd.default.device)
    print("Initial mic stored device:", mic._initial_input_device)
    print("Initial spk stored device:", spk._initial_output_device)

    try:
        # Запускаем оба стрима
        print("Starting microphone and speaker...")
        mic.start_recording()
        spk.start_output()

        # Пытаемся получить несколько блоков и тут же проиграть их обратно (loopback test)
        start_time = time.time()
        got = 0
        while time.time() - start_time < 5.0:  # собираем данные 5 секунд
            samples = mic.get_samples(blocking=False)
            if samples is not None:
                got += 1
                try:
                    spk.play(samples)
                except AudioError as e:
                    print("Play error:", e)
            time.sleep(0.01)

        print(f"Captured blocks: {got}")

    except AudioError as e:
        print("AudioError:", e)
    except Exception as e:
        print("Exception:", e)
    finally:
        print("Stopping...")
        mic.stop_recording()
        spk.stop_output()
        print("Stopped")
