from datetime import UTC, datetime, timedelta

import pytest

from voice_assistant.attention import AttentionEngine, render_attention


NOW = datetime(2026, 8, 30, 9, 0, tzinfo=UTC)


def test_attention_requires_timezone_aware_now() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        AttentionEngine(datetime(2026, 8, 30, 9, 0))


def test_meeting_deadlines_risks_and_questions_are_ranked() -> None:
    items = [
        {
            "id": "late",
            "workspace_id": "w1",
            "kind": "action",
            "text": "Подготовить бюджет",
            "owner": "Иван",
            "due_at": (NOW - timedelta(days=2)).isoformat(),
            "status": "open",
            "source_id": "s1",
        },
        {
            "id": "today",
            "workspace_id": "w1",
            "kind": "commitment",
            "text": "Отправить протокол",
            "owner": "Пользователь",
            "due_at": NOW.date().isoformat(),
            "status": "open",
        },
        {"id": "risk", "workspace_id": "w1", "kind": "risk", "text": "Риск задержки", "status": "open"},
        {"id": "q", "workspace_id": "w1", "kind": "question", "text": "Кто владелец?", "status": "open"},
        {"id": "done", "workspace_id": "w1", "kind": "action", "text": "Готово", "status": "done"},
    ]

    events = AttentionEngine(NOW).rank(meeting_items=items)

    assert [item["key"] for item in events] == [
        "meeting_action:late",
        "meeting_commitment:today",
        "meeting_risk:risk",
        "meeting_question:q",
    ]
    assert events[0]["severity"] == "critical"
    assert "2 дня" in events[0]["reason"]
    assert events[1]["reason"] == "Срок сегодня"
    assert events[0]["source_ref"] == "s1"


def test_tasks_approvals_automations_and_results_have_expected_priority() -> None:
    events = AttentionEngine(NOW).rank(
        tasks=[
            {"id": "error", "title": "Сломанная задача", "status": "error"},
            {"id": "wait", "title": "Нужен выбор", "status": "needs_user"},
            {"id": "done", "title": "Готов отчёт", "status": "done", "updated_at": NOW.isoformat()},
        ],
        approvals=[{"id": "a1", "title": "Отправка письма", "status": "pending"}],
        automations=[{"id": "auto", "name": "Утренний дайджест", "last_status": "failed"}],
        proactivity="proactive",
    )

    assert [item["key"] for item in events[:4]] == [
        "task:error",
        "automation:auto",
        "approval:a1",
        "task:wait",
    ]
    assert events[-1]["key"] == "task:done"
    assert events[-1]["actionable"] is False


def test_inbox_priority_and_proactivity_thresholds() -> None:
    inbox = [
        {"id": "low", "title": "Новый источник", "kind": "source", "priority": 0, "status": "new"},
        {"id": "high", "title": "Ошибка", "kind": "error", "priority": 5, "status": "new"},
    ]
    engine = AttentionEngine(NOW)

    assert [item["key"] for item in engine.rank(inbox=inbox, proactivity="quiet")] == ["inbox_error:high"]
    assert [item["key"] for item in engine.rank(inbox=inbox, proactivity="balanced")] == ["inbox_error:high"]
    assert [item["key"] for item in engine.rank(inbox=inbox, proactivity="proactive")] == [
        "inbox_error:high",
        "inbox_source:low",
    ]


def test_workspace_person_filter_and_stable_deduplication() -> None:
    items = [
        {"id": "same", "workspace_id": "w1", "kind": "risk", "text": "Риск", "owner": "Иван", "status": "open"},
        {"id": "same", "workspace_id": "w1", "kind": "risk", "text": "Риск уточнён", "owner": "Иван", "status": "open"},
        {"id": "other-person", "workspace_id": "w1", "kind": "risk", "text": "Риск", "owner": "Олег", "status": "open"},
        {"id": "other-workspace", "workspace_id": "w2", "kind": "risk", "text": "Риск", "owner": "Иван", "status": "open"},
    ]

    events = AttentionEngine(NOW).rank(
        meeting_items=items,
        workspace_id="w1",
        person="иван",
    )

    assert len(events) == 1
    assert events[0]["key"] == "meeting_risk:same"


def test_changed_decision_and_relative_due_dates() -> None:
    events = AttentionEngine(NOW).rank(
        meeting_items=[
            {"id": "d", "kind": "decision", "text": "Релиз перенесён", "status": "changed"},
            {"id": "soon", "kind": "action", "text": "Проверить", "due_at": (NOW + timedelta(days=2)).date().isoformat(), "status": "open"},
        ]
    )

    assert events[0]["key"] == "meeting_action:soon"
    assert "2 дня" in events[0]["reason"]
    assert events[1]["key"] == "meeting_decision:d"


def test_render_attention_explains_each_item() -> None:
    events = AttentionEngine(NOW).rank(
        tasks=[{"id": "wait", "title": "Выбрать вариант", "status": "needs_user"}],
        meeting_items=[
            {
                "id": "late",
                "kind": "action",
                "text": "Отправить смету",
                "owner": "Мария",
                "due_at": (NOW - timedelta(days=1)).isoformat(),
                "status": "open",
            }
        ],
    )

    text = render_attention(events)

    assert "Требует внимания:" in text
    assert "Просрочено" in text
    assert "Исполнитель: Мария" in text
    assert "ожидает решения пользователя" in text
    assert render_attention([]) == "Сейчас нет событий, требующих вашего внимания."


def test_invalid_proactivity_is_rejected() -> None:
    with pytest.raises(ValueError, match="уровень проактивности"):
        AttentionEngine(NOW).rank(proactivity="maximum")
