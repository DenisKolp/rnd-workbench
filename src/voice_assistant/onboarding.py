from __future__ import annotations

from typing import Any, Mapping


_TOTAL_MILESTONES = 4


def _non_negative_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, parsed)


def _check_statuses(preflight: Mapping[str, Any]) -> dict[str, str]:
    rows = preflight.get("checks")
    if not isinstance(rows, list):
        return {}
    statuses: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        check_id = str(row.get("id") or "")
        status = str(row.get("status") or "")
        if check_id and status in {"pass", "warn", "block", "unverified"}:
            statuses[check_id] = status
    return statuses


def _result(
    *,
    status: str,
    stage: str,
    title: str,
    detail: str,
    action_id: str,
    action_label: str,
    completed: int,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "status": status,
        "stage": stage,
        "title": title,
        "detail": detail,
        "action_id": action_id,
        "action_label": action_label,
        "progress": {
            "completed": max(0, min(completed, _TOTAL_MILESTONES)),
            "total": _TOTAL_MILESTONES,
        },
        "content_transmitted": False,
    }


def build_pilot_onboarding(
    preflight: Mapping[str, Any],
    usage: Mapping[str, Any],
) -> dict[str, Any]:
    """Choose one deterministic next step without inspecting work content.

    Only fixed copy and aggregate counters leave this function.  Preflight
    details, task names, prompts, paths, participants and source metadata are
    deliberately ignored.
    """

    statuses = _check_statuses(preflight)
    first_value = usage.get("first_value_seconds") is not None
    voice_turn = _non_negative_int(usage.get("voice_turns")) > 0
    meeting_import = _non_negative_int(usage.get("meeting_imports")) > 0
    meeting_briefing = _non_negative_int(usage.get("meeting_briefings")) > 0
    completed = sum((first_value, voice_turn, meeting_import, meeting_briefing))

    if not statuses:
        return _result(
            status="blocked",
            stage="check_device",
            title="Проверим устройство",
            detail="Сначала нужна техническая проверка локального контура.",
            action_id="review_preflight",
            action_label="Проверить устройство",
            completed=completed,
        )

    core_blocked = any(statuses.get(check_id) == "block" for check_id in ("storage", "llm"))
    if core_blocked:
        return _result(
            status="blocked",
            stage="repair_core",
            title="Нужна базовая настройка",
            detail="Хранилище или языковая модель пока блокируют первый результат.",
            action_id="review_preflight",
            action_label="Посмотреть проверку",
            completed=completed,
        )

    voice_ready = statuses.get("stt") == "pass" and statuses.get("tts") == "pass"
    if not first_value:
        if voice_ready:
            return _result(
                status="active",
                stage="first_voice_result",
                title="Получим первый результат",
                detail="Откройте голосовой виджет и задайте один рабочий вопрос.",
                action_id="start_voice",
                action_label="Открыть голос",
                completed=completed,
            )
        return _result(
            status="active",
            stage="first_text_result",
            title="Получим первый результат",
            detail="Голос ещё настраивается — первый рабочий вопрос можно задать в чате.",
            action_id="open_chat",
            action_label="Открыть чат",
            completed=completed,
        )

    if not voice_turn:
        if voice_ready:
            return _result(
                status="active",
                stage="try_voice",
                title="Проверьте голосовой сценарий",
                detail="Сделайте один короткий запрос и перебейте ответ новой репликой.",
                action_id="start_voice",
                action_label="Открыть голос",
                completed=completed,
            )
        return _result(
            status="blocked",
            stage="repair_voice",
            title="Настройте локальный голос",
            detail="Для пилота нужны готовые Whisper и OmniVoice-Fast.",
            action_id="review_preflight",
            action_label="Посмотреть проверку",
            completed=completed,
        )

    if not meeting_import:
        return _result(
            status="active",
            stage="import_meeting",
            title="Добавьте первую встречу",
            detail="Импортируйте аудиозапись, транскрипт или ZIP eXpress с контекстом.",
            action_id="show_meeting_import",
            action_label="Открыть встречи",
            completed=completed,
        )

    if not meeting_briefing:
        return _result(
            status="active",
            stage="prepare_briefing",
            title="Подготовьтесь к следующей встрече",
            detail="Соберите сводку из решений, поручений, рисков и открытых вопросов.",
            action_id="prepare_briefing",
            action_label="Подготовить сводку",
            completed=completed,
        )

    return _result(
        status="completed",
        stage="completed",
        title="Быстрый старт завершён",
        detail="Голос и первая вертикаль встреч уже проверены на этом устройстве.",
        action_id="",
        action_label="",
        completed=completed,
    )


__all__ = ["build_pilot_onboarding"]
