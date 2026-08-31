from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
import hashlib
import json
import re
import threading
import weakref
from typing import Any, Mapping, Protocol

from .java_core import (
    ActionJournalRuntime,
    JavaActionExecution,
    JavaCoreProtocolError,
    JavaCoreUnavailable,
)
from .store import (
    CLASSIFICATION_RANK,
    AssistantStore,
    normalize_classification,
    utc_now,
)


_EXECUTION_LOCKS_GUARD = threading.Lock()
_EXECUTION_LOCKS: weakref.WeakValueDictionary[tuple[str, str], Any] = (
    weakref.WeakValueDictionary()
)


class IntegrationIntent(StrEnum):
    """User-visible effect of an integration operation.

    The intent is deliberately independent from a vendor API method.  This keeps
    the confirmation boundary stable when Jira, Kaiten or a mail provider uses a
    different endpoint for the same user-visible action.
    """

    READ = "read"
    DRAFT = "draft"
    WRITE = "write"
    PUBLISH = "publish"
    DELETE = "delete"
    PERMISSIONS = "permissions"
    MASS = "mass"


@dataclass(frozen=True, slots=True)
class ActionPolicy:
    user_policy: str
    store_policy: str
    risk: str
    requires_preview: bool


_ACTION_POLICIES: dict[IntegrationIntent, ActionPolicy] = {
    IntegrationIntent.READ: ActionPolicy("none", "none", "low", False),
    IntegrationIntent.DRAFT: ActionPolicy("none", "none", "low", False),
    IntegrationIntent.WRITE: ActionPolicy("preview", "explicit", "medium", True),
    IntegrationIntent.PUBLISH: ActionPolicy("explicit", "two_step", "high", True),
    IntegrationIntent.DELETE: ActionPolicy("explicit", "two_step", "high", True),
    IntegrationIntent.PERMISSIONS: ActionPolicy(
        "explicit", "two_step", "critical", True
    ),
    IntegrationIntent.MASS: ActionPolicy("explicit", "two_step", "critical", True),
}


def action_policy(intent: IntegrationIntent | str) -> ActionPolicy:
    try:
        normalized = IntegrationIntent(str(intent).casefold().strip())
    except ValueError as exc:
        raise ValueError(f"Неизвестный тип внешнего действия: {intent}") from exc
    return _ACTION_POLICIES[normalized]


@dataclass(frozen=True, slots=True)
class IntegrationRequest:
    system: str
    operation: str
    intent: IntegrationIntent
    payload: Mapping[str, Any] = field(default_factory=dict)
    title: str = ""
    classification: str = "internal"

    def normalized(self) -> "IntegrationRequest":
        system = _identifier(self.system, "Система интеграции")
        operation = _identifier(self.operation, "Операция интеграции", allow_dot=True)
        payload = dict(self.payload)
        _reject_secrets(payload)
        title = re.sub(r"\s+", " ", self.title).strip()
        return IntegrationRequest(
            system=system,
            operation=operation,
            intent=IntegrationIntent(self.intent),
            payload=payload,
            title=title,
            classification=normalize_classification(self.classification),
        )


