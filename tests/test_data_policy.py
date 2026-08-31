from __future__ import annotations

import json
from pathlib import Path
import sqlite3
from typing import Any

import pytest

from voice_assistant.config import Config
from voice_assistant.orchestrator import LocalOrchestrator, RoutingPolicyError
from voice_assistant.store import DATA_CLASSIFICATIONS, SCHEMA_VERSION, AssistantStore
from voice_assistant.ui_backend import EventEmitter, UIBackend


CLASSIFIED_TABLES = (
    "workspaces",
    "tasks",
    "messages",
    "sources",
    "memory",
    "skills",
    "artifacts",
    "artifact_versions",
    "automations",
)


def make_store(tmp_path: Path) -> AssistantStore:
    return AssistantStore(tmp_path / "assistant.sqlite3")


def configure_route(
    store: AssistantStore,
    route: str,
    *,
    workspace_id: str | None = None,
    workspace_scope: bool = False,
) -> None:
    if route == "local":
        store.set_settings(
            {
                "model_mode": "local",
                "external_provider_type": "external",
                "external_context_scope": "task",
                "external_context_scope_endpoint": "",
                "external_context_scope_workspace": "",
            }
        )
        return

    endpoint = f"https://{route}.example.test/v1"
    values = {
        "model_mode": "external",
        "llm_base_url": endpoint,
        "llm_model": f"{route}-model",
        "external_provider_type": route,
        "external_context_scope": "workspace" if workspace_scope else "task",
        "external_context_scope_endpoint": endpoint if workspace_scope else "",
        "external_context_scope_workspace": workspace_id if workspace_scope else "",
    }
    store.set_settings({key: str(value or "") for key, value in values.items()})


def test_fresh_schema_v7_has_fail_safe_defaults_and_public_builtin_skills(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path)

    assert SCHEMA_VERSION == 7
    assert store._connection.execute("PRAGMA user_version").fetchone()[0] == 7
    assert DATA_CLASSIFICATIONS == (
        "public",
        "internal",
        "confidential",
        "restricted",
    )

    for table in CLASSIFIED_TABLES:
        columns = {
            row[1]: row
            for row in store._connection.execute(f"PRAGMA table_info({table})")
        }
        assert "classification" in columns, table
        assert columns["classification"][3] == 1, table  # NOT NULL
        assert columns["classification"][4] == "'internal'", table

    snapshot = store.snapshot()
    assert snapshot["workspaces"][0]["classification"] == "internal"
    assert {skill["classification"] for skill in snapshot["skills"] if skill["builtin"]} == {
        "public"
    }
    assert snapshot["settings"]["default_classification"] == "internal"
    assert snapshot["settings"]["external_provider_type"] == "external"

    with pytest.raises(ValueError, match="Классификация"):
        store.create_workspace("Некорректное пространство", classification="secret")


