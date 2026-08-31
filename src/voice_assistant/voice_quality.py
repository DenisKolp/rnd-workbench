from __future__ import annotations

import re
from typing import Any
import unicodedata

import numpy as np


def normalized_words(text: str) -> list[str]:
    """Normalize Russian/English text for a small local WER check."""

    normalized = unicodedata.normalize("NFKC", text).casefold().replace("ё", "е")
    return re.findall(r"[\w]+", normalized, flags=re.UNICODE)


def word_error_rate(reference: str, hypothesis: str) -> float:
    expected = normalized_words(reference)
    actual = normalized_words(hypothesis)
    if not expected:
        return 0.0 if not actual else 1.0
    previous = list(range(len(actual) + 1))
    for row, expected_word in enumerate(expected, start=1):
        current = [row]
        for column, actual_word in enumerate(actual, start=1):
            current.append(
                min(
                    current[column - 1] + 1,
                    previous[column] + 1,
                    previous[column - 1] + (expected_word != actual_word),
                )
            )
        previous = current
    return previous[-1] / len(expected)


def analyze_audio(
    audio: np.ndarray,
    sample_rate: int,
    *,
    clip_threshold: float = 0.999,
    click_step_threshold: float = 0.35,
) -> dict[str, Any]:
    """Return content-free signal checks useful for repeatable TTS QA."""

    samples = np.asarray(audio, dtype=np.float32).reshape(-1)
    if sample_rate <= 0:
        raise ValueError("sample_rate должен быть больше нуля")
    finite = np.isfinite(samples)
    safe = np.where(finite, samples, 0.0)
    absolute = np.abs(safe)
    steps = np.abs(np.diff(safe)) if safe.size > 1 else np.empty(0, dtype=np.float32)
    return {
        "audio_seconds": round(safe.size / sample_rate, 3),
        "peak": round(float(np.max(absolute)) if safe.size else 0.0, 6),
        "rms": round(float(np.sqrt(np.mean(safe * safe))) if safe.size else 0.0, 6),
        "dc_offset": round(float(np.mean(safe)) if safe.size else 0.0, 7),
        "clipped_samples": int(np.count_nonzero(absolute >= clip_threshold)),
        "nonfinite_samples": int(np.count_nonzero(~finite)),
        "max_sample_step": round(float(np.max(steps)) if steps.size else 0.0, 6),
        "click_candidates": int(np.count_nonzero(steps >= click_step_threshold)),
    }