@dataclass(frozen=True, slots=True)
class IntegrationPreview:
    title: str
    summary: str
    fields: Mapping[str, str] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class IntegrationResult:
    status: str
    message: str
    external_id: str = ""
    production: bool = True
    data: Mapping[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status in {"succeeded", "simulated"}


class IntegrationAdapter(Protocol):
    """A narrow, synchronous boundary around a corporate system connector."""

    system: str
    production: bool
    max_classification: str

    def read(self, operation: str, payload: Mapping[str, Any]) -> IntegrationResult:
        ...

    def draft(self, operation: str, payload: Mapping[str, Any]) -> IntegrationPreview:
        ...

    def preview(self, operation: str, payload: Mapping[str, Any]) -> IntegrationPreview:
        ...

    def execute(
        self,
        operation: str,
        payload: Mapping[str, Any],
        *,
        idempotency_key: str,
    ) -> IntegrationResult:
        ...

    def reconcile(
        self,
        operation: str,
        payload: Mapping[str, Any],
        *,
        idempotency_key: str,
    ) -> IntegrationResult | None:
        """Return a definitive prior result, or ``None`` when still unknown."""
        ...


class IntegrationUnavailable(RuntimeError):
    pass


class SafeIntegrationHub:
    """Routes corporate actions through one auditable confirmation boundary.

    Credentials are intentionally outside this API.  Adapters receive only a
    sanitized operation payload; access tokens must live in the platform secret
    store or in the adapter's process environment.
    """

    def __init__(
        self,
        store: AssistantStore,
        *,
        action_journal: ActionJournalRuntime | None = None,
    ) -> None:
        self.store = store
        self.action_journal = action_journal
        self._adapters: dict[str, IntegrationAdapter] = {}

    def action_journal_diagnostics(self) -> dict[str, Any]:
        journal = self.action_journal
        return {
            "configured": bool(journal and getattr(journal, "configured", False)),
            "ready": bool(journal and getattr(journal, "ready", False)),
            "production_fail_closed": True,
            "content_transmitted": False,
        }

    def register(self, adapter: IntegrationAdapter) -> None:
        system = _identifier(adapter.system, "Система интеграции")
        if system in self._adapters:
            raise ValueError(f"Исполнитель {system} уже зарегистрирован")
        self._adapters[system] = adapter

    def connected_systems(self) -> tuple[str, ...]:
        return tuple(sorted(self._adapters))

    def read(self, request: IntegrationRequest) -> IntegrationResult:
        request = request.normalized()
        if request.intent is not IntegrationIntent.READ:
            raise ValueError("Через read допустимы только операции чтения")
        adapter = self._adapter(request.system)
        _assert_classification_allowed(request.classification, adapter)
        result = adapter.read(request.operation, request.payload)
        self.store.audit(
            None,
            "integration.read",
            request.system,
            result.status,
            _safe_audit_detail(request, production=adapter.production),
            actor="local-user",
            origin="integration_hub",
        )
        return result

    def draft(self, request: IntegrationRequest) -> IntegrationPreview:
        request = request.normalized()
        if request.intent is not IntegrationIntent.DRAFT:
            raise ValueError("Через draft допустима только подготовка черновика")
        adapter = self._adapter(request.system)
        _assert_classification_allowed(request.classification, adapter)
        preview = adapter.draft(request.operation, request.payload)
        self.store.audit(
            None,
            "integration.draft",
            request.system,
            "prepared",
            _safe_audit_detail(request, production=adapter.production),
            actor="local-user",
            origin="integration_hub",
        )
        return preview

    def stage(
        self,
        request: IntegrationRequest,
        *,
        task_id: str | None,
        workflow_id: str | None = None,
        step_index: int = 0,
        actor: str = "local-user",
        origin: str = "assistant",
    ) -> dict[str, Any]:
        request = request.normalized()
        policy = action_policy(request.intent)
        if not policy.requires_preview:
            raise ValueError("Чтение и черновики не нужно помещать в согласования")
        adapter = self._adapters.get(request.system)
        if adapter is not None:
            _assert_classification_allowed(request.classification, adapter)
        preview = (
            adapter.preview(request.operation, request.payload)
            if adapter is not None
            else _disconnected_preview(request)
        )
        payload = {
            "integration": request.system,
            "operation": request.operation,
            "intent": request.intent.value,
            "parameters": dict(request.payload),
            "preview": preview.as_dict(),
            "classification": request.classification,
            "production_connector": bool(adapter and adapter.production),
        }
        title = request.title or preview.title or (
            f"{request.system}: {request.operation}"
        )
        return self.store.create_approval(
            task_id,
            f"{request.system}.{request.operation}",
            title,
            payload,
            risk=policy.risk,
            actor=actor,
            origin=origin,
            workflow_id=workflow_id,
            step_index=step_index,
            confirmation_policy=policy.store_policy,
        )

    def execute_approved(
        self,
        approval_id: str,
        *,
        actor: str = "system",
    ) -> IntegrationResult:
        rows = self.store._rows("SELECT * FROM approvals WHERE id=?", (approval_id,))
        if not rows:
            raise KeyError(approval_id)
        idempotency_key = str(rows[0].get("idempotency_key") or approval_id)
        lock = _execution_lock(self.store, idempotency_key)
        with lock:
            return self._execute_approved_locked(approval_id, actor=actor)

    def _execute_approved_locked(
        self,
        approval_id: str,
        *,
        actor: str,
    ) -> IntegrationResult:
        rows = self.store._rows("SELECT * FROM approvals WHERE id=?", (approval_id,))
        if not rows:
            raise KeyError(approval_id)
        approval = rows[0]
        if approval["status"] == "succeeded":
            return self._stored_success_result(approval)
        if approval["status"] == "executing":
            return IntegrationResult(
                status="in_progress",
                message="Действие уже выполняется",
                production=False,
            )
        if approval["status"] != "approved":
            raise ValueError("Выполнить можно только подтверждённое действие")
        try:
            payload = json.loads(approval["payload"] or "{}")
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("Сохранённые параметры действия повреждены") from exc
        if not isinstance(payload, dict):
            raise ValueError("Сохранённые параметры действия повреждены")
        system = _identifier(payload.get("integration", ""), "Система интеграции")
        operation = _identifier(
            payload.get("operation", ""), "Операция интеграции", allow_dot=True
        )
        parameters = payload.get("parameters", {})
        if not isinstance(parameters, dict):
            raise ValueError("Параметры внешнего действия повреждены")
        _reject_secrets(parameters)
        classification = normalize_classification(payload.get("classification"))
        adapter = self._adapters.get(system)
        if adapter is None:
            message = f"Интеграция {system} не подключена; действие не выполнено"
            self.store.complete_approval_execution(
                approval_id,
                success=False,
                result_code="connector_not_connected",
                result=message,
                actor=actor,
                origin="integration_hub",
            )
            return IntegrationResult(
                status="error",
                message=message,
                production=False,
            )
        _assert_classification_allowed(classification, adapter)
        executing, claimed = self._claim_approval_execution(
            approval_id,
            actor=actor,
        )
        if not claimed:
            if executing["status"] == "succeeded":
                return self._stored_success_result(executing)
            if executing["status"] == "executing":
                return IntegrationResult(
                    status="in_progress",
                    message="Действие уже выполняется",
                    production=False,
                )
            raise ValueError("Состояние согласования уже изменилось")

        fingerprint = _action_request_fingerprint(executing, payload)
        journal = self.action_journal
        journal_ready = _action_journal_ready(journal)
        claim_token: str | None = None
        if journal_ready and journal is not None:
            try:
                claim = journal.claim_action(
                    idempotency_key=str(executing["idempotency_key"]),
                    request_fingerprint=fingerprint,
                )
            except (
                AttributeError,
                JavaCoreUnavailable,
                JavaCoreProtocolError,
                ValueError,
            ):
                claim = None
                journal_ready = False
            if claim is not None:
                self._audit_java_state(
                    executing,
                    action="approval.java_claim",
                    status=claim.disposition.casefold(),
                    result_code=f"java_claim_{claim.disposition.casefold()}",
                    actor=actor,
                )
                if claim.disposition == "REPLAY" and claim.result is not None:
                    return self._apply_java_result(
                        executing,
                        claim.result,
                        actor=actor,
                        origin="java_action_replay",
                    )
                if claim.disposition != "CLAIMED" or not claim.claim_token:
                    message = (
                        "Java core уже видит действие в выполнении; автоматический "
                        "повтор заблокирован и требуется сверка с внешней системой"
                        if claim.disposition == "IN_PROGRESS"
                        else "Java core обнаружил конфликт идентичности действия; выполнение заблокировано"
                    )
                    return self._fail_local_execution(
                        executing,
                        message,
                        result_code=f"java_claim_{claim.disposition.casefold()}",
                        actor=actor,
                        production=bool(adapter.production),
                    )
                claim_token = claim.claim_token

        if adapter.production and (not journal_ready or claim_token is None):
            return self._fail_local_execution(
                executing,
                "Защитный журнал Java core недоступен; корпоративное действие не выполнено",
                result_code="java_action_journal_unavailable",
                actor=actor,
                production=True,
            )
        try:
            result = adapter.execute(
                operation,
                parameters,
                idempotency_key=str(executing["idempotency_key"]),
            )
        except Exception as exc:
            message = f"{system}: исполнитель вернул ошибку {type(exc).__name__}"
            connector_error = IntegrationResult(
                status="error",
                message=message,
                production=bool(adapter.production),
            )
            return self._finish_claimed_execution(
                executing,
                connector_error,
                claim_token=claim_token,
                fingerprint=fingerprint,
                result_code="CONNECTOR.ERROR",
                actor=actor,
            )
        result = _normalize_execution_result(result, adapter=adapter)
        return self._finish_claimed_execution(
            executing,
            result,
            claim_token=claim_token,
            fingerprint=fingerprint,
            result_code=(
                "PRODUCTION.SUCCESS"
                if result.ok and result.production
                else "SIMULATED.SUCCESS"
                if result.ok
                else f"CONNECTOR.{_safe_result_code(result.status)}"
            ),
            actor=actor,
        )

    def _finish_claimed_execution(
        self,
        approval: Mapping[str, Any],
        result: IntegrationResult,
        *,
        claim_token: str | None,
        fingerprint: str,
        result_code: str,
        actor: str,
    ) -> IntegrationResult:
        journal = self.action_journal
        if claim_token is not None and journal is not None:
            try:
                completion = journal.complete_action(
                    idempotency_key=str(approval["idempotency_key"]),
                    request_fingerprint=fingerprint,
                    claim_token=claim_token,
                    outcome="SUCCESS" if result.ok else "FAILURE",
                    result_code=result_code,
                    external_reference=_safe_external_reference(result.external_id),
                )
                completion_confirmed = completion.disposition in {"RECORDED", "REPLAY"}
            except (
                AttributeError,
                JavaCoreUnavailable,
                JavaCoreProtocolError,
                ValueError,
            ):
                completion = None
                completion_confirmed = False
            self._audit_java_state(
                approval,
                action="approval.java_complete",
                status=(
                    completion.disposition.casefold()
                    if completion is not None
                    else "unconfirmed"
                ),
                result_code=(
                    f"java_complete_{completion.disposition.casefold()}"
                    if completion is not None
                    else "java_completion_unconfirmed"
                ),
                actor=actor,
            )
            if not completion_confirmed:
                return self._fail_local_execution(
                    approval,
                    "Внешний исполнитель ответил, но Java core не подтвердил запись результата; повтор заблокирован до сверки",
                    result_code="java_completion_unconfirmed",
                    actor=actor,
                    production=bool(result.production),
                )

        self.store.complete_approval_execution(
            str(approval["id"]),
            success=result.ok,
            result_code=(
                "production_success"
                if result.ok and result.production
                else "simulated"
                if result.ok
                else f"connector_{result.status}"
            ),
            result=result.message,
            actor=actor,
            origin="integration_hub",
        )
        return result

    def _fail_local_execution(
        self,
        approval: Mapping[str, Any],
        message: str,
        *,
        result_code: str,
        actor: str,
        production: bool,
    ) -> IntegrationResult:
        self.store.complete_approval_execution(
            str(approval["id"]),
            success=False,
            result_code=result_code,
            result=message,
            actor=actor,
            origin="integration_hub",
        )
        return IntegrationResult(
            status="error",
            message=message,
            production=production,
        )

    def _apply_java_result(
        self,
        approval: Mapping[str, Any],
        result: JavaActionExecution,
        *,
        actor: str,
        origin: str,
        recovery: bool = False,
    ) -> IntegrationResult:
        success = result.outcome == "SUCCESS"
        simulated = success and result.result_code == "SIMULATED.SUCCESS"
        local_result_code = (
            "simulated"
            if simulated
            else "production_success"
            if success
            else result.result_code.casefold()
        )
        external_suffix = (
            f" · ссылка {result.external_reference}"
            if result.external_reference
            else ""
        )
        message = (
            "Java core подтвердил ранее выполненное действие"
            if success
            else "Java core подтвердил ранее завершившуюся ошибку"
        ) + external_suffix
        if recovery:
            self.store.reconcile_approval_execution(
                str(approval["id"]),
                success=success,
                result_code=local_result_code,
                result=message,
                actor=actor,
                origin=origin,
            )
        else:
            self.store.complete_approval_execution(
                str(approval["id"]),
                success=success,
                result_code=local_result_code,
                result=message,
                actor=actor,
                origin=origin,
            )
        return IntegrationResult(
            status="simulated" if simulated else "succeeded" if success else "error",
            message=message,
            external_id=result.external_reference or "",
            production=bool(success and not simulated),
        )

    def _audit_java_state(
        self,
        approval: Mapping[str, Any],
        *,
        action: str,
        status: str,
        result_code: str,
        actor: str,
    ) -> None:
        self.store.audit(
            approval.get("task_id"),
            action,
            str(approval["id"]),
            status,
            self.store._approval_audit_detail(
                action_type=str(approval["action_type"]),
                risk=str(approval["risk"]),
                confirmation_policy=str(approval["confirmation_policy"]),
                workflow_id=str(approval["workflow_id"]),
                step_index=int(approval["step_index"]),
                revision=int(approval["revision"]),
                result_code=result_code,
            ),
            actor=actor,
            origin="java_core",
        )

    def reconcile_interrupted(self, *, actor: str = "system") -> dict[str, int | bool]:
        """Reconcile durable claims without ever retrying a side effect.

        A connector may only report a result it can prove by its own
        idempotency lookup. ``None`` keeps the approval in a visible error state.
        """

        summary: dict[str, int | bool] = {
            "journal_ready": False,
            "inspected": 0,
            "resolved": 0,
            "requires_attention": 0,
            "skipped": 0,
        }
        journal = self.action_journal
        if not _action_journal_ready(journal):
            return summary
        summary["journal_ready"] = True
        rows = self.store._rows("SELECT * FROM approvals WHERE status='error'")
        for approval in rows:
            history = self.store.approval_history(str(approval["id"]))
            recoverable = any(
                item["action"] == "approval.recover"
                or (
                    item["action"] in {
                        "approval.java_claim",
                        "approval.java_complete",
                    }
                    and item["status"] in {"in_progress", "unconfirmed"}
                )
                for item in history
            )
            if not recoverable:
                summary["skipped"] = int(summary["skipped"]) + 1
                continue
            try:
                payload = json.loads(approval["payload"] or "{}")
                if not isinstance(payload, dict):
                    raise ValueError("invalid payload")
                system = _identifier(payload.get("integration", ""), "Система интеграции")
                operation = _identifier(
                    payload.get("operation", ""),
                    "Операция интеграции",
                    allow_dot=True,
                )
                parameters = payload.get("parameters", {})
                if not isinstance(parameters, dict):
                    raise ValueError("invalid parameters")
                _reject_secrets(parameters)
                adapter = self._adapters.get(system)
                if adapter is None:
                    summary["requires_attention"] = int(summary["requires_attention"]) + 1
                    continue
                fingerprint = _action_request_fingerprint(approval, payload)
                inspection = journal.inspect_action(
                    idempotency_key=str(approval["idempotency_key"]),
                    request_fingerprint=fingerprint,
                )
            except (
                JavaCoreUnavailable,
                JavaCoreProtocolError,
                AttributeError,
                KeyError,
                TypeError,
                ValueError,
                json.JSONDecodeError,
            ):
                summary["requires_attention"] = int(summary["requires_attention"]) + 1
                continue
            summary["inspected"] = int(summary["inspected"]) + 1
            self._audit_java_state(
                approval,
                action="approval.java_inspect",
                status=inspection.disposition.casefold(),
                result_code=f"java_inspect_{inspection.disposition.casefold()}",
                actor=actor,
            )
            if inspection.disposition == "COMPLETED" and inspection.result is not None:
                self._apply_java_result(
                    approval,
                    inspection.result,
                    actor=actor,
                    origin="java_action_recovery",
                    recovery=True,
                )
                self._sync_reconciled_task(approval.get("task_id"))
                summary["resolved"] = int(summary["resolved"]) + 1
                continue
            if inspection.disposition != "IN_PROGRESS" or not inspection.claim_token:
                summary["requires_attention"] = int(summary["requires_attention"]) + 1
                continue
            try:
                observed = adapter.reconcile(
                    operation,
                    parameters,
                    idempotency_key=str(approval["idempotency_key"]),
                )
            except Exception:
                observed = None
            if observed is None:
                summary["requires_attention"] = int(summary["requires_attention"]) + 1
                continue
            observed = _normalize_execution_result(observed, adapter=adapter)
            result_code = (
                "PRODUCTION.SUCCESS"
                if observed.ok and observed.production
                else "SIMULATED.SUCCESS"
                if observed.ok
                else f"CONNECTOR.{_safe_result_code(observed.status)}"
            )
            try:
                completion = journal.complete_action(
                    idempotency_key=str(approval["idempotency_key"]),
                    request_fingerprint=fingerprint,
                    claim_token=inspection.claim_token,
                    outcome="SUCCESS" if observed.ok else "FAILURE",
                    result_code=result_code,
                    external_reference=_safe_external_reference(observed.external_id),
                )
            except (
                AttributeError,
                JavaCoreUnavailable,
                JavaCoreProtocolError,
                ValueError,
            ):
                completion = None
            if completion is None or completion.disposition not in {"RECORDED", "REPLAY"}:
                summary["requires_attention"] = int(summary["requires_attention"]) + 1
                continue
            java_result = completion.result or JavaActionExecution(
                outcome="SUCCESS" if observed.ok else "FAILURE",
                result_code=result_code,
                external_reference=_safe_external_reference(observed.external_id),
                completed_at=utc_now(),
            )
            self._apply_java_result(
                approval,
                java_result,
                actor=actor,
                origin="java_action_recovery",
                recovery=True,
            )
            self._sync_reconciled_task(approval.get("task_id"))
            summary["resolved"] = int(summary["resolved"]) + 1
        return summary

    def _sync_reconciled_task(self, task_id: Any) -> None:
        if not task_id:
            return
        rows = self.store._rows(
            "SELECT status FROM approvals WHERE task_id=?",
            (str(task_id),),
        )
        statuses = {str(item["status"]) for item in rows}
        target = (
            "needs_user"
            if statuses & {"pending", "approved", "executing", "error"}
            else "done"
        )
        self.store.update_task(str(task_id), status=target)

    def _claim_approval_execution(
        self,
        approval_id: str,
        *,
        actor: str,
    ) -> tuple[dict[str, Any], bool]:
        """Atomically reserve an approved action before crossing the connector.

        The process-local idempotency lock makes duplicate callers wait for the
        persisted result.  The conditional SQLite update is the second line of
        defence for callers from another hub or process: only its winner may
        invoke the connector.
        """

        now = utc_now()
        with self.store.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE approvals
                SET status='executing', updated_at=?
                WHERE id=? AND status='approved'
                  AND NOT EXISTS (
                    SELECT 1 FROM approvals AS predecessor
                    WHERE predecessor.workflow_id=approvals.workflow_id
                      AND predecessor.step_index<approvals.step_index
                      AND predecessor.status!='succeeded'
                  )
                """,
                (now, approval_id),
            )
            row = connection.execute(
                "SELECT * FROM approvals WHERE id=?",
                (approval_id,),
            ).fetchone()
            if row is None:
                raise KeyError(approval_id)
            claimed = cursor.rowcount == 1
            current = dict(row)
            if not claimed and current["status"] == "approved":
                predecessor = connection.execute(
                    """
                    SELECT step_index, status FROM approvals
                    WHERE workflow_id=? AND step_index<? AND status!='succeeded'
                    ORDER BY step_index LIMIT 1
                    """,
                    (current["workflow_id"], current["step_index"]),
                ).fetchone()
                if predecessor is not None:
                    raise ValueError(
                        "Сначала должен успешно завершиться шаг "
                        f"{predecessor['step_index']} "
                        f"(сейчас: {predecessor['status']})"
                    )
        if claimed:
            self.store.audit(
                current["task_id"],
                "approval.execute",
                approval_id,
                "executing",
                self.store._approval_audit_detail(
                    action_type=current["action_type"],
                    risk=current["risk"],
                    confirmation_policy=current["confirmation_policy"],
                    workflow_id=current["workflow_id"],
                    step_index=int(current["step_index"]),
                    revision=int(current["revision"]),
                ),
                actor=actor,
                origin="integration_hub",
            )
        return current, claimed

    def _stored_success_result(
        self,
        approval: Mapping[str, Any],
    ) -> IntegrationResult:
        rows = self.store._rows(
            """
            SELECT detail FROM audit_log
            WHERE target=?
              AND action IN ('approval.execute', 'approval.reconcile')
              AND status='succeeded'
            ORDER BY rowid DESC LIMIT 1
            """,
            (str(approval["id"]),),
        )
        result_code = ""
        if rows:
            try:
                detail = json.loads(rows[0].get("detail") or "{}")
            except (TypeError, json.JSONDecodeError):
                detail = {}
            if isinstance(detail, dict):
                result_code = str(detail.get("result_code") or "")
        production = result_code == "production_success"
        status = "succeeded" if production or result_code != "simulated" else "simulated"
        return IntegrationResult(
            status=status,
            message=str(approval.get("result") or "Действие уже выполнено"),
            production=production,
        )

    def _adapter(self, system: str) -> IntegrationAdapter:
        try:
            return self._adapters[system]
        except KeyError as exc:
            raise IntegrationUnavailable(
                f"Интеграция {system} не подключена; действие не выполнено"
            ) from exc


class InMemoryIntegrationAdapter:
    """Deterministic pilot/test adapter.  It never represents production."""

    production = False
    max_classification = "restricted"

    def __init__(
        self,
        system: str,
        *,
        records: list[Mapping[str, Any]] | None = None,
        fail_operations: set[str] | None = None,
    ) -> None:
        self.system = _identifier(system, "Система интеграции")
        self.records = [dict(item) for item in (records or [])]
        self.fail_operations = set(fail_operations or ())
        self.executions: list[dict[str, Any]] = []
        self._results: dict[str, IntegrationResult] = {}

    def read(self, operation: str, payload: Mapping[str, Any]) -> IntegrationResult:
        query = str(payload.get("query", "")).casefold().strip()
        records = self.records
        if query:
            records = [
                item
                for item in records
                if query in json.dumps(item, ensure_ascii=False).casefold()
            ]
        return IntegrationResult(
            status="simulated",
            message=f"Тестовый источник: найдено {len(records)}",
            production=False,
            data={"items": records},
        )

    def draft(self, operation: str, payload: Mapping[str, Any]) -> IntegrationPreview:
        return self.preview(operation, payload)

    def preview(self, operation: str, payload: Mapping[str, Any]) -> IntegrationPreview:
        fields = {
            key: _preview_value(value)
            for key, value in payload.items()
            if value not in (None, "", [], {})
        }
        return IntegrationPreview(
            title=f"{self.system}: {operation}",
            summary="Тестовый предпросмотр; внешняя система не будет изменена",
            fields=fields,
            warnings=("Подключён только локальный тестовый исполнитель",),
        )

    def execute(
        self,
        operation: str,
        payload: Mapping[str, Any],
        *,
        idempotency_key: str,
    ) -> IntegrationResult:
        if idempotency_key in self._results:
            return self._results[idempotency_key]
        if operation in self.fail_operations:
            raise RuntimeError("simulated connector failure")
        self.executions.append(
            {
                "operation": operation,
                "payload": dict(payload),
                "idempotency_key": idempotency_key,
            }
        )
        result = IntegrationResult(
            status="simulated",
            message="Действие выполнено только в локальном тестовом контуре",
            external_id=f"simulated-{len(self.executions)}",
            production=False,
        )
        self._results[idempotency_key] = result
        return result

    def reconcile(
        self,
        operation: str,
        payload: Mapping[str, Any],
        *,
        idempotency_key: str,
    ) -> IntegrationResult | None:
        del operation, payload
        return self._results.get(idempotency_key)


def _identifier(value: Any, label: str, *, allow_dot: bool = False) -> str:
    normalized = str(value).casefold().strip()
    pattern = r"[a-z0-9_-]+(?:\.[a-z0-9_-]+)*" if allow_dot else r"[a-z0-9_-]+"
    if not normalized or re.fullmatch(pattern, normalized) is None:
        raise ValueError(f"{label} задана некорректно")
    return normalized


def _execution_lock(store: AssistantStore, idempotency_key: str) -> Any:
    scope = (str(store.path.resolve()), idempotency_key)
    with _EXECUTION_LOCKS_GUARD:
        lock = _EXECUTION_LOCKS.get(scope)
        if lock is None:
            lock = threading.RLock()
            _EXECUTION_LOCKS[scope] = lock
        return lock


def _action_journal_ready(journal: ActionJournalRuntime | None) -> bool:
    """Start a compatible journal without letting an old bridge break the UI."""

    if journal is None:
        return False
    if bool(getattr(journal, "ready", False)):
        return True
    try:
        return bool(journal.start())
    except (
        AttributeError,
        JavaCoreUnavailable,
        JavaCoreProtocolError,
        ValueError,
    ):
        return False


def _action_request_fingerprint(
    approval: Mapping[str, Any],
    payload: Mapping[str, Any],
) -> str:
    identity = {
        "action_type": str(approval["action_type"]),
        "classification": normalize_classification(payload.get("classification")),
        "integration": str(payload.get("integration") or ""),
        "intent": str(payload.get("intent") or ""),
        "operation": str(payload.get("operation") or ""),
        "parameters": payload.get("parameters", {}),
        "revision": int(approval["revision"]),
    }
    canonical = json.dumps(
        identity,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _safe_result_code(value: str) -> str:
    normalized = re.sub(r"[^A-Z0-9._:-]+", ".", value.upper()).strip(".")
    return (normalized or "ERROR")[:128]


def _safe_external_reference(value: str) -> str | None:
    candidate = value.strip()
    if candidate and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}", candidate):
        return candidate
    return None


def _normalize_execution_result(
    result: IntegrationResult,
    *,
    adapter: IntegrationAdapter,
) -> IntegrationResult:
    production = bool(
        adapter.production and result.production and result.status == "succeeded"
    )
    status = result.status
    if result.ok and not production:
        status = "simulated"
    return IntegrationResult(
        status=status,
        message=result.message,
        external_id=result.external_id,
        production=production,
        data=result.data,
    )


def _reject_secrets(value: Any, *, path: str = "payload") -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            # Split camelCase as well as snake/kebab-case. Corporate APIs use
            # all three styles for credential fields (for example
            # accessToken, client_secret and authorization-header).
            canonical = re.sub(
                r"(?<=[a-z0-9])(?=[A-Z])",
                "_",
                str(key),
            ).casefold()
            parts = {
                part
                for part in re.split(r"[^a-z0-9]+", canonical)
                if part
            }
            if parts.intersection(
                {"token", "secret", "password", "authorization", "cookie"}
            ) or re.sub(r"[^a-z0-9]", "", canonical) in {
                "apikey",
                "authkey",
                "bearertoken",
            }:
                raise ValueError(
                    f"Секреты нельзя передавать в параметры интеграции ({path}.{key})"
                )
            _reject_secrets(nested, path=f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, nested in enumerate(value):
            _reject_secrets(nested, path=f"{path}[{index}]")


def _assert_classification_allowed(
    classification: str,
    adapter: IntegrationAdapter,
) -> None:
    actual = normalize_classification(classification)
    allowed = normalize_classification(
        getattr(adapter, "max_classification", "internal")
    )
    if CLASSIFICATION_RANK[actual] > CLASSIFICATION_RANK[allowed]:
        raise IntegrationUnavailable(
            "Классификация данных выше разрешённого уровня интеграции: "
            f"{actual} > {allowed}"
        )


def _safe_audit_detail(
    request: IntegrationRequest,
    *,
    production: bool,
) -> str:
    return json.dumps(
        {
            "system": request.system,
            "operation": request.operation,
            "intent": request.intent.value,
            "classification": request.classification,
            "parameter_keys": sorted(str(key) for key in request.payload),
            "production_connector": production,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _preview_value(value: Any) -> str:
    if isinstance(value, str):
        compact = re.sub(r"\s+", " ", value).strip()
    else:
        compact = json.dumps(value, ensure_ascii=False, sort_keys=True)
    return compact if len(compact) <= 160 else compact[:157] + "…"


def _disconnected_preview(request: IntegrationRequest) -> IntegrationPreview:
    return IntegrationPreview(
        title=request.title or f"{request.system}: {request.operation}",
        summary="Черновик подготовлен локально; исполнитель системы не подключён",
        fields={
            key: _preview_value(value)
            for key, value in request.payload.items()
            if value not in (None, "", [], {})
        },
        warnings=(
            "Подтверждение не приведёт к внешнему изменению до подключения API",
        ),
    )