def test_v4_migration_backfills_every_llm_entity_without_data_loss(
    tmp_path: Path,
) -> None:
    database = tmp_path / "assistant.sqlite3"
    original = AssistantStore(database)
    workspace_id = original.default_workspace_id()
    task = original.create_task(workspace_id, "Legacy task")
    message = original.add_message(task["id"], "user", "LEGACY_MESSAGE")
    source = original.add_source(
        workspace_id,
        "document",
        "Legacy source",
        "LEGACY_SOURCE searchable marker",
    )
    memory = original.remember(
        "Legacy memory",
        "LEGACY_MEMORY",
        workspace_id=workspace_id,
    )
    skill = original.create_or_update_skill(
        "Legacy skill",
        "/legacy",
        "Legacy description",
        "LEGACY_SKILL",
        workspace_id=workspace_id,
    )
    artifact = original.create_artifact(
        workspace_id,
        task["id"],
        "Legacy artifact",
        "LEGACY_ARTIFACT",
        source_refs=[source["id"]],
    )
    automation = original.create_automation(
        workspace_id,
        "Legacy automation",
        "LEGACY_AUTOMATION",
        "при изменении контекста",
    )
    original.close()

    connection = sqlite3.connect(database)
    connection.execute("PRAGMA foreign_keys = OFF")
    for table in CLASSIFIED_TABLES:
        connection.execute(f"ALTER TABLE {table} DROP COLUMN classification")
    connection.execute("PRAGMA user_version = 4")
    connection.commit()
    connection.close()

    migrated = AssistantStore(database)

    assert migrated._connection.execute("PRAGMA user_version").fetchone()[0] == 7
    assert migrated._connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert migrated._connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert migrated.get_workspace(workspace_id)["classification"] == "internal"
    assert migrated.get_task(task["id"])["classification"] == "internal"
    assert migrated.messages(task["id"])[0] == {
        **message,
        "classification": "internal",
    }
    assert migrated.get_source(source["id"])["classification"] == "internal"
    assert migrated._rows("SELECT * FROM memory WHERE id=?", (memory["id"],))[0][
        "classification"
    ] == "internal"
    assert migrated._rows("SELECT * FROM skills WHERE id=?", (skill["id"],))[0][
        "classification"
    ] == "internal"
    assert migrated.get_artifact(artifact["id"])["classification"] == "internal"
    assert migrated.artifact_versions(artifact["id"])[0]["classification"] == "internal"
    assert migrated._rows(
        "SELECT * FROM automations WHERE id=?", (automation["id"],)
    )[0]["classification"] == "internal"
    assert {
        row["classification"]
        for row in migrated._rows("SELECT * FROM skills WHERE builtin=1")
    } == {"public"}
    assert migrated.search_sources("searchable marker", workspace_id=workspace_id)[0][
        "id"
    ] == source["id"]
    assert Path(migrated.get_artifact(artifact["id"])["path"]).read_text() == (
        "LEGACY_ARTIFACT"
    )


