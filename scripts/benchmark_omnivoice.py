from __future__ import annotations

import argparse
import json
from pathlib import Path
import threading
import time

import numpy as np
import soundfile as sf

from voice_assistant.audio import AudioPlayer
from voice_assistant.backends import OmniVoiceFastTTS
from voice_assistant.config import Config
from voice_assistant.voice_quality import analyze_audio, word_error_rate


def measure_barge_in_recovery(
    backend: OmniVoiceFastTTS,
    player: AudioPlayer,
    *,
    cancel_after_playback_start_seconds: float = 0.2,
    playback_start_timeout_seconds: float = 30.0,
    worker_stop_timeout_seconds: float = 5.0,
) -> dict[str, float]:
    """Measure the real playback cancellation and next-turn recovery path.

    The clock starts immediately before setting ``cancel_event``.  The player
    metric is recorded only after ``AudioPlayer.play`` has returned; that
    method aborts/stops and closes its output stream before returning.  The
    recovery metric deliberately keeps the same origin, so it includes both
    release of the interrupted synthesis/playback worker and generation of
    the first audio block for the follow-up turn.
    """

    cancel_event = threading.Event()
    playback_started = threading.Event()
    playback_finished = threading.Event()
    player_returned_at: list[float] = []
    worker_released_at: list[float] = []
    playback_errors: list[BaseException] = []

    def run_playback() -> None:
        try:
            player.play(
                backend.synthesize(
                    "Это длинная контрольная реплика для проверки немедленного "
                    "перебивания. "
                    * 8,
                    cancel_event=cancel_event,
                ),
                cancel_event=cancel_event,
                on_start=playback_started.set,
            )
        except BaseException as exc:
            playback_errors.append(exc)
        finally:
            # AudioPlayer guarantees that the stream was aborted/stopped and
            # closed before play() returns.
            player_returned_at.append(time.perf_counter())
            worker_released_at.append(time.perf_counter())
            playback_finished.set()

    playback_worker = threading.Thread(
        target=run_playback,
        name="omnivoice-benchmark-playback",
        daemon=True,
    )
    playback_worker.start()
    if not playback_started.wait(playback_start_timeout_seconds):
        cancel_event.set()
        playback_worker.join(worker_stop_timeout_seconds)
        if playback_errors:
            raise playback_errors[0]
        raise RuntimeError(
            "OmniVoice не начал воспроизведение для проверки перебивания"
        )

    if cancel_after_playback_start_seconds > 0:
        time.sleep(cancel_after_playback_start_seconds)
    cancel_requested_at = time.perf_counter()
    cancel_event.set()
    if not playback_finished.wait(worker_stop_timeout_seconds):
        raise RuntimeError(
            "Аудиоплеер не остановился после запроса перебивания"
        )
    playback_worker.join()
    if playback_errors:
        raise playback_errors[0]

    recovery_stream = backend.synthesize(
        "После перебивания я снова готов к работе."
    )
    try:
        next(recovery_stream)
        recovery_first_audio_at = time.perf_counter()
    finally:
        recovery_stream.close()

    return {
        "cancel_to_audio_player_stop_seconds": (
            player_returned_at[0] - cancel_requested_at
        ),
        "cancel_to_worker_release_seconds": (
            worker_released_at[0] - cancel_requested_at
        ),
        "cancel_to_recovery_first_audio_seconds": (
            recovery_first_audio_at - cancel_requested_at
        ),
        "worker_release_to_recovery_first_audio_seconds": (
            recovery_first_audio_at - worker_released_at[0]
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Benchmark the local OmniVoice Metal backend")
    parser.add_argument("--config", type=Path, default=Path("config.toml"))
    parser.add_argument("--output", type=Path, default=Path("artifacts/omnivoice-fast-test.wav"))
    parser.add_argument("--report", type=Path)
    parser.add_argument("--chunk-duration", type=float)
    parser.add_argument("--verify-stt", action="store_true")
    parser.add_argument(
        "--text",
        # Production intentionally speaks one concise complete sentence while
        # the full answer stays in chat, so the default benchmark must measure
        # that actual UX rather than a multi-sentence long-form TTS workload.
        default=(
            "Я подготовил краткий итог встречи и сохранил полный разбор в чате."
        ),
    )
    args = parser.parse_args()

    config = Config.load(args.config)
    if args.chunk_duration is not None:
        if args.chunk_duration <= 0:
            raise ValueError("--chunk-duration должен быть больше нуля")
        config.tts.chunk_duration_s = args.chunk_duration
    if config.tts.backend != "omnivoice_fast":
        raise RuntimeError("В конфигурации не выбран omnivoice_fast")
    backend = OmniVoiceFastTTS(config.tts)
    player = AudioPlayer(
        config.audio.output_device,
        output_gain=config.audio.output_gain,
    )

    load_started = time.perf_counter()
    backend.load()
    load_seconds = time.perf_counter() - load_started
    try:
        synth_started = time.perf_counter()
        first_audio_seconds = None
        chunks: list[np.ndarray] = []
        sample_rate = 24_000
        for audio, sample_rate in backend.synthesize(args.text):
            if first_audio_seconds is None:
                first_audio_seconds = time.perf_counter() - synth_started
            chunks.append(audio)
        synth_seconds = time.perf_counter() - synth_started

        interruption = measure_barge_in_recovery(backend, player)
    finally:
        backend.close()

    if not chunks:
        raise RuntimeError("OmniVoice не вернул аудио")
    audio = np.concatenate(chunks)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    sf.write(args.output, audio, sample_rate, subtype="PCM_24")

    signal = analyze_audio(audio, sample_rate)
    duration = float(signal["audio_seconds"])
    report = {
        "backend": "OmniVoice Q8 / GGML Metal",
        "input_text": args.text,
        "steps": config.tts.steps,
        "chunk_duration_seconds": config.tts.chunk_duration_s,
        "load_seconds": round(load_seconds, 3),
        "first_audio_seconds": round(first_audio_seconds or synth_seconds, 3),
        "synthesis_seconds": round(synth_seconds, 3),
        **signal,
        "rtf": round(synth_seconds / duration, 3),
        "chunks": len(chunks),
        **{name: round(value, 3) for name, value in interruption.items()},
        # Keep generated evidence portable and safe to publish.  An explicit
        # absolute --output remains explicit, while the default no longer
        # leaks a developer machine path into the JSON report.
        "output": str(args.output),
    }
    if args.verify_stt:
        from voice_assistant.backends import WhisperSTT

        stt = WhisperSTT(config.stt)
        stt.load()
        transcript = stt.transcribe(audio, sample_rate)
        report["stt_verification"] = {
            "transcript": transcript,
            "wer": round(word_error_rate(args.text, transcript), 4),
            "exact_words": word_error_rate(args.text, transcript) == 0.0,
        }
    report["slo"] = {
        "tts_rtf_lte_0_45": report["rtf"] <= 0.45,
        "zero_clipped_samples": report["clipped_samples"] == 0,
        "zero_nonfinite_samples": report["nonfinite_samples"] == 0,
        "barge_in_audio_player_stop_lte_0_25": (
            report["cancel_to_audio_player_stop_seconds"] <= 0.25
        ),
        "barge_in_recovery_first_audio_lte_2_0": (
            report["cancel_to_recovery_first_audio_seconds"] <= 2.0
        ),
    }
    if args.report is not None:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
