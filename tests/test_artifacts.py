import json
from pathlib import Path
import sqlite3
import threading

from voice_assistant.config import Config
from voice_assistant.orchestrator import LocalOrchestrator
from voice_assistant.store import SCHEMA_VERSION, AssistantStore
from voice_assistant.ui_backend import EventEmitter, UIBackend


def test_markdown_artifact_lists_versions_and_restore_creates_new_version(tmp_path) -> None:
    store = AssistantStore(tmp_path / "assistant.sqlite3")
    workspace = store.default_workspace_id()
    artifact = store.create_artifact(workspace, None, "Отчёт", "Первая версия")
    store.update_artifact(artifact["id"], "Вторая версия")

    restored = store.restore_artifact(artifact["id"], 1)
    versions = store.artifact_versions(artifact["id"])

    assert restored["current_version"] == 3
    assert Path(restored["path"]).read_text(encoding="utf-8") == "Первая версия"
    assert [item["version"] for item in versions] == [1, 2, 3]
    assert [item["is_current"] for item in versions] == [False, False, True]
    assert versions[-1]["metadata"]["restored_from_version"] == 1
    assert Path(versions[1]["path"]).read_text(encoding="utf-8") == "Вторая версия"
    restored_relation = next(
        item
        for item in store.artifact_relations(artifact["id"], version=3)
        if item["relation_type"] == "restored_from"
    )
    assert restored_relation["related_artifact_id"] == artifact["id"]
    assert restored_relation["related_artifact_version"] == 1


def test_artifact_provenance_links_task_source_artifact_and_version(tmp_path) -> None:
    store = AssistantStore(tmp_path / "assistant.sqlite3")
    workspace = store.default_workspace_id()
    task = store.create_task(workspace, "Исследовать проект")
    source = store.add_source(
        workspace,
        "document",
        "Исходный отчёт",
        "Локальный пилот подтверждён.",
        path="/tmp/source.md",
    )
    base = store.create_artifact(
        workspace,
        task["id"],
        "Базовый отчёт",
        "Базовый текст",
        source_refs=[
            {
                "id": source["id"],
                "chunk_id": "chunk-1",
                "char_start": 0,
                "char_end": 15,
                "selection": "retrieved",
            }
        ],
        metadata={"skill": "research"},
    )
    derived = store.create_artifact(
        workspace,
        task["id"],
        "Производный отчёт",
        "Производный текст",
        related_artifact_id=base["id"],
        related_artifact_version=1,
        metadata={"conversion": "summary"},
    )

    base_relations = store.artifact_relations(base["id"], version=1)
    derived_relations = store.artifact_relations(derived["id"], version=1)
    task_relation = next(
        item for item in base_relations if item["relation_type"] == "produced_by_task"
    )
    source_relation = next(
        item for item in base_relations if item["relation_type"] == "derived_from_source"
    )
    artifact_relation = next(
        item
        for item in derived_relations
        if item["relation_type"] == "derived_from_artifact"
    )

    assert task_relation["task_id"] == task["id"]
    assert task_relation["artifact_version"] == 1
    assert source_relation["source_id"] == source["id"]
    assert source_relation["metadata"] == {
        "chunk_id": "chunk-1",
        "char_start": 0,
        "char_end": 15,
        "selection": "retrieved",
    }
    assert artifact_relation["related_artifact_id"] == base["id"]
    assert artifact_relation["related_artifact_version"] == 1


def test_artifact_commands_and_quick_action_contract(capsys, tmp_path) -> None:
    store = AssistantStore(tmp_path / "assistant.sqlite3")
    backend = UIBackend(Config.defaults(), EventEmitter(), store)
    task = store.create_task(store.default_workspace_id(), "Итог пилота")
    store.update_task(task["id"], status="done", result="Пилот завершён успешно.")
    backend.current_task_id = task["id"]
    capsys.readouterr()

    actions = backend.orchestrator.quick_actions(task["id"])
    assert actions[0]["id"] == "save_as_artifact"
    assert actions[0]["command"] == "quick_action"

    backend.handle(
        {
            "command": "quick_action",
            "id": "save_as_artifact",
            "task_id": task["id"],
        }
    )
    quick_events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    completed = next(item for item in quick_events if item["type"] == "quick_action_completed")
    artifact = completed["result"]["artifact"]
    assert completed["result"]["created"] is True
    assert {
        item["relation_type"] for item in store.artifact_relations(artifact["id"])
    } == {"produced_by_task"}

    backend.handle({"command": "artifact_versions", "artifact_id": artifact["id"]})
    version_event = json.loads(capsys.readouterr().out)
    assert version_event["type"] == "artifact_versions"
    assert version_event["versions"][0]["version"] == 1

    backend.handle(
        {"command": "restore_artifact", "artifact_id": artifact["id"], "version": 1}
    )
    restore_events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    restored = next(item for item in restore_events if item["type"] == "artifact_restored")
    assert restored["artifact"]["current_version"] == 2
    assert len(restored["versions"]) == 2


def test_assistant_end_exposes_quick_actions_after_plain_result(capsys, tmp_path) -> None:
    store = AssistantStore(tmp_path / "assistant.sqlite3")
    backend = UIBackend(Config.defaults(), EventEmitter(), store)

    class FakeAssistant:
        @staticmethod
        def answer(text, *, on_token, on_phase, **kwargs):  # noqa: ANN001
            del text, kwargs
            on_phase("thinking")
            on_token("Готово")
            return "Готово"

    backend.assistant = FakeAssistant()
    turn = backend._prepare_turn("Ответь кратко", spoken=False)
    backend._answer(turn, cancel_event=threading.Event(), speak=False)
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assistant_end = next(item for item in events if item["type"] == "assistant_end")

    assert {item["id"] for item in assistant_end["quick_actions"]} == {
        "save_as_artifact",
        "save_to_memory",
    }


def test_schema_v3_migrates_artifact_metadata_and_relations(tmp_path) -> None:
    database = tmp_path / "assistant.sqlite3"
    store = AssistantStore(database)
    store.close()

    connection = sqlite3.connect(database)
    connection.execute("DROP TABLE artifact_relations")
    connection.execute("ALTER TABLE artifact_versions DROP COLUMN metadata")
    connection.execute("PRAGMA user_version = 3")
    connection.commit()
    connection.close()

    migrated = AssistantStore(database)
    columns = {
        row[1]
        for row in migrated._connection.execute("PRAGMA table_info(artifact_versions)")
    }
    assert "metadata" in columns
    assert migrated._connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='artifact_relations'"
    ).fetchone()
    assert migrated._connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