def test_crud_inherits_validates_and_propagates_classification(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    workspace = store.create_workspace(
        "Чувствительный проект",
        classification="confidential",
    )
    inherited_task = store.create_task(workspace["id"], "Inherited task")
    public_task = store.create_task(
        workspace["id"],
        "Public task",
        classification="public",
    )
    inherited_message = store.add_message(
        inherited_task["id"],
        "user",
        "Inherited message",
    )
    restricted_message = store.add_message(
        public_task["id"],
        "assistant",
        "Restricted result",
        classification="restricted",
    )
    task_source = store.add_source(
        workspace["id"],
        "document",
        "Task source",
        "Task-only content",
        visibility="task",
        task_id=public_task["id"],
    )
    restricted_source = store.add_source(
        workspace["id"],
        "document",
        "Restricted source",
        "Restricted content",
        classification="restricted",
    )
    inherited_memory = store.remember(
        "Derived memory",
        "Derived content",
        workspace_id=workspace["id"],
        source_id=restricted_source["id"],
    )
    custom_skill = store.create_or_update_skill(
        "Confidential skill",
        "/confidential",
        "Description",
        "Instruction",
        workspace_id=workspace["id"],
        classification="confidential",
    )
    automation = store.create_automation(
        workspace["id"],
        "Inherited automation",
        "Automation prompt",
        "при изменении контекста",
    )
    artifact = store.create_artifact(
        workspace["id"],
        public_task["id"],
        "Derived artifact",
        "Derived artifact content",
        source_refs=[restricted_source["id"]],
    )

    assert inherited_task["classification"] == "confidential"
    assert public_task["classification"] == "public"
    assert inherited_message["classification"] == "confidential"
    assert restricted_message["classification"] == "restricted"
    assert task_source["classification"] == "public"
    assert restricted_source["classification"] == "restricted"
    assert inherited_memory["classification"] == "restricted"
    assert custom_skill["classification"] == "confidential"
    assert automation["classification"] == "confidential"
    assert artifact["classification"] == "restricted"
    assert store.artifact_versions(artifact["id"])[0]["classification"] == "restricted"

    updated = store.set_classification(
        "source",
        restricted_source["id"],
        "internal",
        reason="test",
    )
    assert updated["classification"] == "internal"
    audit = store._rows(
        "SELECT * FROM audit_log WHERE action='classification.update' ORDER BY created_at DESC"
    )[0]
    assert audit["target"] == f"source:{restricted_source['id']}"
    assert audit["detail"] == "restricted->internal; source=test"
    assert "Restricted content" not in json.dumps(audit, ensure_ascii=False)

    with pytest.raises(ValueError, match="Классификация"):
        store.add_message(public_task["id"], "user", "bad", classification="top-secret")
    with pytest.raises(ValueError, match="Классификацию можно менять"):
        store.set_classification("message", restricted_message["id"], "public")


@pytest.mark.parametrize(
    ("route", "classification", "allowed", "allowed_max"),
    [
        ("local", "public", True, "restricted"),
        ("local", "internal", True, "restricted"),
        ("local", "confidential", True, "restricted"),
        ("local", "restricted", True, "restricted"),
        ("external", "public", True, "internal"),
        ("external", "internal", True, "internal"),
        ("external", "confidential", False, "internal"),
        ("external", "restricted", False, "internal"),
        ("corporate", "public", True, "confidential"),
        ("corporate", "internal", True, "confidential"),
        ("corporate", "confidential", True, "confidential"),
        ("corporate", "restricted", False, "confidential"),
    ],
)
def test_local_external_corporate_policy_matrix(
    tmp_path: Path,
    route: str,
    classification: str,
    allowed: bool,
    allowed_max: str,
) -> None:
    store = make_store(tmp_path)
    workspace = store.create_workspace("Public policy workspace", classification="public")
    task = store.create_task(
        workspace["id"],
        f"{route}-{classification}",
        classification=classification,
    )
    store.set_setting("memory_enabled", "false")
    configure_route(store, route)
    orchestrator = LocalOrchestrator(store)

    if allowed:
        turn = orchestrator.prepare_turn(
            "Нейтральный запрос",
            workspace_id=workspace["id"],
            task_id=task["id"],
        )
        assert turn.policy is not None
        assert turn.policy.route == route
        assert turn.policy.allowed_max == allowed_max
        assert turn.policy.effective_classification == classification
        assert store.get_task(task["id"])["status"] == "running"
    else:
        with pytest.raises(RoutingPolicyError) as raised:
            orchestrator.prepare_turn(
                "Нейтральный запрос",
                workspace_id=workspace["id"],
                task_id=task["id"],
            )
        assert raised.value.route == route
        assert raised.value.allowed_max == allowed_max
        assert raised.value.effective_classification == classification
        assert store.get_task(task["id"])["status"] == "needs_user"


def test_loopback_openai_route_keeps_restricted_data_local(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    workspace = store.create_workspace("Loopback", classification="public")
    task = store.create_task(
        workspace["id"],
        "Restricted local server task",
        classification="restricted",
    )
    store.set_settings(
        {
            "model_mode": "external",
            "llm_base_url": "http://127.0.0.1:11434/v1",
            "llm_model": "local-server-model",
            "external_provider_type": "external",
            "external_context_scope": "task",
        }
    )

    turn = LocalOrchestrator(store).prepare_turn(
        "Локальный запрос",
        workspace_id=workspace["id"],
        task_id=task["id"],
    )

    assert turn.policy is not None
    assert turn.policy.route == "local"
    assert turn.policy.allowed_max == "restricted"
    assert turn.classification == "restricted"


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://localhost.evil.example/v1",
        "https://127.0.0.1.evil.example/v1",
    ],
)
def test_loopback_lookalike_hostname_is_still_external_and_blocks_restricted_data(
    tmp_path: Path,
    endpoint: str,
) -> None:
    store = make_store(tmp_path)
    workspace = store.create_workspace("Lookalike", classification="public")
    task = store.create_task(
        workspace["id"],
        "Restricted remote task",
        classification="restricted",
    )
    store.set_settings(
        {
            "model_mode": "external",
            "llm_base_url": endpoint,
            "llm_model": "remote-model",
            "external_provider_type": "external",
            "external_context_scope": "task",
        }
    )

    with pytest.raises(RoutingPolicyError) as raised:
        LocalOrchestrator(store).prepare_turn(
            "Не отправлять",
            workspace_id=workspace["id"],
            task_id=task["id"],
        )

    assert raised.value.route == "external"
    assert raised.value.allowed_max == "internal"


