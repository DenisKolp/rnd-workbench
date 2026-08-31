from __future__ import annotations

from collections import deque
from contextlib import AbstractContextManager
from dataclasses import dataclass
import queue
import sys
import threading
import time
from typing import Callable, Iterable

import numpy as np

from .config import AudioConfig


def rms(block: np.ndarray) -> float:
    audio = np.asarray(block, dtype=np.float32).reshape(-1)
    if audio.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(audio * audio) + 1e-12))


@dataclass(slots=True)
class DetectorState:
    threshold: float
    listening: bool = False


class UtteranceDetector:
    """Energy-based end-of-turn detector with pre-roll.

    It is intentionally dependency-free. The threshold is calibrated against the
    room before the main loop, which is sufficient for a close laptop microphone.
    """

    def __init__(self, config: AudioConfig, threshold: float) -> None:
        self.config = config
        self.state = DetectorState(threshold=threshold)
        self._pre_roll: deque[np.ndarray] = deque(maxlen=self._blocks(config.pre_roll_ms))
        self._utterance: list[np.ndarray] = []
        self._voiced_blocks = 0
        self._silent_blocks = 0
        self._total_blocks = 0

    def _blocks(self, milliseconds: int) -> int:
        return max(1, int(np.ceil(milliseconds / self.config.block_ms)))

    def reset(self) -> None:
        self.state.listening = False
        self._pre_roll.clear()
        self._utterance.clear()
        self._voiced_blocks = 0
        self._silent_blocks = 0
        self._total_blocks = 0

    def feed(self, block: np.ndarray) -> np.ndarray | None:
        samples = np.asarray(block, dtype=np.float32).reshape(-1).copy()
        voiced = rms(samples) >= self.state.threshold

        if not self.state.listening:
            self._pre_roll.append(samples)
            if not voiced:
                return None
            self.state.listening = True
            self._utterance.extend(self._pre_roll)
            self._pre_roll.clear()
            self._voiced_blocks = 1
            self._total_blocks = len(self._utterance)
            return None

        self._utterance.append(samples)
        self._total_blocks += 1
        if voiced:
            self._voiced_blocks += 1
            self._silent_blocks = 0
        else:
            self._silent_blocks += 1

        enough_silence = self._silent_blocks >= self._blocks(self.config.silence_ms)
        too_long = self._total_blocks >= self._blocks(int(self.config.max_utterance_s * 1000))
        if not (enough_silence or too_long):
            return None

        min_voiced = self._blocks(self.config.min_utterance_ms)
        result = np.concatenate(self._utterance) if self._voiced_blocks >= min_voiced else None
        self.reset()
        return result


class PlaybackReference:
    """Thread-safe short-term loudness reference for audio sent to speakers."""

    def __init__(self, hold_ms: int = 500) -> None:
        self.hold_s = hold_ms / 1000
        self._levels: deque[tuple[float, float]] = deque(maxlen=32)
        self._lock = threading.Lock()

    def update(self, block: np.ndarray, _sample_rate: int) -> None:
        now = time.monotonic()
        with self._lock:
            self._levels.append((now, rms(block)))
            self._prune(now)

    def recent_level(self) -> float:
        now = time.monotonic()
        with self._lock:
            self._prune(now)
            return max((level for _, level in self._levels), default=0.0)

    def _prune(self, now: float) -> None:
        cutoff = now - self.hold_s
        while self._levels and self._levels[0][0] < cutoff:
            self._levels.popleft()


class PlaybackLimiter:
    """Keep synthesized speech below full scale without hard clipping.

    Gain reduction reacts immediately to a loud block and recovers gradually,
    which avoids the flat-topped samples and pumping caused by ``np.clip``.
    """

    def __init__(
        self,
        output_gain: float = 0.86,
        ceiling: float = 0.94,
        release: float = 0.08,
    ) -> None:
        if not 0 < output_gain <= 1:
            raise ValueError("output_gain должен быть больше 0 и не больше 1")
        if not 0 < ceiling < 1:
            raise ValueError("ceiling должен быть между 0 и 1")
        if not 0 < release <= 1:
            raise ValueError("release должен быть больше 0 и не больше 1")
        self.output_gain = output_gain
        self.ceiling = ceiling
        self.release = release
        self._gain_reduction = 1.0

    def process(self, block: np.ndarray) -> np.ndarray:
        samples = np.nan_to_num(
            np.asarray(block, dtype=np.float32),
            copy=True,
            nan=0.0,
            posinf=0.0,
            neginf=0.0,
        )
        peak = float(np.max(np.abs(samples))) if samples.size else 0.0
        required_gain = (
            min(1.0, self.ceiling / (peak * self.output_gain))
            if peak > 0
            else 1.0
        )
        if required_gain < self._gain_reduction:
            self._gain_reduction = required_gain
        else:
            self._gain_reduction += (
                required_gain - self._gain_reduction
            ) * self.release
        return samples * np.float32(self.output_gain * self._gain_reduction)


