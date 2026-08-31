from __future__ import annotations

from datetime import UTC, datetime
import json
from pathlib import Path

import pytest

from voice_assistant.config import Config
from voice_assistant.orchestrator import LocalOrchestrator
from voice_assistant.store import AssistantStore
from voice_assistant.ui_backend import EventEmitter, UIBackend
from voice_assistant.workflows import (
    build_digest,
    mutate_task_plan,
    parse_digest_command,
    parse_task_plan_command,
    persist_digest,
)


TRANSCRIPT = """[00:01] Анна: Тема: Запуск пилота.
[00:14] Иван: Решили запустить пилот в сентябре.
[00:30] Анна: Иван подготовит смету до 12 сентября.
[00:44] Олег: Беру на себя проверку безопасности к пятнице.
[01:02] Анна: Риск: подрядчик может не успеть.
[01:14] Иван: Кто согласует бюджет?"""


def _seed_digest_data(store: AssistantStore) -> dict[str, object]:
    workspace_id = store.default_workspace_id()
    task = store.create_task(
        workspace_id,
        "Подготовить пилот",
        ["Собрать смету", "Проверить риски"],
        classification="confidential",
    )
    store.update_task(task["id"], status="needs_user")
    source = store.add_source(
        workspace_id,
        "meeting",
        "Статус пилота",
        TRANSCRIPT,
        path="/meetings/status-pilot.md",
        classification="restricted",
    )
    meeting = store.analyze_meeting(source["id"])
    inbox_id = store.add_inbox(
        workspace_id,
        "notice",
        "Нужно решение",
        "Согласуйте бюджет пилота.",
        priority=3,
        source_ref=task["id"],
    )
    artifact = store.create_artifact(
        workspace_id,
        task["id"],
        "Черновик пилота",
        "# Черновик",
        source_refs=[source["id"]],
    )
    return {
        "workspace_id": workspace_id,
        "task": task,
        "source": source,
        "meeting": meeting,
        "inbox_id": inbox_id,
        "artifact": artifact,
    }


def test_plan_commands_mutate_persisted_plan_and_preserve_task_state(
    tmp_path: Path,
) -> None:
    store = AssistantStore(tmp_path / "assistant.sqlite3")
    task = store.create_task(
        store.default_workspace_id(),
        "План пилота",
        ["Собрать данные", "Проверить риски", "Подготовить вывод"],
    )
    store.update_task(task["id"], status="needs_user", result="Черновик")

    added = mutate_task_plan(store, "добавь в план Согласовать результат", task["id"])
    replaced = mutate_task_plan(store, "замени шаг 2 на Проверить бюджет", task["id"])
    deleted = mutate_task_plan(store, "удали шаг 1", task["id"])

    assert added and added["action"] == "add"
    assert replaced and replaced["old_step"] == "Проверить риски"
    assert deleted and deleted["old_step"] == "Собрать данные"
    persisted = store.get_task(task["id"])
    assert persisted["plan"] == [
        "Проверить бюджет",
        "Подготовить вывод",
        "Согласовать результат",
    ]
    assert persisted["status"] == "needs_user"
    assert persisted["result"] == "Черновик"
    assert len(store._rows("SELECT id FROM task_events WHERE task_id=? AND kind='plan'", (task["id"],))) == 3
    assert [
        row["action"]
        for row in store._rows(
            "SELECT action FROM audit_log WHERE task_id=? AND action LIKE 'task.plan.%' ORDER BY rowid",
            (task["id"],),
        )
    ] == ["task.plan.add", "task.plan.replace", "task.plan.delete"]


def test_plan_command_parser_is_bounded_and_reports_invalid_mutations(
    tmp_path: Path,
) -> None:
    assert parse_task_plan_command("Расскажи про план") is None
    assert parse_task_plan_command("замени 3-й шаг на Финальную проверку") == {
        "action": "replace",
        "index": 3,
        "step": "Финальную проверку",
    }
    with pytest.raises(ValueError, match="Используйте"):
        parse_task_plan_command("добавь шаг в план")

    store = AssistantStore(tmp_path / "assistant.sqlite3")
    task = store.create_task(store.default_workspace_id(), "Один шаг", ["Единственный"])
    with pytest.raises(ValueError, match="единственный"):
        mutate_task_plan(store, "удали шаг 1", task["id"])
    with pytest.raises(ValueError, match="шаг 5 не найден"):
        mutate_task_plan(store, "замени шаг 5 на Другой", task["id"])
    assert store.get_task(task["id"])["plan"] == ["Единственный"]