def test_external_workspace_scope_filters_sensitive_auto_source_and_memory(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path)
    workspace = store.create_workspace("Internal workspace", classification="internal")
    task = store.create_task(workspace["id"], "Safe task")
    source = store.add_source(
        workspace["id"],
        "document",
        "Кобальтовый бюджет",
        "Кобальтовый бюджет утверждён. SOURCE_SECRET_MARKER",
        classification="confidential",
    )
    memory = store.remember(
        "Sensitive memory",
        "MEMORY_SECRET_MARKER",
        workspace_id=workspace["id"],
        classification="restricted",
    )
    configure_route(
        store,
        "external",
        workspace_id=workspace["id"],
        workspace_scope=True,
    )

    turn = LocalOrchestrator(store).prepare_turn(
        "Проверь кобальтовый бюджет",
        workspace_id=workspace["id"],
        task_id=task["id"],
    )

    assert turn.policy is not None
    assert turn.policy.route == "external"
    assert turn.policy.allowed_max == "internal"
    assert turn.policy.effective_classification == "internal"
    assert turn.sources == []
    assert "SOURCE_SECRET_MARKER" not in turn.prompt
    assert "MEMORY_SECRET_MARKER" not in turn.prompt
    assert {(item["kind"], item["id"], item["classification"]) for item in turn.policy.filtered_refs} == {
        ("source", source["id"], "confidential"),
        ("memory", memory["id"], "restricted"),
    }
    assert store._rows(
        "SELECT 1 FROM task_events WHERE task_id=? AND kind='routing_filtered'",
        (task["id"],),
    )
    policy_audit = store._rows(
        "SELECT * FROM audit_log WHERE task_id=? AND action='llm.route_allowed'",
        (task["id"],),
    )[-1]
    assert policy_audit["detail"].endswith("filtered=2")
    assert "SOURCE_SECRET_MARKER" not in json.dumps(policy_audit)
    assert "MEMORY_SECRET_MARKER" not in json.dumps(policy_audit)


