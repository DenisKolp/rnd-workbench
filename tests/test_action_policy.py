from __future__ import annotations

import json
import sqlite3

import pytest

from voice_assistant.config import Config
from voice_assistant.orchestrator import LocalOrchestrator
from voice_assistant.store import AssistantStore
from voice_assistant.ui_backend import EventEmitter, UIBackend


def make_store(tmp_path) -> AssistantStore:  # noqa: ANN001
    return AssistantStore(tmp_path / "assistant.sqlite3")


def test_v6_approval_and_audit_rows_migrate_to_action_policy_schema(tmp_path) -> None:  # noqa: ANN001
    database = tmp_path / "assistant.sqlite3"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE approvals (
            id TEXT PRIMARY KEY,
            task_id TEXT,
            action_type TEXT NOT NULL,
            title TEXT NOT NULL,
            payload TEXT NOT NULL DEFAULT '{}',
            risk TEXT NOT NULL DEFAULT 'confirm',
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE audit_log (
            id TEXT PRIMARY KEY,
            task_id TEXT,
            action TEXT NOT NULL,
            target TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL,
            detail TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );
        INSERT INTO approvals VALUES (
            'legacy-approval', NULL, 'email.send', 'Legacy', '{}',
            'confirm', 'pending', '2026-08-30T00:00:00+00:00',
            '2026-08-30T00:00:00+00:00'
        );
        INSERT INTO audit_log VALUES (
            'legacy-audit', NULL, 'approval.request', 'legacy-approval',
            'pending', '', '2026-08-30T00:00:00+00:00'
        );
        PRAGMA user_version = 6;
        """
    )
    connection.commit()
    connection.close()

    store = AssistantStore(database)
    approval = store._rows(
        "SELECT * FROM approvals WHERE id='legacy-approval'"
    )[0]
    audit = store._rows("SELECT * FROM audit_log WHERE id='legacy-audit'")[0]

    assert approval["workflow_id"] == "legacy-approval"
    assert approval["idempotency_key"] == "legacy:legacy-approval"
    assert approval["confirmation_policy"] == "explicit"
    assert approval["revision"] == 1
    assert audit["actor"] == "system"
    assert audit["origin"] == "application"
    assert store._connection.execute("PRAGMA user_version").fetchone()[0] == 7


def test_one_request_stages_ordered_multi_action_workflow(tmp_path) -> None:  # noqa: ANN001
    store = make_store(tmp_path)
    orchestrator = LocalOrchestrator(store)
    turn = orchestrator.prepare_turn(
        "Создай встречу, отправь материалы и напиши письмо участникам",
        workspace_id=store.default_workspace_id(),
        task_id=None,
    )

    approvals = store._rows(
        "SELECT * FROM approvals WHERE task_id=? ORDER BY step_index",
        (turn.task_id,),
    )
    assert [item["action_type"] for item in approvals] == [
        "calendar.create",
        "materials.send",
        "email.send",
    ]
    assert [item["step_index"] for item in approvals] == [1, 2, 3]
    assert len({item["workflow_id"] for item in approvals}) == 1
    assert len({item["idempotency_key"] for item in approvals}) == 3
    assert {item["confirmation_policy"] for item in approvals} == {"explicit"}
    assert [item["risk"] for item in approvals] == ["medium", "high", "high"]
    assert {item["actor"] for item in approvals} == {"local-user"}
    assert {item["origin"] for item in approvals} == {"user_request"}

    events = store._rows(
        "SELECT * FROM task_events WHERE task_id=? AND kind='action_plan'",
        (turn.task_id,),
    )
    assert len(events) == 1
    assert "Calendar" in events[0]["detail"]
    assert "Передача материалов" in events[0]["detail"]


def test_action_idempotency_is_deterministic_and_deduplicates_restage(tmp_path) -> None:  # noqa: ANN001
    store = make_store(tmp_path)
    task = store.create_task(store.default_workspace_id(), "Отправить письмо")
    orchestrator = LocalOrchestrator(store)
    request = "Напиши письмо о статусе проекта"

    orchestrator._stage_external_action(request, task["id"])
    first = store._rows(
        "SELECT * FROM approvals WHERE task_id=?", (task["id"],)
    )
    orchestrator._stage_external_action(request, task["id"])
    second = store._rows(
        "SELECT * FROM approvals WHERE task_id=?", (task["id"],)
    )

    assert len(first) == len(second) == 1
    assert first[0]["id"] == second[0]["id"]
    assert first[0]["idempotency_key"] == second[0]["idempotency_key"]


def test_significant_action_cannot_disable_confirmation(tmp_path) -> None:  # noqa: ANN001
    store = make_store(tmp_path)
    task = store.create_task(store.default_workspace_id(), "Внешнее действие")

    with pytest.raises(ValueError, match="требует подтверждения"):
        store.create_approval(
            task["id"],
            "email.send",
            "Письмо",
            {"request": "hello"},
            risk="high",
            confirmation_policy="none",
        )


def test_workflow_steps_cannot_execute_out_of_order(tmp_path) -> None:  # noqa: ANN001
    store = make_store(tmp_path)
    task = store.create_task(store.default_workspace_id(), "Последовательный план")
    first = store.create_approval(
        task["id"],
        "calendar.create",
        "Шаг 1",
        {"request": "Встреча"},
        workflow_id="ordered-workflow",
        step_index=1,
    )
    second = store.create_approval(
        task["id"],
        "email.send",
        "Шаг 2",
        {"request": "Письмо"},
        workflow_id="ordered-workflow",
        step_index=2,
    )

    store.resolve_approval(second["id"], "approved")
    with pytest.raises(ValueError, match="Сначала должен успешно завершиться шаг 1"):
        store.begin_approval_execution(second["id"])

    store.resolve_approval(first["id"], "approved")
    store.begin_approval_execution(first["id"])
    store.complete_approval_execution(
        first["id"],
        success=True,
        result_code="executor_acknowledged",
        result="Исполнитель подтвердил выполнение",
        actor="test-executor",
    )
    executing = store.begin_approval_execution(second["id"])
    assert executing["status"] == "executing"


def test_rejection_is_final_and_cancels_later_workflow_steps(tmp_path) -> None:  # noqa: ANN001
    store = make_store(tmp_path)
    task = store.create_task(store.default_workspace_id(), "Цепочка")
    first = store.create_approval(
        task["id"],
        "calendar.create",
        "Шаг 1",
        {"request": "Секретный текст 123", "connected": False},
        workflow_id="workflow-1",
        step_index=1,
    )
    second = store.create_approval(
        task["id"],
        "email.send",
        "Шаг 2",
        {"request": "Другой секрет", "connected": False},
        workflow_id="workflow-1",
        step_index=2,
    )

    rejected = store.resolve_approval(first["id"], "rejected", actor="denis")
    store.cancel_approval_dependents(first["id"], actor="system")

    assert rejected["status"] == "rejected"
    assert rejected["resolved_by"] == "denis"
    assert rejected["resolved_at"]
    assert store._rows("SELECT status FROM approvals WHERE id=?", (second["id"],))[0][
        "status"
    ] == "cancelled"
    with pytest.raises(ValueError, match="Изменить можно"):
        store.update_approval_payload(first["id"], {"request": "Новый план"})

    audit = store.approval_history(first["id"])
    assert [item["status"] for item in audit] == ["pending", "rejected"]
    assert audit[-1]["actor"] == "denis"
    assert all("Секретный текст" not in item["detail"] for item in audit)


def test_error_can_be_edited_and_replanned_with_new_revision(tmp_path) -> None:  # noqa: ANN001
    store = make_store(tmp_path)
    task = store.create_task(store.default_workspace_id(), "Письмо")
    approval = store.create_approval(
        task["id"],
        "email.send",
        "Письмо",
        {"request": "Черновик 1", "connected": False},
        workflow_id="workflow-replan",
        step_index=1,
    )
    old_key = approval["idempotency_key"]

    store.resolve_approval(approval["id"], "approved", actor="denis")
    store.begin_approval_execution(approval["id"])
    failed = store.complete_approval_execution(
        approval["id"],
        success=False,
        result_code="executor_not_connected",
        result="Исполнитель не подключён",
    )
    assert failed["status"] == "error"

    replanned = store.update_approval_payload(
        approval["id"],
        {"request": "Черновик 2", "connected": False},
        actor="denis",
    )
    assert replanned["status"] == "pending"
    assert replanned["revision"] == 2
    assert replanned["idempotency_key"] != old_key
    assert replanned["result"] == ""
    assert replanned["resolved_at"] is None
    assert [item["action"] for item in store.approval_history(approval["id"])] == [
        "approval.request",
        "approval.decision",
        "approval.execute",
        "approval.execute",
        "approval.replan",
    ]


def test_restart_recovers_inflight_action_without_retrying_or_claiming_success(
    tmp_path,
) -> None:  # noqa: ANN001
    database = tmp_path / "assistant.sqlite3"
    store = AssistantStore(database)
    task = store.create_task(store.default_workspace_id(), "Прерванное действие")
    approval = store.create_approval(
        task["id"],
        "email.send",
        "Письмо",
        {"request": "CONFIDENTIAL_PAYLOAD", "connected": False},
    )
    store.resolve_approval(approval["id"], "approved")
    store.begin_approval_execution(approval["id"])
    store.close()

    recovered = AssistantStore(database)
    row = recovered._rows(
        "SELECT * FROM approvals WHERE id=?", (approval["id"],)
    )[0]

    assert row["status"] == "error"
    assert "результат не подтверждён" in row["result"].casefold()
    assert recovered.get_task(task["id"])["status"] == "needs_user"
    history = recovered.approval_history(approval["id"])
    assert history[-1]["action"] == "approval.recover"
    assert history[-1]["status"] == "error"
    assert "CONFIDENTIAL_PAYLOAD" not in history[-1]["detail"]


@pytest.mark.parametrize("connected", [False, True])
def test_ui_approval_without_real_executor_finishes_in_visible_error(
    capsys,
    tmp_path,
    connected: bool,
) -> None:  # noqa: ANN001
    store = make_store(tmp_path)
    task = store.create_task(store.default_workspace_id(), "Отправить письмо")
    approval = store.create_approval(
        task["id"],
        "email.send",
        "Письмо",
        {
            "request": "TOP_SECRET: внутренний черновик",
            "capability": "Email",
            "connected": connected,
        },
        risk="high",
        actor="local-user",
        origin="user_request",
        workflow_id="workflow-ui",
        step_index=1,
    )
    backend = UIBackend(Config.defaults(), EventEmitter(), store)
    capsys.readouterr()

    backend._resolve_approval(approval["id"], "approved")

    updated = store._rows("SELECT * FROM approvals WHERE id=?", (approval["id"],))[0]
    assert updated["status"] == "error"
    assert "не " in updated["result"].casefold()
    assert store.get_task(task["id"])["status"] == "needs_user"
    assert store._rows(
        "SELECT * FROM inbox WHERE source_ref=? AND kind='error'", (approval["id"],)
    )
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert any(item["type"] == "approval_execution_failed" for item in events)
    assert any(item["type"] == "error" for item in events)
    assert any(item["type"] == "snapshot" for item in events)

    audit = store.approval_history(approval["id"])
    assert audit[-1]["status"] == "error"
    assert all("TOP_SECRET" not in item["detail"] for item in audit)


def test_ui_rejection_completes_task_without_external_success(tmp_path) -> None:  # noqa: ANN001
    store = make_store(tmp_path)
    task = store.create_task(store.default_workspace_id(), "Не отправлять")
    approval = store.create_approval(
        task["id"],
        "email.send",
        "Письмо",
        {"request": "Отменить", "connected": False},
    )
    backend = UIBackend(Config.defaults(), EventEmitter(), store)

    backend._resolve_approval(approval["id"], "rejected")

    updated = store._rows("SELECT * FROM approvals WHERE id=?", (approval["id"],))[0]
    assert updated["status"] == "rejected"
    assert store.get_task(task["id"])["status"] == "done"
    history = store.approval_history(approval["id"])
    assert not any(item["status"] == "succeeded" for item in history)