def test_digest_has_structured_sections_and_transparent_local_references(
    tmp_path: Path,
) -> None:
    store = AssistantStore(tmp_path / "assistant.sqlite3")
    seeded = _seed_digest_data(store)

    digest = build_digest(
        store,
        str(seeded["workspace_id"]),
        "weekly",
        now=datetime.now(UTC),
    )

    assert [section["id"] for section in digest["sections"]] == [
        "tasks",
        "meeting_items",
        "inbox",
        "artifacts",
    ]
    assert set(digest["counts"]) == {"tasks", "meeting_items", "inbox", "artifacts"}
    assert all(
        item["reference"].startswith("D")
        for section in digest["sections"]
        for item in section["items"]
    )
    assert {reference["type"] for reference in digest["references"]} >= {
        "task",
        "meeting_item",
        "inbox",
        "artifact",
    }
    meeting_references = [
        reference
        for reference in digest["references"]
        if reference["type"] == "meeting_item"
    ]
    meeting_items = {
        item["id"]: item
        for item in store.list_meeting_items(str(seeded["workspace_id"]))
    }
    source = store.get_source(str(seeded["source"]["id"]))  # type: ignore[index]
    assert meeting_references
    for reference in meeting_references:
        meeting_item = meeting_items[reference["id"]]
        assert reference["source_id"] == source["id"]
        assert (
            source["content"][reference["char_start"] : reference["char_end"]]
            == meeting_item["source_quote"]
        )
    assert digest["classification"] == "restricted"
    assert digest["local_only"] is True
    assert "Почта, календарь, Синапс" in digest["scope_note"]
    assert "[D1]" in digest["text"]


def test_digest_period_command_and_persistence_are_deterministic(
    tmp_path: Path,
) -> None:
    assert parse_digest_command("/digest утро") == "morning"
    assert parse_digest_command("/digest итоги дня") == "evening"
    assert parse_digest_command("/digest обнови итоги недели") == "weekly"
    assert parse_digest_command("Собери дайджест") is None
    with pytest.raises(ValueError, match="утро, вечер или неделя"):
        parse_digest_command("/digest квартал")

    store = AssistantStore(tmp_path / "assistant.sqlite3")
    seeded = _seed_digest_data(store)
    persisted = persist_digest(
        store,
        str(seeded["workspace_id"]),
        "weekly",
        request_text="/digest неделя",
        now=datetime.now(UTC),
    )

    task = store.get_task(persisted["task"]["id"])
    assert task["status"] == "done"
    assert task["result"] == persisted["text"]
    messages = store.messages(task["id"])
    metadata = json.loads(messages[-1]["metadata"])
    assert [message["role"] for message in messages] == ["user", "assistant"]
    assert metadata["deterministic"] is True
    assert metadata["digest"]["period"] == "weekly"
    version = store.artifact_versions(persisted["artifact"]["id"])[0]
    assert version["metadata"]["origin"] == "structured_digest"
    assert version["metadata"]["local_only"] is True
    assert version["metadata"]["references"] == persisted["references"]


def test_ui_plan_and_digest_commands_bypass_llm(capsys, tmp_path: Path) -> None:
    store = AssistantStore(tmp_path / "assistant.sqlite3")
    workspace_id = store.default_workspace_id()
    task = store.create_task(workspace_id, "План", ["Старт"])
    backend = UIBackend(Config.defaults(), EventEmitter(), store)
    backend.current_task_id = task["id"]

    class NoLLMAssistant:
        @staticmethod
        def answer(*args, **kwargs):  # noqa: ANN002, ANN003
            del args, kwargs
            raise AssertionError("LLM must not be called for deterministic commands")

    backend.assistant = NoLLMAssistant()  # type: ignore[assignment]
    backend._text_turn("добавь в план Проверить бюджет", speak=False)
    backend._text_turn("/digest weekly", speak=False)

    assert store.get_task(task["id"])["plan"] == ["Старт", "Проверить бюджет"]
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert any(event["type"] == "plan_updated" for event in events)
    assert any(event["type"] == "digest_generated" for event in events)
    deterministic_ends = [
        event
        for event in events
        if event["type"] == "assistant_end" and event.get("deterministic")
    ]
    assert {event["local_event"] for event in deterministic_ends} == {
        "task_plan",
        "digest",
    }


def test_digest_automation_uses_structured_local_path(capsys, tmp_path: Path) -> None:
    store = AssistantStore(tmp_path / "assistant.sqlite3")
    workspace_id = store.default_workspace_id()
    store.create_task(workspace_id, "Проверить пилот")
    automation = store.create_automation(
        workspace_id,
        "Еженедельная сводка",
        "/digest weekly",
        "при изменении контекста",
    )
    backend = UIBackend(Config.defaults(), EventEmitter(), store)

    class NoLLMAssistant:
        @staticmethod
        def answer(*args, **kwargs):  # noqa: ANN002, ANN003
            del args, kwargs
            raise AssertionError("LLM must not be called for a digest automation")

    backend.assistant = NoLLMAssistant()  # type: ignore[assignment]
    backend._run_automation(automation)

    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    completed = next(event for event in events if event["type"] == "automation_completed")
    assert completed["deterministic"] is True
    assert completed["local_event"] == "digest"
    digest_artifacts = store._rows(
        """
        SELECT av.metadata FROM artifact_versions av
        JOIN artifacts a ON a.id=av.artifact_id
        WHERE a.workspace_id=?
        """,
        (workspace_id,),
    )
    assert any(
        json.loads(row["metadata"])["origin"] == "structured_digest"
        for row in digest_artifacts
    )
