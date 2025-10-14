import asyncio
import numpy as np

class MicrophoneAsyncAdapter:
    """Адаптер для потокового микрофона, чтобы читать данные асинхронно."""
    def __init__(self, microphone):
        self.mic = microphone

    async def get_samples(self):
        """Периодически проверяет очередь микрофона, возвращая блоки."""
        samples = self.mic.get_samples(blocking=False)
        if samples is not None:
            return samples
        await asyncio.sleep(0.01)
        return None


class SpeakerAsyncAdapter:
    """Адаптер для потокового спикера, чтобы безопасно вызывать play() из async."""
    def __init__(self, speaker):
        self.spk = speaker

    async def play(self, samples: np.ndarray):
        """Вызов в executor, чтобы не блокировать event loop."""
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self.spk.play, samples)
