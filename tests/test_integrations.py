from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import threading

import pytest

from voice_assistant.integrations import (
    InMemoryIntegrationAdapter,
    IntegrationIntent,
    IntegrationRequest,
    IntegrationResult,
    IntegrationUnavailable,
    SafeIntegrationHub,
    action_policy,
)
from voice_assistant.store import AssistantStore


def make_hub(tmp_path, system: str = "jira"):  # noqa: ANN001, ANN201
    store = AssistantStore(tmp_path / "assistant.sqlite3")
    adapter = InMemoryIntegrationAdapter(system)
    hub = SafeIntegrationHub(store)
    hub.register(adapter)
    return store, hub, adapter


class BlockingIntegrationAdapter(InMemoryIntegrationAdapter):
    """Expose duplicate connector entry without relying on scheduler timing."""

    def __init__(self, system: str) -> None:
        super().__init__(system)
        self.entered = threading.Event()
        self.release = threading.Event()
        self.overlap = threading.Event()
        self.call_count = 0
        self._active = 0
        self._guard = threading.Lock()

    def execute(  # noqa: ANN201
        self,
        operation: str,
        payload,  # noqa: ANN001
        *,
        idempotency_key: str,
    ):
        with self._guard:
            self.call_count += 1
            self._active += 1
            if self._active > 1:
                self.overlap.set()
        self.entered.set()
        try:
            if not self.release.wait(timeout=5):
                raise TimeoutError("test did not release connector")
            return super().execute(
                operation,
                payload,
                idempotency_key=idempotency_key,
            )
        finally:
            with self._guard:
                self._active -= 1


@pytest.mark.parametrize(
    ("intent", "user_policy", "store_policy", "risk"),
    [
        ("read", "none", "none", "low"),
        ("draft", "none", "none", "low"),
        ("write", "preview", "explicit", "medium"),
        ("publish", "explicit", "two_step", "high"),
        ("delete", "explicit", "two_step", "high"),
        ("permissions", "explicit", "two_step", "critical"),
        ("mass", "explicit", "two_step", "critical"),
    ],
)
def test_product_autonomy_policy_maps_to_storage_contract(
    intent: str,
    user_policy: str,
    store_policy: str,
    risk: str,
) -> None:
    policy = action_policy(intent)
    assert (policy.user_policy, policy.store_policy, policy.risk) == (
        user_policy,
        store_policy,
        risk,
    )


def test_read_and_draft_do_not_create_approval(tmp_path) -> None:  # noqa: ANN001
    store, hub, _ = make_hub(tmp_path)
    result = hub.read(
        IntegrationRequest(
            "jira",
            "issues.search",
            IntegrationIntent.READ,
            {"query": "RND"},
        )
    )
    preview = hub.draft(
        IntegrationRequest(
            "jira",
            "issue.create",
            IntegrationIntent.DRAFT,
            {"summary": "Подготовить пилот"},
        )
    )

    assert result.status == "simulated"
    assert "Тестовый" in preview.summary
    assert store._rows("SELECT * FROM approvals") == []


def test_write_is_staged_with_preview_and_never_executes_before_approval(
    tmp_path,
) -> None:  # noqa: ANN001
    store, hub, adapter = make_hub(tmp_path)
    task = store.create_task(store.default_workspace_id(), "Поставить задачу")
    approval = hub.stage(
        IntegrationRequest(
            "jira",
            "issue.create",
            IntegrationIntent.WRITE,
            {"summary": "Подготовить пилот", "assignee": "team"},
        ),
        task_id=task["id"],
    )

    payload = json.loads(approval["payload"])
    assert approval["confirmation_policy"] == "explicit"
    assert approval["status"] == "pending"
    assert payload["preview"]["summary"].startswith("Тестовый")
    assert adapter.executions == []
    with pytest.raises(ValueError, match="подтверждённое"):
        hub.execute_approved(approval["id"])