class BargeInDetector:
    """Detect deliberate near-mic speech while an answer is being generated.

    During playback the detector learns the loudness of the assistant leaking
    into the microphone and requires both a separate absolute floor and a
    substantially louder signal. This is a lightweight echo guard rather than
    acoustic echo cancellation, but works well for a close laptop microphone
    and becomes very reliable on headphones.
    """

    def __init__(
        self,
        config: AudioConfig,
        threshold: float,
    ) -> None:
        self.config = config
        self.base_threshold = threshold
        self.echo_level = threshold
        self.echo_gain = 0.0
        self._candidate_blocks = 0
        self._speaker_blocks = 0
        self._speaker_active = False

    def _blocks(self, milliseconds: int) -> int:
        return max(1, int(np.ceil(milliseconds / self.config.block_ms)))

    def feed(
        self,
        block: np.ndarray,
        *,
        speaker_active: bool,
        speaker_level: float | None = None,
    ) -> bool:
        level = rms(block)

        if speaker_active and not self._speaker_active:
            self._speaker_blocks = 0
            self.echo_level = max(self.base_threshold, level)
            self.echo_gain = 0.0
            self._candidate_blocks = 0
        self._speaker_active = speaker_active

        if not speaker_active:
            trigger_level = self.base_threshold
        else:
            self._speaker_blocks += 1
            if self._speaker_blocks <= self._blocks(self.config.barge_in_grace_ms):
                self.echo_level = max(self.echo_level, level)
                self._update_echo_gain(level, speaker_level)
                self._candidate_blocks = 0
                return False
            reference_echo = (
                speaker_level * self.echo_gain
                if speaker_level is not None and speaker_level > 0
                else 0.0
            )
            trigger_level = max(
                self.base_threshold * 1.75,
                self.config.barge_in_playback_min_rms,
                self.echo_level * self.config.barge_in_echo_multiplier,
                reference_echo * self.config.barge_in_echo_multiplier,
            )

        if level >= trigger_level:
            self._candidate_blocks += 1
        else:
            self._candidate_blocks = 0
            if speaker_active:
                # Follow normal speaker leakage slowly. Candidate speech is not
                # folded into the baseline, otherwise it would mask itself.
                self.echo_level = max(
                    self.base_threshold,
                    self.echo_level * 0.99,
                    level,
                )
                self._update_echo_gain(level, speaker_level)

        return self._candidate_blocks >= self._blocks(self.config.barge_in_trigger_ms)

    def _update_echo_gain(self, microphone_level: float, speaker_level: float | None) -> None:
        if speaker_level is None or speaker_level < 0.01:
            return
        measured_gain = microphone_level / speaker_level
        self.echo_gain = max(self.echo_gain * 0.995, measured_gain)


class Microphone(AbstractContextManager["Microphone"]):
    def __init__(self, config: AudioConfig) -> None:
        self.config = config
        self._queue: queue.Queue[np.ndarray] = queue.Queue(maxsize=512)
        self._stream = None
        self.detector: UtteranceDetector | None = None

    @property
    def block_size(self) -> int:
        return self.config.sample_rate * self.config.block_ms // 1000

    def __enter__(self) -> "Microphone":
        import sounddevice as sd

        def callback(indata, frames, time_info, status) -> None:  # noqa: ANN001
            del frames, time_info
            if status:
                print(f"\n[audio] {status}", file=sys.stderr)
            try:
                self._queue.put_nowait(indata[:, 0].copy())
            except queue.Full:
                try:
                    self._queue.get_nowait()
                except queue.Empty:
                    pass

        self._stream = sd.InputStream(
            device=self.config.input_device,
            channels=1,
            samplerate=self.config.sample_rate,
            blocksize=self.block_size,
            dtype="float32",
            callback=callback,
        )
        self._stream.start()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:  # noqa: ANN001
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None

    def calibrate(self) -> float:
        self._drain()
        count = max(1, int(self.config.calibration_s * 1000 / self.config.block_ms))
        levels = [rms(self._queue.get(timeout=2.0)) for _ in range(count)]
        noise_floor = float(np.percentile(levels, 80))
        threshold = max(self.config.min_rms, noise_floor * self.config.noise_multiplier)
        self.detector = UtteranceDetector(self.config, threshold)
        return threshold

    def listen(self, cancel_event: threading.Event | None = None) -> np.ndarray | None:
        if self.detector is None:
            raise RuntimeError("Сначала вызовите calibrate()")
        while True:
            if cancel_event is not None and cancel_event.is_set():
                return None
            block = self.read_block(timeout=0.25)
            if block is None:
                continue
            result = self.detector.feed(block)
            if result is not None:
                return result

    def read_block(self, timeout: float = 0.25) -> np.ndarray | None:
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def discard_pending(self) -> None:
        """Drop audio captured while models or speakers were active."""
        self._drain()
        if self.detector is not None:
            self.detector.reset()

    def _drain(self) -> None:
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                return


