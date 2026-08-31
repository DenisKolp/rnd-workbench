import numpy as np
import sys
import threading
from types import SimpleNamespace

from voice_assistant.audio import (
    AudioPlayer,
    BargeInDetector,
    PlaybackLimiter,
    PlaybackReference,
    UtteranceDetector,
    rms,
)
from voice_assistant.config import AudioConfig


def block(level: float, size: int = 480) -> np.ndarray:
    return np.full(size, level, dtype=np.float32)


def test_rms() -> None:
    assert abs(rms(block(0.1)) - 0.1) < 1e-5


def test_detector_emits_utterance_after_silence() -> None:
    config = AudioConfig(
        block_ms=30,
        pre_roll_ms=60,
        silence_ms=90,
        min_utterance_ms=90,
        max_utterance_s=2,
    )
    detector = UtteranceDetector(config, threshold=0.05)
    sequence = [block(0.0)] * 2 + [block(0.1)] * 4 + [block(0.0)] * 3
    results = [detector.feed(item) for item in sequence]
    utterance = next(item for item in results if item is not None)
    assert utterance.size >= 8 * 480


def test_detector_rejects_short_click() -> None:
    config = AudioConfig(
        block_ms=30,
        pre_roll_ms=60,
        silence_ms=60,
        min_utterance_ms=120,
        max_utterance_s=2,
    )
    detector = UtteranceDetector(config, threshold=0.05)
    results = [detector.feed(item) for item in [block(0.1), block(0.0), block(0.0)]]
    assert all(item is None for item in results)


def test_barge_in_ignores_speaker_echo_then_detects_near_voice() -> None:
    config = AudioConfig(
        block_ms=30,
        barge_in_grace_ms=90,
        barge_in_trigger_ms=90,
        barge_in_playback_min_rms=0.03,
        barge_in_echo_multiplier=1.8,
    )
    detector = BargeInDetector(config, threshold=0.01)

    assert not any(detector.feed(block(0.02), speaker_active=True) for _ in range(7))
    results = [detector.feed(block(0.05), speaker_active=True) for _ in range(3)]
    assert results == [False, False, True]


def test_barge_in_can_interrupt_while_model_is_thinking() -> None:
    config = AudioConfig(block_ms=30, barge_in_trigger_ms=90)
    detector = BargeInDetector(config, threshold=0.01)
    results = [detector.feed(block(0.03), speaker_active=False) for _ in range(3)]
    assert results == [False, False, True]


def test_barge_in_tracks_louder_tts_from_playback_reference() -> None:
    config = AudioConfig(
        block_ms=30,
        barge_in_grace_ms=60,
        barge_in_trigger_ms=90,
        barge_in_playback_min_rms=0.03,
        barge_in_echo_multiplier=1.6,
    )
    detector = BargeInDetector(config, threshold=0.01)

    assert not any(
        detector.feed(block(0.02), speaker_active=True, speaker_level=0.10)
        for _ in range(3)
    )
    assert not any(
        detector.feed(block(0.04), speaker_active=True, speaker_level=0.25)
        for _ in range(6)
    )
    results = [
        detector.feed(block(0.15), speaker_active=True, speaker_level=0.25)
        for _ in range(3)
    ]
    assert results == [False, False, True]


def test_barge_in_playback_floor_blocks_delayed_self_echo() -> None:
    config = AudioConfig(
        block_ms=30,
        barge_in_grace_ms=60,
        barge_in_trigger_ms=90,
        barge_in_playback_min_rms=0.032,
        barge_in_echo_multiplier=1.8,
    )
    detector = BargeInDetector(config, threshold=0.006)

    # The playback reference can briefly expire between streamed TTS chunks.
    # Low-level acoustic echo must not become a barge-in candidate then.
    assert not any(
        detector.feed(block(0.025), speaker_active=True, speaker_level=0.0)
        for _ in range(70)
    )

    results = [
        detector.feed(block(0.08), speaker_active=True, speaker_level=0.0)
        for _ in range(3)
    ]
    assert results == [False, False, True]


def test_default_barge_in_guard_survives_two_seconds_of_streamed_tts() -> None:
    config = AudioConfig()
    detector = BargeInDetector(config, threshold=0.006)

    # A quiet TTS onset is followed by a louder passage. With the production
    # defaults neither passage may be mistaken for the user after ~2 seconds.
    echo = [block(0.018)] * 20 + [block(0.029)] * 50
    assert not any(
        detector.feed(item, speaker_active=True, speaker_level=0.12)
        for item in echo
    )

    near_voice = [
        detector.feed(block(0.10), speaker_active=True, speaker_level=0.12)
        for _ in range(10)
    ]
    assert near_voice[:-1] == [False] * 9
    assert near_voice[-1]


def test_playback_reference_holds_recent_peak_across_streaming_gap(monkeypatch) -> None:
    now = [10.0]
    monkeypatch.setattr("voice_assistant.audio.time.monotonic", lambda: now[0])
    reference = PlaybackReference()
    reference.update(block(0.02), 16_000)
    reference.update(block(0.08), 16_000)

    now[0] += 0.49
    assert abs(reference.recent_level() - 0.08) < 1e-5

    now[0] += 0.02
    assert reference.recent_level() == 0.0


def test_playback_limiter_avoids_hard_clipping() -> None:
    limiter = PlaybackLimiter(output_gain=0.9, ceiling=0.94)
    source = np.array([2.0, -2.0, np.nan, np.inf], dtype=np.float32)

    processed = limiter.process(source)

    assert np.isfinite(processed).all()
    assert float(np.max(np.abs(processed))) <= 0.94001
    assert processed[0] > 0
    assert processed[1] < 0


def test_audio_player_stops_inside_long_chunk(monkeypatch) -> None:
    cancelled = threading.Event()
    started: list[bool] = []
    playback_levels: list[float] = []

    class FakeStream:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs
            self.writes: list[np.ndarray] = []
            self.aborted = False

        def start(self) -> None:
            pass

        def write(self, samples: np.ndarray) -> None:
            self.writes.append(samples)
            cancelled.set()

        def abort(self) -> None:
            self.aborted = True

        def stop(self) -> None:
            pass

        def close(self) -> None:
            pass

    stream = FakeStream()
    fake_sounddevice = SimpleNamespace(OutputStream=lambda **kwargs: stream)
    monkeypatch.setitem(sys.modules, "sounddevice", fake_sounddevice)

    AudioPlayer(output_gain=1.0).play(
        [(np.ones(16_000, dtype=np.float32), 16_000)],
        cancel_event=cancelled,
        on_start=lambda: started.append(True),
        on_block=lambda samples, _rate: playback_levels.append(rms(samples)),
    )

    assert started == [True]
    assert len(stream.writes) == 2
    assert len(stream.writes[0]) == 480
    assert len(stream.writes[1]) == 96
    assert stream.writes[0][0, 0] == 0
    assert stream.writes[1][-1, 0] == 0
    assert float(np.max(np.abs(stream.writes[0]))) <= 0.94001
    assert len(playback_levels) == 1
    assert 0 < playback_levels[0] < 0.94
    assert stream.aborted