def test_linked_sensitive_source_blocks_remote_turn(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    workspace = store.create_workspace("Public workspace", classification="public")
    task = store.create_task(workspace["id"], "Internal task", classification="internal")
    source = store.add_source(
        workspace["id"],
        "document",
        "Linked confidential source",
        "LINKED_SECRET_MARKER",
        classification="confidential",
    )
    store.link_task_source(task["id"], source["id"])
    configure_route(store, "external")

    with pytest.raises(RoutingPolicyError) as raised:
        LocalOrchestrator(store).prepare_turn(
            "Продолжай задачу",
            workspace_id=workspace["id"],
            task_id=task["id"],
        )

    blocked = list(raised.value.blocked_refs)
    assert {
        (item["kind"], item["id"], item["classification"], item["selection"])
        for item in blocked
    } == {("source", source["id"], "confidential", "linked")}
    assert store.get_task(task["id"])["status"] == "needs_user"
    assert store.messages(task["id"])[-1]["content"] == "Продолжай задачу"
    audit = store._rows(
        "SELECT * FROM audit_log WHERE task_id=? AND action='llm.route_blocked'",
        (task["id"],),
    )[-1]
    assert "LINKED_SECRET_MARKER" not in json.dumps(audit)


def test_mixed_history_blocks_remote_turn_even_when_task_label_is_allowed(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path)
    workspace = store.create_workspace("Public workspace", classification="public")
    task = store.create_task(workspace["id"], "Internal task", classification="internal")
    store.add_message(task["id"], "user", "Allowed history", classification="internal")
    sensitive = store.add_message(
        task["id"],
        "assistant",
        "Sensitive derived answer",
        classification="confidential",
    )
    configure_route(store, "external")

    with pytest.raises(RoutingPolicyError) as raised:
        LocalOrchestrator(store).prepare_turn(
            "Новый разрешённый запрос",
            workspace_id=workspace["id"],
            task_id=task["id"],
        )

    assert any(
        item["kind"] == "message"
        and item["id"] == sensitive["id"]
        and item["classification"] == "confidential"
        and item["selection"] == "history"
        for item in raised.value.blocked_refs
    )
    assert [item["content"] for item in store.messages(task["id"])] == [
        "Allowed history",
        "Sensitive derived answer",
        "Новый разрешённый запрос",
    ]


def test_turn_taint_propagates_to_assistant_artifact_and_explicit_memory(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path)
    workspace = store.create_workspace("Public workspace", classification="public")
    task = store.create_task(workspace["id"], "Public task", classification="public")
    source = store.add_source(
        workspace["id"],
        "document",
        "Restricted linked source",
        "RESTRICTED_CONTEXT_MARKER",
        visibility="task",
        task_id=task["id"],
        classification="restricted",
    )
    configure_route(store, "local")
    orchestrator = LocalOrchestrator(store)

    turn = orchestrator.prepare_turn(
        "/document запомни, что правило действует только локально",
        workspace_id=workspace["id"],
        task_id=task["id"],
    )
    artifact = orchestrator.finish_turn(turn, "Результат на основе источника [S1].")
    saved_result = orchestrator.run_quick_action(
        "save_to_memory",
        task_id=task["id"],
    )

    assert turn.classification == "restricted"
    assert [item["id"] for item in turn.sources] == [source["id"]]
    messages = store.messages(task["id"])
    assert messages[-2]["role"] == "user"
    assert messages[-2]["classification"] == "public"
    assert messages[-1]["role"] == "assistant"
    assert messages[-1]["classification"] == "restricted"
    explicit_memory = store._rows(
        "SELECT * FROM memory WHERE workspace_id=? AND kind='explicit'",
        (workspace["id"],),
    )[-1]
    assert explicit_memory["classification"] == "restricted"
    assert artifact is not None
    assert artifact["classification"] == "restricted"
    assert store.artifact_versions(artifact["id"])[0]["classification"] == "restricted"
    assert saved_result["memory"]["classification"] == "restricted"


def test_ui_emits_routing_blocked_without_calling_external_chat(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    class RecordingExternalChat:
        def __init__(self) -> None:
            self.calls = 0

        def stream_reply(self, *_: Any, **__: Any):  # noqa: ANN202
            self.calls += 1
            yield "MUST_NOT_BE_CALLED"

        @staticmethod
        def remember(*_: Any, **__: Any) -> None:
            return None

    store = make_store(tmp_path)
    workspace = store.create_workspace("Public workspace", classification="public")
    task = store.create_task(
        workspace["id"],
        "Restricted task",
        classification="restricted",
    )
    configure_route(store, "external")
    backend = UIBackend(Config.defaults(), EventEmitter(), store)
    external_chat = RecordingExternalChat()
    backend.assistant.chat = external_chat  # type: ignore[assignment]
    backend.current_workspace_id = workspace["id"]
    backend.current_task_id = task["id"]
    capsys.readouterr()

    backend._text_turn("Не отправляй это наружу", speak=False)

    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    blocked = next(event for event in events if event["type"] == "routing_blocked")
    assert blocked["task_id"] == task["id"]
    assert blocked["route"] == "external"
    assert blocked["allowed_max"] == "internal"
    assert blocked["effective_classification"] == "restricted"
    assert external_chat.calls == 0
    assert not any(event["type"] in {"assistant_start", "assistant_delta"} for event in events)
    assert store.get_task(task["id"])["status"] == "needs_user"


@pytest.mark.parametrize(
    ("key", "attempted"),
    [
        ("external_provider_type", "corporate"),
        ("default_classification", "public"),
    ],
)
def test_generic_setting_command_refuses_provider_trust_and_policy_defaults(
    tmp_path: Path,
    key: str,
    attempted: str,
) -> None:
    store = make_store(tmp_path)
    backend = UIBackend(Config.defaults(), EventEmitter(), store)
    before = store.settings()[key]

    with pytest.raises(ValueError, match="специальными командами"):
        backend.handle(
            {
                "command": "setting",
                "key": key,
                "value": attempted,
            }
        )

    assert store.settings()[key] == before
