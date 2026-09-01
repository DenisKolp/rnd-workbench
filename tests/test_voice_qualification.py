import json

import pytest

from voice_assistant.voice_qualification import (
    VOICE_QUALIFICATION_CASES,
    VoiceQualificationCancelled,
    run_voice_qualification,
)


def test_voice_qualification_returns_only_content_free_numeric_aggregates() -> None:
    recorded: list[tuple[str, float]] = []
    progress: list[tuple[int, int, str]] = []

    result = run_voice_qualification(
        lambda text: text,
        lambda metric, value: recorded.append((metric, value)),
        cancelled=lambda: False,
        progress=lambda completed, total, category: progress.append(
            (completed, total, category)
        ),
    )
    serialized = json.dumps(result, ensure_ascii=False)

    assert result["sample_count"] == 10
    assert result["content_transmitted"] is False
    assert result["acoustic_hardware_measured"] is False
    assert result["metrics"]["stt_clean_wer"] == {
        "count": 5,
        "average": 0.0,
        "max": 0.0,
    }
    assert result["metrics"]["stt_corporate_wer"]["count"] == 5
    assert len(recorded) == len(VOICE_QUALIFICATION_CASES)
    assert progress[-1] == (10, 10, "corporate")
    assert all(case.text not in serialized for case in VOICE_QUALIFICATION_CASES)


def test_voice_qualification_cancels_without_recording_content() -> None:
    calls = 0

    def cancelled() -> bool:
        nonlocal calls
        calls += 1
        return calls > 1

    with pytest.raises(VoiceQualificationCancelled):
        run_voice_qualification(
            lambda text: text,
            lambda _metric, _value: None,
            cancelled=cancelled,
        )
