from voice_assistant.store import AssistantStore


def add_decision_meeting(
    store: AssistantStore,
    workspace_id: str,
    *,
    title: str,
    occurred_at: str,
    topic: str,
    decision: str,
) -> tuple[dict, dict]:
    transcript = f"Встреча {title}. Решение: {decision}"
    source = store.add_source(
        workspace_id,
        "meeting",
        f"{title}.md",
        transcript,
        path=f"/tmp/{title}.md",
    )
    start = transcript.index(decision)
    meeting = store.upsert_meeting(
        source["id"],
        title=title,
        occurred_at=occurred_at,
        participants=["Анна"],
        summary=f"Обсуждалась тема {topic}",
        items=[
            {
                "kind": "decision",
                "text": decision,
                "topic": topic,
                "status": "open",
                "source_quote": decision,
                "source_start": start,
                "source_end": start + len(decision),
                "confidence": 0.95,
            }
        ],
    )
    return source, meeting


def test_workspace_timeline_contains_all_supported_entities_and_targets(tmp_path) -> None:
    store = AssistantStore(tmp_path / "assistant.sqlite3")
    workspace = store.default_workspace_id()
    task = store.create_task(workspace, "Подготовить запуск")
    store.add_task_event(task["id"], "progress", "План согласован", "Можно продолжать")
    document_text = "План запуска утверждён на октябрь."
    document = store.add_source(
        workspace,
        "document",
        "План запуска",
        document_text,
        path="/tmp/plan.md",
    )
    artifact = store.create_artifact(
        workspace,
        task["id"],
        "Итог запуска",
        "Готовый итог",
        source_refs=[
            {
                "id": document["id"],
                "char_start": 0,
                "char_end": 12,
                "selection": "explicit",
            }
        ],
    )
    approval = store.create_approval(
        task["id"],
        "send_message",
        "Отправить итог команде",
        {"channel": "project"},
    )
    _, meeting = add_decision_meeting(
        store,
        workspace,
        title="Статус проекта",
        occurred_at="2026-08-20T10:00:00+00:00",
        topic="Запуск",
        decision="Запуск переносится на октябрь",
    )

    first = store.workspace_timeline(workspace)
    second = store.workspace_timeline(workspace)

    assert [item["id"] for item in first] == [item["id"] for item in second]
    assert {item["type"] for item in first} >= {
        "task",
        "task_event",
        "meeting",
        "source",
        "artifact",
        "approval",
        "decision",
    }
    by_id = {item["id"]: item for item in first}
    assert by_id[f"task:{task['id']}"]["target"] == {
        "section": "tasks",
        "entity_type": "task",
        "entity_id": task["id"],
    }
    assert by_id[f"meeting:{meeting['id']}"]["target"]["section"] == "meetings"
    assert by_id[f"approval:{approval['id']}"]["target"]["section"] == "approvals"
    artifact_item = by_id[f"artifact:{artifact['id']}"]
    assert artifact_item["target"]["section"] == "artifacts"
    assert artifact_item["source"]["id"] == document["id"]
    assert artifact_item["source"]["char_start"] == 0
    assert artifact_item["source"]["char_end"] == 12
    assert artifact_item["source"]["excerpt"] == document_text[:12]
    assert store.snapshot(workspace_id=workspace)["workspace_timeline"] == first


def test_decision_history_uses_only_exact_normalized_topic_key(tmp_path) -> None:
    store = AssistantStore(tmp_path / "assistant.sqlite3")
    workspace = store.default_workspace_id()
    _, older = add_decision_meeting(
        store,
        workspace,
        title="Бюджет 1",
        occurred_at="2026-08-01T09:00:00+00:00",
        topic=" БЮДЖЁТ ",
        decision="Сохранить исходный бюджет",
    )
    _, newer = add_decision_meeting(
        store,
        workspace,
        title="Бюджет 2",
        occurred_at="2026-08-15T09:00:00+00:00",
        topic="бюджет",
        decision="Увеличить бюджет на десять процентов",
    )
    add_decision_meeting(
        store,
        workspace,
        title="Бюджетирование",
        occurred_at="2026-08-20T09:00:00+00:00",
        topic="бюджетирование",
        decision="Обновить процесс бюджетирования",
    )

    decisions = [
        item for item in store.workspace_timeline(workspace) if item["type"] == "decision"
    ]
    budget = sorted(
        (item for item in decisions if item["decision_thread_key"] == "бюджет"),
        key=lambda item: item["decision_sequence"],
    )
    budgeting = [
        item for item in decisions if item["decision_thread_key"] == "бюджетирование"
    ]

    assert [item["decision_sequence"] for item in budget] == [1, 2]
    assert {item["decision_count"] for item in budget} == {2}
    assert budget[0]["is_current_decision"] is False
    assert budget[1]["is_current_decision"] is True
    assert budget[1]["target"]["entity_id"] == newer["id"]
    assert budget[0]["target"]["entity_id"] == older["id"]
    assert budget[0]["current_decision_text"] == "Увеличить бюджет на десять процентов"
    assert len(budgeting) == 1
    assert budgeting[0]["decision_count"] == 1
    assert budgeting[0]["is_current_decision"] is True


def test_workspace_timeline_does_not_leak_entities_from_another_workspace(tmp_path) -> None:
    store = AssistantStore(tmp_path / "assistant.sqlite3")
    personal = store.default_workspace_id()
    other = store.create_workspace("Закрытый проект")["id"]
    personal_task = store.create_task(personal, "Личная задача")
    other_task = store.create_task(other, "Чужая задача")
    store.create_approval(
        other_task["id"],
        "external_write",
        "Чужое согласование",
        {"value": 1},
    )

    timeline = store.workspace_timeline(personal)
    serialized = "\n".join(f"{item['title']} {item['detail']}" for item in timeline)

    assert f"task:{personal_task['id']}" in {item["id"] for item in timeline}
    assert f"task:{other_task['id']}" not in {item["id"] for item in timeline}
    assert "Чужая задача" not in serialized
    assert "Чужое согласование" not in serialized
