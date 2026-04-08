import numpy as np

from infrastructure.speaker_output import SpeakerInterface


def test_output_callback_stitches_short_chunks_without_padding():
    speaker = SpeakerInterface(samplerate=16000, channels=1, blocksize=1024)
    with speaker._lock:
        speaker._is_playing = True

    first = np.ones((512, 1), dtype=np.float32)
    second = np.full((512, 1), 0.25, dtype=np.float32)

    speaker.play(first)
    speaker.play(second)

    out = np.zeros((1024, 1), dtype=np.float32)
    speaker._output_callback(out, 1024, None, None)

    assert np.allclose(out[:512], 1.0)
    assert np.allclose(out[512:], 0.25)
    assert speaker.pending_frames() == 0


def test_output_callback_preserves_remainder_between_callbacks():
    speaker = SpeakerInterface(samplerate=16000, channels=1, blocksize=1024)
    with speaker._lock:
        speaker._is_playing = True

    payload = np.arange(1500, dtype=np.float32).reshape(-1, 1) / 1500.0
    speaker.play(payload)

    first = np.zeros((1024, 1), dtype=np.float32)
    second = np.zeros((1024, 1), dtype=np.float32)

    speaker._output_callback(first, 1024, None, None)
    assert np.allclose(first, payload[:1024])
    assert speaker.pending_frames() == 476

    speaker._output_callback(second, 1024, None, None)
    assert np.allclose(second[:476], payload[1024:])
    assert np.allclose(second[476:], 0.0)
    assert speaker.pending_frames() == 0


def test_normalize_chunk_duplicates_mono_to_stereo_output():
    speaker = SpeakerInterface(samplerate=16000, channels=1, blocksize=1024)
    speaker._stream_channels = 2

    payload = np.array([[0.25], [-0.5], [0.75]], dtype=np.float32)

    normalized = speaker._normalize_chunk(payload)

    assert normalized.shape == (3, 2)
    assert np.allclose(normalized[:, 0], payload[:, 0])
    assert np.allclose(normalized[:, 1], payload[:, 0])
