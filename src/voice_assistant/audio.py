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


def trailing_silence_seconds(
    audio: np.ndarray,
    *,
    sample_rate: int,
    block_ms: int,
    threshold: float,
    limit_ms: int,
) -> float:
    """Estimate how long VAD waited after the last voiced input block.

    ``Microphone.listen`` returns only after end-of-turn silence has already
    elapsed. Subtracting that known tail makes the pilot metric represent the
    user's end of speech instead of the later moment at which VAD returned.
    """

    samples = np.asarray(audio, dtype=np.float32).reshape(-1)
    if samples.size == 0 or sample_rate <= 0 or block_ms <= 0 or limit_ms <= 0:
        return 0.0
    block_size = max(1, sample_rate * block_ms // 1000)
    silent_samples = 0
    for end in range(samples.size, 0, -block_size):
        start = max(0, end - block_size)
        block = samples[start:end]
        if rms(block) >= threshold:
            break
        silent_samples += int(block.size)
        if silent_samples * 1000 >= limit_ms * sample_rate:
            break
    return min(silent_samples / sample_rate, limit_ms / 1000)


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


class PushToTalkDurationExceededError(TimeoutError):
    """Raised when push-to-talk remains held beyond its wall-clock limit."""


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
            # Do not seed the echo baseline from the transition block. The
            # user can begin talking at exactly the same time as playback;
            # treating that block as echo would raise the threshold above the
            # user's voice and make an early interruption impossible.
            self.echo_level = self.base_threshold
            self.echo_gain = 0.0
            self._candidate_blocks = 0
        self._speaker_active = speaker_active

        if not speaker_active:
            trigger_level = self.base_threshold
        else:
            self._speaker_blocks += 1
            in_grace = self._speaker_blocks <= self._blocks(
                self.config.barge_in_grace_ms
            )
            reference_echo = (
                speaker_level * self.echo_gain
                if speaker_level is not None and speaker_level > 0
                else 0.0
            )
            # Before an echo ratio has been learned, use a conservative
            # reference-derived floor. It prevents a loud playback onset from
            # looking like speech without suppressing a close-mic voice.
            unlearned_reference = 0.0
            if in_grace and self.echo_gain == 0.0:
                if speaker_level is not None and speaker_level > 0:
                    unlearned_reference = speaker_level * 0.25
                else:
                    unlearned_reference = (
                        self.config.barge_in_playback_min_rms * 1.5
                    )
            trigger_level = max(
                self.base_threshold * 1.75,
                self.config.barge_in_playback_min_rms,
                self.echo_level * self.config.barge_in_echo_multiplier,
                reference_echo * self.config.barge_in_echo_multiplier,
                unlearned_reference,
            )

            if in_grace and level < trigger_level:
                # Grace is a calibration window, not an unconditional mute.
                # Only blocks that are below the current speech threshold may
                # train the echo guard; a likely user utterance proceeds to the
                # normal consecutive-block trigger below.
                self.echo_level = max(self.echo_level, level)
                self._update_echo_gain(level, speaker_level)
                self._candidate_blocks = 0
                return False

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
    _threshold_cache: dict[str, float] = {}
    _threshold_cache_lock = threading.Lock()

    def __init__(self, config: AudioConfig) -> None:
        self.config = config
        self._queue: queue.Queue[np.ndarray] = queue.Queue(maxsize=512)
        self._stream = None
        self.detector: UtteranceDetector | None = None
        self._adaptive_noise_level: float | None = None
        self._adaptive_background_blocks = 0

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
        fallback = self._cached_or_initial_threshold()
        # A user often starts speaking immediately after pressing the mic.
        # Treat a loud/unstable sample as voiced rather than teaching VAD that
        # the first utterance is room noise. Explicit CLI calibration still
        # benefits from a genuinely quiet sample; otherwise it degrades safely
        # to the fast adaptive threshold used by the UI.
        low = float(np.percentile(levels, 20))
        quiet_limit = max(self.config.min_rms * 2.0, 0.009)
        stable = noise_floor <= max(low * 2.5, low + 0.004)
        quiet = noise_floor <= quiet_limit
        threshold = (
            max(self.config.min_rms, noise_floor * self.config.noise_multiplier)
            if quiet and stable
            else fallback
        )
        self.detector = UtteranceDetector(self.config, threshold)
        self._adaptive_noise_level = min(
            noise_floor if quiet and stable else threshold / self.config.noise_multiplier,
            threshold / self.config.noise_multiplier,
        )
        self._remember_threshold(threshold)
        return threshold

    def start_adaptive(self) -> float:
        """Start listening immediately without consuming the first utterance.

        The compact UI must become receptive within 300 ms.  A conservative
        cached/default threshold is installed synchronously, then clearly
        sub-threshold blocks refine it in the background.  Candidate speech is
        never folded into the noise estimate.
        """

        threshold = self._cached_or_initial_threshold()
        self.detector = UtteranceDetector(self.config, threshold)
        self._adaptive_noise_level = threshold / self.config.noise_multiplier
        self._adaptive_background_blocks = 0
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
            self._adapt_threshold(block)
            result = self.detector.feed(block)
            if result is not None:
                return result

    def record_until_release(
        self,
        release_event: threading.Event,
        *,
        cancel_event: threading.Event | None = None,
        max_duration_s: float | None = None,
    ) -> np.ndarray | None:
        """Capture push-to-talk audio until the key is released.

        Unlike :meth:`listen`, this path deliberately does not use end-of-turn
        VAD: the held key is the turn boundary.  A short queue timeout keeps the
        release-to-transcription hand-off responsive, while the duration cap
        prevents a lost key-up event from recording indefinitely.
        """

        # A push-to-talk Microphone is opened specifically for this key press.
        # Keep blocks that arrived between stream start and this method call:
        # draining here would clip the beginning of a user who speaks
        # immediately after pressing the key.
        duration_s = (
            self.config.max_utterance_s
            if max_duration_s is None
            else max(0.1, float(max_duration_s))
        )
        max_blocks = max(1, int(np.ceil(duration_s * 1000 / self.config.block_ms)))
        deadline = time.monotonic() + duration_s
        blocks: list[np.ndarray] = []
        while True:
            if cancel_event is not None and cancel_event.is_set():
                return None
            if release_event.is_set():
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                # Resolve a key-up racing the deadline as a normal release. If
                # the key is still held, fail explicitly: returning the audio
                # here would turn a lost key-up/device stall into unintended
                # text insertion.
                if release_event.is_set():
                    break
                raise PushToTalkDurationExceededError(
                    f"Превышен лимит записи {duration_s:g} с"
                )
            block = self.read_block(timeout=min(0.05, remaining))
            if block is not None and len(blocks) < max_blocks:
                blocks.append(np.asarray(block, dtype=np.float32).reshape(-1).copy())

        if cancel_event is not None and cancel_event.is_set():
            return None
        # The OS can deliver key-up in the middle of the input device's current
        # 30 ms block. Give that in-flight callback one block plus scheduler
        # slack to arrive; an immediate non-blocking drain clips final
        # consonants on some devices. This bounded grace adds under 50 ms and
        # never waits when cancellation discards the utterance.
        tail_deadline = time.monotonic() + self.config.block_ms / 1000 + 0.015
        while len(blocks) < max_blocks:
            if cancel_event is not None and cancel_event.is_set():
                return None
            remaining = tail_deadline - time.monotonic()
            if remaining <= 0:
                break
            tail = self.read_block(timeout=min(remaining, 0.02))
            if tail is not None:
                blocks.append(np.asarray(tail, dtype=np.float32).reshape(-1).copy())
        if not blocks:
            return None
        audio = np.concatenate(blocks)
        minimum_samples = max(1, int(self.config.sample_rate * 0.08))
        return audio if audio.size >= minimum_samples else None

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

    def _cache_key(self) -> str:
        return str(self.config.input_device) if self.config.input_device is not None else "default"

    def _cached_or_initial_threshold(self) -> float:
        with self._threshold_cache_lock:
            cached = self._threshold_cache.get(self._cache_key())
        if cached is not None:
            return max(self.config.min_rms, cached)
        return max(self.config.min_rms * 2.0, 0.009)

    def _remember_threshold(self, threshold: float) -> None:
        with self._threshold_cache_lock:
            self._threshold_cache[self._cache_key()] = float(threshold)

    def _adapt_threshold(self, block: np.ndarray) -> None:
        detector = self.detector
        if detector is None or detector.state.listening:
            return
        level = rms(block)
        threshold = detector.state.threshold
        # Only unmistakable background can train the estimator. A first word
        # above the current threshold reaches the utterance detector unchanged.
        if level >= max(self.config.min_rms, threshold * 0.8):
            return
        previous = self._adaptive_noise_level
        self._adaptive_noise_level = (
            level if previous is None else previous * 0.94 + level * 0.06
        )
        target = max(
            self.config.min_rms,
            self._adaptive_noise_level * self.config.noise_multiplier,
        )
        detector.state.threshold = threshold * 0.9 + target * 0.1
        self._adaptive_background_blocks += 1
        if self._adaptive_background_blocks >= self._blocks_for_cache():
            self._remember_threshold(detector.state.threshold)
            self._adaptive_background_blocks = 0

    def _blocks_for_cache(self) -> int:
        return max(1, int(np.ceil(1000 / self.config.block_ms)))


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