class AudioPlayer:
    _PLAYBACK_BLOCK_MS = 30
    _EDGE_FADE_MS = 6
    _OUTPUT_CEILING = 0.94

    def __init__(
        self,
        output_device: int | str | None = None,
        output_gain: float = 0.86,
    ) -> None:
        self.output_device = output_device
        self.output_gain = output_gain
        # Validate configuration before opening an audio device.
        PlaybackLimiter(output_gain=output_gain, ceiling=self._OUTPUT_CEILING)

    def play(
        self,
        chunks: Iterable[tuple[np.ndarray, int]],
        cancel_event: threading.Event | None = None,
        on_start: Callable[[], None] | None = None,
        on_block: Callable[[np.ndarray, int], None] | None = None,
    ) -> None:
        import sounddevice as sd

        stream = None
        current_rate = None
        playback_started = False
        cancelled = False
        first_output_block = True
        last_output_sample: float | None = None
        limiter = PlaybackLimiter(
            output_gain=self.output_gain,
            ceiling=self._OUTPUT_CEILING,
        )
        try:
            for audio, sample_rate in chunks:
                if cancel_event is not None and cancel_event.is_set():
                    cancelled = True
                    break
                samples = np.asarray(audio, dtype=np.float32).reshape(-1, 1)
                if samples.size == 0:
                    continue
                if stream is None or current_rate != sample_rate:
                    if stream is not None:
                        self._write_fade_out(stream, last_output_sample, current_rate)
                        stream.stop()
                        stream.close()
                    stream = sd.OutputStream(
                        device=self.output_device,
                        channels=1,
                        samplerate=sample_rate,
                        dtype="float32",
                    )
                    stream.start()
                    current_rate = sample_rate
                    limiter = PlaybackLimiter(
                        output_gain=self.output_gain,
                        ceiling=self._OUTPUT_CEILING,
                    )
                    first_output_block = True
                    last_output_sample = None
                if not playback_started:
                    playback_started = True
                    if on_start is not None:
                        on_start()
                # A generated TTS chunk can be several seconds long. Small
                # writes make a voice interruption audible within ~30 ms.
                frame_count = max(1, sample_rate * self._PLAYBACK_BLOCK_MS // 1000)
                for offset in range(0, len(samples), frame_count):
                    if cancel_event is not None and cancel_event.is_set():
                        cancelled = True
                        break
                    playback_block = limiter.process(
                        samples[offset : offset + frame_count]
                    )
                    if first_output_block:
                        playback_block = self._fade_in(playback_block, sample_rate)
                        first_output_block = False
                    last_output_sample = float(playback_block[-1, 0])
                    if on_block is not None:
                        on_block(playback_block, sample_rate)
                    stream.write(playback_block)
                if cancelled:
                    break
        finally:
            if stream is not None:
                self._write_fade_out(stream, last_output_sample, current_rate)
                if cancelled and hasattr(stream, "abort"):
                    stream.abort()
                else:
                    stream.stop()
                stream.close()

    def _fade_in(self, block: np.ndarray, sample_rate: int) -> np.ndarray:
        faded = block.copy()
        fade_frames = min(len(faded), self._fade_frames(sample_rate))
        if fade_frames:
            ramp = np.linspace(0.0, 1.0, fade_frames, dtype=np.float32)
            faded[:fade_frames, 0] *= ramp
        return faded

    def _write_fade_out(
        self,
        stream: object,
        last_sample: float | None,
        sample_rate: int | None,
    ) -> None:
        if last_sample is None or sample_rate is None or abs(last_sample) < 1e-7:
            return
        fade_frames = self._fade_frames(sample_rate)
        tail = np.linspace(
            last_sample,
            0.0,
            fade_frames + 1,
            dtype=np.float32,
        )[1:].reshape(-1, 1)
        try:
            stream.write(tail)  # type: ignore[attr-defined]
        except Exception:
            # Preserve the original playback/cancellation outcome if the audio
            # device disappears while the short safety tail is being written.
            pass

    def _fade_frames(self, sample_rate: int) -> int:
        return max(1, sample_rate * self._EDGE_FADE_MS // 1000)



def list_audio_devices() -> str:
    import sounddevice as sd

    return str(sd.query_devices())
