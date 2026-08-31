from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
import json
import re
import threading
import weakref
from typing import Any, Mapping, Protocol

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


class IntegrationUnavailable(RuntimeError):
    pass


class SafeIntegrationHub:
    """Routes corporate actions through one auditable confirmation boundary.

    Credentials are intentionally outside this API.  Adapters receive only a
    sanitized operation payload; access tokens must live in the platform secret
    store or in the adapter's process environment.
    """

    def __init__(self, store: AssistantStore) -> None:
        self.store = store
        self._adapters: dict[str, IntegrationAdapter] = {}

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
        try:
            result = adapter.execute(
                operation,
                parameters,
                idempotency_key=str(executing["idempotency_key"]),
            )
        except Exception as exc:
            message = f"{system}: исполнитель вернул ошибку {type(exc).__name__}"
            self.store.complete_approval_execution(
                approval_id,
                success=False,
                result_code="connector_error",
                result=message,
                actor=actor,
                origin="integration_hub",
            )
            return IntegrationResult(
                status="error",
                message=message,
                production=bool(adapter.production),
            )
        result = _normalize_execution_result(result, adapter=adapter)
        if not result.ok:
            self.store.complete_approval_execution(
                approval_id,
                success=False,
                result_code=f"connector_{result.status}",
                result=result.message,
                actor=actor,
                origin="integration_hub",
            )
            return result
        self.store.complete_approval_execution(
            approval_id,
            success=True,
            result_code=("production_success" if result.production else "simulated"),
            result=result.message,
            actor=actor,
            origin="integration_hub",
        )
        return result

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
            WHERE target=? AND action='approval.execute' AND status='succeeded'
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
