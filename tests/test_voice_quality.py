from __future__ import annotations

import numpy as np

from voice_assistant.voice_quality import analyze_audio, normalized_words, word_error_rate


def test_russian_word_normalization_ignores_case_punctuation_and_yo() -> None:
    assert normalized_words("Ёлка, ГОТОВА!") == ["елка", "готова"]
    assert word_error_rate("Ёлка готова.", "елка, готова") == 0.0


def test_word_error_rate_counts_substitution_insertion_and_deletion() -> None:
    assert word_error_rate("один два три", "один пять три") == 1 / 3
    assert word_error_rate("один два", "один новый два") == 1 / 2
    assert word_error_rate("один два три", "один три") == 1 / 3


def test_audio_quality_report_detects_clipping_nonfinite_and_clicks() -> None:
    audio = np.array([0.0, 0.1, 1.0, np.nan, -0.8, 0.0], dtype=np.float32)

    report = analyze_audio(audio, 2, click_step_threshold=0.5)

    assert report["audio_seconds"] == 3.0
    assert report["peak"] == 1.0
    assert report["clipped_samples"] == 1
    assert report["nonfinite_samples"] == 1
    assert report["click_candidates"] >= 2


def test_smooth_bounded_wave_has_no_quality_flags() -> None:
    audio = np.linspace(-0.2, 0.2, 24_000, dtype=np.float32)

    report = analyze_audio(audio, 24_000)

    assert report["audio_seconds"] == 1.0
    assert report["clipped_samples"] == 0
    assert report["nonfinite_samples"] == 0
    assert report["click_candidates"] == 0
