from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from .voice_quality import word_error_rate


@dataclass(frozen=True, slots=True)
class VoiceQualificationCase:
    category: str
    text: str


VOICE_QUALIFICATION_CASES = (
    VoiceQualificationCase("clean", "Сегодня хорошая погода и мы готовы к работе"),
    VoiceQualificationCase("clean", "Пожалуйста проверь статус текущей задачи"),
    VoiceQualificationCase("clean", "Добавь встречу в календарь на десять часов"),
    VoiceQualificationCase("clean", "Подготовь краткое резюме этого документа"),
    VoiceQualificationCase("clean", "Напомни мне отправить письмо после обеда"),
    VoiceQualificationCase("corporate", "Собери транскрипцию встречи из Синапса"),
    VoiceQualificationCase("corporate", "Создай задачу в Джире и добавь ссылку на Конфлюенс"),
    VoiceQualificationCase("corporate", "Обнови карточку в Кайтене после согласования"),
    VoiceQualificationCase("corporate", "Подготовь повестку по проекту Эр Эн Ди Воркбенч"),
    VoiceQualificationCase("corporate", "Проверь вложения встречи и список договоренностей"),
)

VOICE_QUALIFICATION_METRICS = {
    "clean": "stt_clean_wer",
    "corporate": "stt_corporate_wer",
}


class VoiceQualificationCancelled(RuntimeError):
    """Raised without any work content when a local qualification is cancelled."""


def run_voice_qualification(
    transcribe_case: Callable[[str], str],
    record_metric: Callable[[str, float], None],
    *,
    cancelled: Callable[[], bool],
    progress: Callable[[int, int, str], None] | None = None,
    cases: Iterable[VoiceQualificationCase] = VOICE_QUALIFICATION_CASES,
) -> dict[str, Any]:
    """Measure local digital-loopback WER and return content-free aggregates.

    Fixed phrases and hypotheses exist only in memory. Callers receive category
    names and numeric measurements; no audio, reference, or transcript leaves
    this function.
    """

    selected = tuple(cases)
    if not selected:
        raise ValueError("Набор проверки распознавания пуст")
    values: dict[str, list[float]] = {
        metric: [] for metric in VOICE_QUALIFICATION_METRICS.values()
    }
    total = len(selected)
    for completed, case in enumerate(selected, start=1):
        if cancelled():
            raise VoiceQualificationCancelled("Проверка распознавания отменена")
        metric = VOICE_QUALIFICATION_METRICS.get(case.category)
        if metric is None:
            raise ValueError("Неизвестная категория проверки распознавания")
        transcript = transcribe_case(case.text)
        if cancelled():
            raise VoiceQualificationCancelled("Проверка распознавания отменена")
        value = round(word_error_rate(case.text, transcript), 6)
        record_metric(metric, value)
        values[metric].append(value)
        if progress is not None:
            progress(completed, total, case.category)

    return {
        "schema_version": 1,
        "privacy": "content_free_aggregate",
        "mode": "local_digital_loopback",
        "sample_count": total,
        "acoustic_hardware_measured": False,
        "content_transmitted": False,
        "metrics": {
            metric: {
                "count": len(metric_values),
                "average": round(sum(metric_values) / len(metric_values), 6),
                "max": round(max(metric_values), 6),
            }
            for metric, metric_values in values.items()
            if metric_values
        },
    }


__all__ = [
    "VOICE_QUALIFICATION_CASES",
    "VOICE_QUALIFICATION_METRICS",
    "VoiceQualificationCancelled",
    "VoiceQualificationCase",
    "run_voice_qualification",
]