def test_approved_action_executes_once_with_persisted_idempotency(tmp_path) -> None:  # noqa: ANN001
    store, hub, adapter = make_hub(tmp_path, "kaiten")
    task = store.create_task(store.default_workspace_id(), "Создать карточку")
    approval = hub.stage(
        IntegrationRequest(
            "kaiten",
            "card.create",
            IntegrationIntent.WRITE,
            {"title": "Разобрать решения встречи"},
        ),
        task_id=task["id"],
    )
    store.resolve_approval(approval["id"], "approved")

    first = hub.execute_approved(approval["id"])
    second = hub.execute_approved(approval["id"])

    assert first.status == "simulated"
    assert first.production is False
    assert second.status == "simulated"
    assert second.production is False
    assert len(adapter.executions) == 1
    row = store._rows("SELECT * FROM approvals WHERE id=?", (approval["id"],))[0]
    assert row["status"] == "succeeded"
    assert "локальном тестовом контуре" in row["result"]


def test_concurrent_execution_reserves_one_idempotency_key_before_connector(
    tmp_path,
) -> None:  # noqa: ANN001
    store = AssistantStore(tmp_path / "assistant.sqlite3")
    adapter = BlockingIntegrationAdapter("jira")
    hub = SafeIntegrationHub(store)
    hub.register(adapter)
    approval = hub.stage(
        IntegrationRequest(
            "jira",
            "issue.create",
            IntegrationIntent.WRITE,
            {"summary": "Одна корпоративная задача"},
        ),
        task_id=None,
    )
    store.resolve_approval(approval["id"], "approved")

    with ThreadPoolExecutor(max_workers=2) as executor:
        first_future = executor.submit(hub.execute_approved, approval["id"])
        assert adapter.entered.wait(timeout=2)
        second_future = executor.submit(hub.execute_approved, approval["id"])
        try:
            assert not adapter.overlap.wait(timeout=0.5)
        finally:
            adapter.release.set()
        first = first_future.result(timeout=2)
        second = second_future.result(timeout=2)

    assert (first.status, first.production) == ("simulated", False)
    assert (second.status, second.production) == ("simulated", False)
    assert adapter.call_count == 1
    assert len(adapter.executions) == 1
    succeeded_audits = store._rows(
        """
        SELECT * FROM audit_log
        WHERE target=? AND action='approval.execute' AND status='succeeded'
        """,
        (approval["id"],),
    )
    assert len(succeeded_audits) == 1


def test_connector_cannot_mark_simulated_result_as_production(tmp_path) -> None:  # noqa: ANN001
    class MisreportingAdapter(InMemoryIntegrationAdapter):
        production = True

        def execute(  # noqa: ANN201
            self,
            operation: str,
            payload,  # noqa: ANN001
            *,
            idempotency_key: str,
        ):
            return IntegrationResult(
                status="simulated",
                message="Тестовый результат",
                production=True,
            )

    store = AssistantStore(tmp_path / "assistant.sqlite3")
    hub = SafeIntegrationHub(store)
    hub.register(MisreportingAdapter("confluence"))
    approval = hub.stage(
        IntegrationRequest(
            "confluence",
            "page.update",
            IntegrationIntent.WRITE,
            {"title": "Пилот"},
        ),
        task_id=None,
    )
    store.resolve_approval(approval["id"], "approved")

    first = hub.execute_approved(approval["id"])
    replay = hub.execute_approved(approval["id"])

    assert (first.status, first.production) == ("simulated", False)
    assert (replay.status, replay.production) == ("simulated", False)


def test_missing_connector_stages_preview_but_never_claims_external_success(
    tmp_path,
) -> None:  # noqa: ANN001
    store = AssistantStore(tmp_path / "assistant.sqlite3")
    hub = SafeIntegrationHub(store)
    request = IntegrationRequest(
        "confluence",
        "page.update",
        IntegrationIntent.WRITE,
        {"title": "Статус"},
    )

    approval = hub.stage(request, task_id=None)
    assert approval["status"] == "pending"
    payload = json.loads(approval["payload"])
    assert payload["production_connector"] is False
    assert "не подключён" in payload["preview"]["summary"]

    store.resolve_approval(approval["id"], "approved")
    result = hub.execute_approved(approval["id"])
    assert result.status == "error"
    assert "не подключена" in result.message
    assert store._rows(
        "SELECT status FROM approvals WHERE id=?", (approval["id"],)
    )[0]["status"] == "error"


