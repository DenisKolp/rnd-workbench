from __future__ import annotations

import argparse
import json
from pathlib import Path
import threading
import time

import numpy as np
import soundfile as sf

from voice_assistant.backends import OmniVoiceFastTTS
from voice_assistant.config import Config


def main() -> None:
    parser = argparse.ArgumentParser(description="Benchmark the local OmniVoice Metal backend")
    parser.add_argument("--config", type=Path, default=Path("config.toml"))
    parser.add_argument("--output", type=Path, default=Path("artifacts/omnivoice-fast-test.wav"))
    parser.add_argument(
        "--text",
        default=(
            "Здравствуйте! Я RnD Workbench. Я могу общаться с вами голосом и текстом, "
            "помогать с исследованиями, документами и рабочим контекстом."
        ),
    )
    args = parser.parse_args()

    config = Config.load(args.config)
    if config.tts.backend != "omnivoice_fast":
        raise RuntimeError("В конфигурации не выбран omnivoice_fast")
    backend = OmniVoiceFastTTS(config.tts)

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

        cancel_event = threading.Event()
        cancel_timer = threading.Timer(0.2, cancel_event.set)
        cancel_started = time.perf_counter()
        cancel_timer.start()
        list(
            backend.synthesize(
                "Это длинная контрольная реплика для проверки немедленного перебивания. "
                * 8,
                cancel_event=cancel_event,
            )
        )
        cancel_timer.cancel()
        cancellation_seconds = time.perf_counter() - cancel_started

        recovery_started = time.perf_counter()
        recovery_stream = backend.synthesize("После перебивания я снова готов к работе.")
        next(recovery_stream)
        recovery_first_audio_seconds = time.perf_counter() - recovery_started
        recovery_stream.close()
    finally:
        backend.close()

    if not chunks:
        raise RuntimeError("OmniVoice не вернул аудио")
    audio = np.concatenate(chunks)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    sf.write(args.output, audio, sample_rate, subtype="PCM_24")

    peak = float(np.max(np.abs(audio)))
    clipped_samples = int(np.count_nonzero(np.abs(audio) >= 0.999))
    duration = audio.size / sample_rate
    report = {
        "backend": "OmniVoice Q8 / GGML Metal",
        "steps": config.tts.steps,
        "load_seconds": round(load_seconds, 3),
        "first_audio_seconds": round(first_audio_seconds or synth_seconds, 3),
        "synthesis_seconds": round(synth_seconds, 3),
        "audio_seconds": round(duration, 3),
        "rtf": round(synth_seconds / duration, 3),
        "peak": round(peak, 6),
        "clipped_samples": clipped_samples,
        "chunks": len(chunks),
        "cancellation_seconds": round(cancellation_seconds, 3),
        "recovery_first_audio_seconds": round(recovery_first_audio_seconds, 3),
        "output": str(args.output.resolve()),
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