@pytest.mark.parametrize(
    "field_name",
    (
        "auth_token",
        "accessToken",
        "refreshToken",
        "clientSecret",
        "authorizationHeader",
        "cookieJar",
    ),
)
def test_secret_like_fields_are_rejected_before_storage(
    tmp_path,
    field_name: str,
) -> None:  # noqa: ANN001
    store, hub, _ = make_hub(tmp_path)
    request = IntegrationRequest(
        "jira",
        "issue.create",
        IntegrationIntent.WRITE,
        {"summary": "Текст", field_name: "must-not-be-stored"},
    )

    with pytest.raises(ValueError, match="Секреты нельзя"):
        hub.stage(request, task_id=None)
    serialized = json.dumps(store.snapshot(), ensure_ascii=False)
    assert "must-not-be-stored" not in serialized


def test_adapter_classification_limit_blocks_payload_before_connector(tmp_path) -> None:  # noqa: ANN001
    store = AssistantStore(tmp_path / "assistant.sqlite3")

    class InternalOnlyAdapter(InMemoryIntegrationAdapter):
        max_classification = "internal"

    hub = SafeIntegrationHub(store)
    hub.register(InternalOnlyAdapter("jira"))
    request = IntegrationRequest(
        "jira",
        "issues.search",
        IntegrationIntent.READ,
        {"query": "проект"},
        classification="confidential",
    )

    with pytest.raises(IntegrationUnavailable, match="выше разрешённого"):
        hub.read(request)

    assert store._rows("SELECT * FROM audit_log WHERE action='integration.read'") == []


def test_unknown_integration_classification_is_rejected() -> None:
    with pytest.raises(ValueError, match="Классификация"):
        IntegrationRequest(
            "jira",
            "issues.search",
            IntegrationIntent.READ,
            {},
            classification="top-secret",
        ).normalized()


def test_audit_contains_shape_not_work_content(tmp_path) -> None:  # noqa: ANN001
    store, hub, _ = make_hub(tmp_path)
    secret_work_text = "Внутренний проект Север 4821"
    hub.draft(
        IntegrationRequest(
            "jira",
            "issue.create",
            IntegrationIntent.DRAFT,
            {"summary": secret_work_text, "assignee": "team"},
        )
    )

    rows = store._rows("SELECT detail FROM audit_log WHERE action='integration.draft'")
    assert len(rows) == 1
    assert secret_work_text not in rows[0]["detail"]
    assert json.loads(rows[0]["detail"])["parameter_keys"] == [
        "assignee",
        "summary",
    ]


def test_connector_exception_is_recorded_without_exception_message(tmp_path) -> None:  # noqa: ANN001
    store = AssistantStore(tmp_path / "assistant.sqlite3")
    adapter = InMemoryIntegrationAdapter("mail", fail_operations={"message.send"})
    hub = SafeIntegrationHub(store)
    hub.register(adapter)
    task = store.create_task(store.default_workspace_id(), "Отправить письмо")
    approval = hub.stage(
        IntegrationRequest(
            "mail",
            "message.send",
            IntegrationIntent.WRITE,
            {"body": "Совершенно секретный текст"},
        ),
        task_id=task["id"],
    )
    store.resolve_approval(approval["id"], "approved")

    result = hub.execute_approved(approval["id"])

    assert result.status == "error"
    assert "RuntimeError" in result.message
    assert "simulated connector failure" not in result.message
    audit = json.dumps(store.approval_history(approval["id"]), ensure_ascii=False)
    assert "Совершенно секретный текст" not in audit


def test_high_risk_operations_require_two_step_confirmation(tmp_path) -> None:  # noqa: ANN001
    store, hub, _ = make_hub(tmp_path, "confluence")
    for index, intent in enumerate(
        (
            IntegrationIntent.DELETE,
            IntegrationIntent.PERMISSIONS,
            IntegrationIntent.PUBLISH,
            IntegrationIntent.MASS,
        ),
        start=1,
    ):
        approval = hub.stage(
            IntegrationRequest(
                "confluence",
                f"operation.{index}",
                intent,
                {"page_id": str(index)},
            ),
            task_id=None,
        )
        assert approval["confirmation_policy"] == "two_step"
