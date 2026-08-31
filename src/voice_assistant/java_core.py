"""Safe local JSONL client for the Java 21 policy companion.

Only bounded policy metadata crosses this boundary. Prompts, transcripts,
documents, credentials and provider responses remain in the Python/native
runtime.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from queue import Empty, Full, Queue
import re
import subprocess
import threading
from typing import Any, Protocol, Sequence


PROTOCOL_VERSION = "1.0"
MAX_FRAME_CHARACTERS = 65_536
JAVA_CORE_MAIN_CLASS = "com.rndworkbench.core.ipc.CoreIpcApplication"
_CLASSIFICATION_MAP = {
    "public": "PUBLIC",
    "personal": "PERSONAL",
    "internal": "CORPORATE_INTERNAL",
    "confidential": "CONFIDENTIAL",
    "restricted": "RESTRICTED",
}
_PREFERENCES = frozenset({"AUTO", "LOCAL", "CORPORATE", "EXTERNAL"})
_ROUTES = frozenset({"LOCAL", "CORPORATE", "EXTERNAL"})
_STATUSES = frozenset({"SELECTED", "BLOCKED", "UNAVAILABLE"})
_CLAIM_DISPOSITIONS = frozenset({"CLAIMED", "REPLAY", "IN_PROGRESS", "CONFLICT"})
_INSPECTION_DISPOSITIONS = frozenset(
    {"NOT_FOUND", "IN_PROGRESS", "COMPLETED", "CONFLICT"}
)
_COMPLETION_DISPOSITIONS = frozenset(
    {"RECORDED", "REPLAY", "NOT_CLAIMED", "CONFLICT"}
)
_SAFE_ACTION_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,199}\Z")
_FINGERPRINT = re.compile(r"[a-f0-9]{64}\Z")
_CLAIM_TOKEN = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z",
    re.IGNORECASE,
)
_RESULT_CODE = re.compile(r"[A-Z0-9][A-Z0-9._:-]{0,127}\Z")
_EXTERNAL_REFERENCE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}\Z")


class JavaCoreUnavailable(RuntimeError):
    """The companion is absent, stopped or did not answer within the bound."""


class JavaCoreProtocolError(RuntimeError):
    """The companion returned a response outside the strict IPC contract."""


@dataclass(frozen=True, slots=True)
class JavaRouteDecision:
    status: str
    route: str | None
    reason: str
    local_fallback_before_first_output: bool


@dataclass(frozen=True, slots=True)
class JavaActionExecution:
    outcome: str
    result_code: str
    external_reference: str | None
    completed_at: str


@dataclass(frozen=True, slots=True)
class JavaActionClaim:
    disposition: str
    claim_token: str | None
    result: JavaActionExecution | None


@dataclass(frozen=True, slots=True)
class JavaActionInspection:
    disposition: str
    claim_token: str | None
    result: JavaActionExecution | None


@dataclass(frozen=True, slots=True)
class JavaActionCompletion:
    disposition: str
    result: JavaActionExecution | None


class ActionJournalRuntime(Protocol):
    @property
    def configured(self) -> bool: ...

    @property
    def ready(self) -> bool: ...

    def start(self) -> bool: ...

    def claim_action(
        self,
        *,
        idempotency_key: str,
        request_fingerprint: str,
    ) -> JavaActionClaim: ...

    def inspect_action(
        self,
        *,
        idempotency_key: str,
        request_fingerprint: str,
    ) -> JavaActionInspection: ...

    def complete_action(
        self,
        *,
        idempotency_key: str,
        request_fingerprint: str,
        claim_token: str,
        outcome: str,
        result_code: str,
        external_reference: str | None = None,
        completed_at: str | None = None,
    ) -> JavaActionCompletion: ...


class CorePolicyRuntime(Protocol):
    @property
    def configured(self) -> bool: ...

    @property
    def ready(self) -> bool: ...

    def diagnostics(self) -> dict[str, Any]: ...

    def start(self) -> bool: ...

    def decide_route(
        self,
        *,
        classification: str,
        preference: str,
        local_available: bool,
        corporate_available: bool,
        external_available: bool = False,
        corporate_scope_authorized: bool = False,
        explicit_external_consent: bool = False,
    ) -> JavaRouteDecision: ...

    def close(self) -> None: ...


def _validate_action_identity(idempotency_key: str, request_fingerprint: str) -> None:
    if not _SAFE_ACTION_KEY.fullmatch(idempotency_key):
        raise ValueError("Unsupported action idempotency key")
    if not _FINGERPRINT.fullmatch(request_fingerprint):
        raise ValueError("Unsupported action request fingerprint")


def _parse_action_execution(value: Any) -> JavaActionExecution:
    if not isinstance(value, dict) or set(value) not in (
        {"outcome", "resultCode", "completedAt"},
        {"outcome", "resultCode", "externalReference", "completedAt"},
    ):
        raise JavaCoreProtocolError("Java action result is invalid")
    outcome = value.get("outcome")
    result_code = value.get("resultCode")
    external_reference = value.get("externalReference")
    completed_at = value.get("completedAt")
    if outcome not in {"SUCCESS", "FAILURE"}:
        raise JavaCoreProtocolError("Java action outcome is invalid")
    if not isinstance(result_code, str) or not _RESULT_CODE.fullmatch(result_code):
        raise JavaCoreProtocolError("Java action result code is invalid")
    if external_reference is not None and (
        not isinstance(external_reference, str)
        or not _EXTERNAL_REFERENCE.fullmatch(external_reference)
    ):
        raise JavaCoreProtocolError("Java action external reference is invalid")
    if not isinstance(completed_at, str):
        raise JavaCoreProtocolError("Java action completion time is invalid")
    try:
        parsed_time = datetime.fromisoformat(completed_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise JavaCoreProtocolError("Java action completion time is invalid") from exc
    if parsed_time.tzinfo is None:
        raise JavaCoreProtocolError("Java action completion time is invalid")
    return JavaActionExecution(
        outcome=outcome,
        result_code=result_code,
        external_reference=external_reference,
        completed_at=completed_at,
    )


def java_classification(value: str) -> str:
    """Map the persisted product classification to the Java IPC enum."""

    try:
        return _CLASSIFICATION_MAP[value.strip().casefold()]
    except (AttributeError, KeyError) as exc:
        raise ValueError("Unsupported data classification") from exc


class JavaCorePolicyClient:
    """One-process, one-request-at-a-time Java policy adapter.

    A dedicated reader thread gives the synchronous desktop bridge a bounded
    response timeout on Windows, where pipe handles cannot be used with the
    normal ``selectors`` API.
    """

    def __init__(
        self,
        command_prefix: Sequence[str],
        journal_path: Path,
        *,
        timeout_seconds: float = 2.0,
    ) -> None:
        self._command_prefix = tuple(str(part) for part in command_prefix if str(part))
        self._journal_path = Path(journal_path)
        self._timeout_seconds = max(0.1, min(float(timeout_seconds), 10.0))
        self._process: subprocess.Popen[str] | None = None
        self._responses: Queue[str | None] = Queue(maxsize=8)
        self._reader: threading.Thread | None = None
        self._request_lock = threading.Lock()
        self._counter = 0
        self._ready = False

    @classmethod
    def from_environment(cls, data_path: Path) -> JavaCorePolicyClient:
        java_value = os.environ.get("RND_WORKBENCH_JAVA_CORE_JAVA", "").strip()
        library_value = os.environ.get("RND_WORKBENCH_JAVA_CORE_LIB_DIR", "").strip()
        external_models_enabled = os.environ.get(
            "RND_WORKBENCH_JAVA_CORE_EXTERNAL_MODELS_ENABLED", ""
        ).strip().casefold() in {"1", "true", "yes"}
        command: tuple[str, ...] = ()
        if java_value and library_value:
            java_path = Path(java_value)
            library_path = Path(library_value)
            if java_path.is_file() and library_path.is_dir():
                command = (
                    str(java_path),
                    "-cp",
                    str(library_path / "*"),
                    JAVA_CORE_MAIN_CLASS,
                    *(
                        ("--external-models-enabled",)
                        if external_models_enabled
                        else ()
                    ),
                )
        journal_path = data_path.with_name(f"{data_path.stem}-java-actions.sqlite3")
        return cls(command, journal_path)

    @property
    def configured(self) -> bool:
        return bool(self._command_prefix)

    @property
    def ready(self) -> bool:
        process = self._process
        return bool(self._ready and process is not None and process.poll() is None)

    def diagnostics(self) -> dict[str, Any]:
        return {
            "configured": self.configured,
            "ready": self.ready,
            "protocol_version": PROTOCOL_VERSION if self.ready else None,
            "policy": "java21" if self.ready else "python_fallback",
        }

    def start(self) -> bool:
        if self.ready:
            return True
        if not self.configured:
            return False
        self.close()
        self._journal_path.parent.mkdir(parents=True, exist_ok=True)
        creation_flags = 0
        if os.name == "nt":
            creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            process = subprocess.Popen(
                [
                    *self._command_prefix,
                    "--journal",
                    str(self._journal_path),
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="strict",
                bufsize=1,
                shell=False,
                creationflags=creation_flags,
            )
        except (OSError, ValueError):
            return False
        if process.stdin is None or process.stdout is None:
            process.kill()
            return False
        self._process = process
        self._responses = Queue(maxsize=8)
        self._reader = threading.Thread(
            target=self._read_responses,
            args=(process,),
            name="rnd-java-core-reader",
            daemon=True,
        )
        self._reader.start()
        try:
            response = self._request(
                "health.check",
                {},
                expected_type="health.status",
                allow_unready=True,
            )
            payload = response.get("payload")
            if not isinstance(payload, dict) or payload != {
                "protocolVersion": PROTOCOL_VERSION,
                "status": "ready",
            }:
                raise JavaCoreProtocolError("Java core health response is invalid")
        except (JavaCoreUnavailable, JavaCoreProtocolError):
            self.close()
            return False
        self._ready = True
        return True

    def decide_route(
        self,
        *,
        classification: str,
        preference: str,
        local_available: bool,
        corporate_available: bool,
        external_available: bool = False,
        corporate_scope_authorized: bool = False,
        explicit_external_consent: bool = False,
    ) -> JavaRouteDecision:
        normalized_preference = preference.strip().upper()
        if normalized_preference not in _PREFERENCES:
            raise ValueError("Unsupported route preference")
        response = self._request(
            "route.decide",
            {
                "classification": java_classification(classification),
                "preference": normalized_preference,
                "availableRoutes": {
                    "local": bool(local_available),
                    "corporate": bool(corporate_available),
                    "external": bool(external_available),
                },
                "corporateScopeAuthorized": bool(corporate_scope_authorized),
                "explicitExternalConsent": bool(explicit_external_consent),
            },
            expected_type="route.decision",
        )
        payload = response.get("payload")
        if not isinstance(payload, dict) or set(payload) not in (
            {"status", "route", "reason", "localFallbackBeforeFirstOutput"},
            {"status", "reason", "localFallbackBeforeFirstOutput"},
        ):
            raise JavaCoreProtocolError("Java route payload is invalid")
        status = payload.get("status")
        route = payload.get("route")
        reason = payload.get("reason")
        fallback = payload.get("localFallbackBeforeFirstOutput")
        if (
            status not in _STATUSES
            or not isinstance(reason, str)
            or not isinstance(fallback, bool)
            or (status == "SELECTED" and route not in _ROUTES)
            or (status != "SELECTED" and route is not None)
        ):
            raise JavaCoreProtocolError("Java route decision is invalid")
        return JavaRouteDecision(status, route, reason, fallback)

    def claim_action(
        self,
        *,
        idempotency_key: str,
        request_fingerprint: str,
    ) -> JavaActionClaim:
        _validate_action_identity(idempotency_key, request_fingerprint)
        response = self._request(
            "action.claim",
            {
                "idempotencyKey": idempotency_key,
                "requestFingerprint": request_fingerprint,
            },
            expected_type="action.claim.result",
        )
        payload = response.get("payload")
        if not isinstance(payload, dict):
            raise JavaCoreProtocolError("Java action claim is invalid")
        disposition = payload.get("disposition")
        if disposition not in _CLAIM_DISPOSITIONS:
            raise JavaCoreProtocolError("Java action claim disposition is invalid")
        expected_keys = {
            "CLAIMED": {"disposition", "claimToken"},
            "REPLAY": {"disposition", "result"},
            "IN_PROGRESS": {"disposition"},
            "CONFLICT": {"disposition"},
        }[disposition]
        if set(payload) != expected_keys:
            raise JavaCoreProtocolError("Java action claim payload is invalid")
        claim_token = payload.get("claimToken")
        if disposition == "CLAIMED" and (
            not isinstance(claim_token, str) or not _CLAIM_TOKEN.fullmatch(claim_token)
        ):
            raise JavaCoreProtocolError("Java action claim token is invalid")
        result = (
            _parse_action_execution(payload.get("result"))
            if disposition == "REPLAY"
            else None
        )
        return JavaActionClaim(disposition, claim_token, result)

    def inspect_action(
        self,
        *,
        idempotency_key: str,
        request_fingerprint: str,
    ) -> JavaActionInspection:
        _validate_action_identity(idempotency_key, request_fingerprint)
        response = self._request(
            "action.inspect",
            {
                "idempotencyKey": idempotency_key,
                "requestFingerprint": request_fingerprint,
            },
            expected_type="action.inspect.result",
        )
        payload = response.get("payload")
        if not isinstance(payload, dict):
            raise JavaCoreProtocolError("Java action inspection is invalid")
        disposition = payload.get("disposition")
        if disposition not in _INSPECTION_DISPOSITIONS:
            raise JavaCoreProtocolError("Java action inspection disposition is invalid")
        expected_keys = {
            "NOT_FOUND": {"disposition"},
            "CONFLICT": {"disposition"},
            "IN_PROGRESS": {"disposition", "claimToken"},
            "COMPLETED": {"disposition", "result"},
        }[disposition]
        if set(payload) != expected_keys:
            raise JavaCoreProtocolError("Java action inspection payload is invalid")
        claim_token = payload.get("claimToken")
        if disposition == "IN_PROGRESS" and (
            not isinstance(claim_token, str) or not _CLAIM_TOKEN.fullmatch(claim_token)
        ):
            raise JavaCoreProtocolError("Java action inspection token is invalid")
        result = (
            _parse_action_execution(payload.get("result"))
            if disposition == "COMPLETED"
            else None
        )
        return JavaActionInspection(disposition, claim_token, result)

    def complete_action(
        self,
        *,
        idempotency_key: str,
        request_fingerprint: str,
        claim_token: str,
        outcome: str,
        result_code: str,
        external_reference: str | None = None,
        completed_at: str | None = None,
    ) -> JavaActionCompletion:
        _validate_action_identity(idempotency_key, request_fingerprint)
        if not _CLAIM_TOKEN.fullmatch(claim_token):
            raise ValueError("Unsupported action claim token")
        normalized_outcome = outcome.strip().upper()
        if normalized_outcome not in {"SUCCESS", "FAILURE"}:
            raise ValueError("Unsupported action outcome")
        if not _RESULT_CODE.fullmatch(result_code):
            raise ValueError("Unsupported action result code")
        if external_reference is not None and not _EXTERNAL_REFERENCE.fullmatch(
            external_reference
        ):
            raise ValueError("Unsupported action external reference")
        completion_time = completed_at or datetime.now(timezone.utc).isoformat().replace(
            "+00:00", "Z"
        )
        try:
            parsed_time = datetime.fromisoformat(completion_time.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("Unsupported action completion time") from exc
        if parsed_time.tzinfo is None:
            raise ValueError("Unsupported action completion time")
        payload: dict[str, Any] = {
            "idempotencyKey": idempotency_key,
            "requestFingerprint": request_fingerprint,
            "claimToken": claim_token,
            "outcome": normalized_outcome,
            "resultCode": result_code,
            "completedAt": completion_time,
        }
        if external_reference is not None:
            payload["externalReference"] = external_reference
        response = self._request(
            "action.complete",
            payload,
            expected_type="action.complete.result",
        )
        body = response.get("payload")
        if not isinstance(body, dict):
            raise JavaCoreProtocolError("Java action completion is invalid")
        disposition = body.get("disposition")
        if disposition not in _COMPLETION_DISPOSITIONS:
            raise JavaCoreProtocolError("Java action completion disposition is invalid")
        expected_keys = (
            {"disposition", "result"}
            if disposition in {"RECORDED", "REPLAY"}
            else {"disposition"}
        )
        if set(body) != expected_keys:
            raise JavaCoreProtocolError("Java action completion payload is invalid")
        result = (
            _parse_action_execution(body.get("result"))
            if disposition in {"RECORDED", "REPLAY"}
            else None
        )
        return JavaActionCompletion(disposition, result)

    def close(self) -> None:
        self._ready = False
        process = self._process
        self._process = None
        if process is None:
            return
        if process.stdin is not None:
            try:
                process.stdin.close()
            except OSError:
                pass
        try:
            process.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1.0)
        if process.stdout is not None:
            try:
                process.stdout.close()
            except OSError:
                pass

    def _read_responses(self, process: subprocess.Popen[str]) -> None:
        stdout = process.stdout
        if stdout is None:
            self._put_response(None)
            return
        try:
            for line in stdout:
                self._put_response(line.rstrip("\r\n"))
        except (OSError, UnicodeError):
            pass
        finally:
            self._put_response(None)

    def _put_response(self, value: str | None) -> None:
        try:
            self._responses.put_nowait(value)
        except Full:
            # A peer that floods unsolicited frames is invalid. The bounded
            # queue remains full and the next request fails closed.
            pass

    def _request(
        self,
        request_type: str,
        payload: dict[str, Any],
        *,
        expected_type: str,
        allow_unready: bool = False,
    ) -> dict[str, Any]:
        with self._request_lock:
            process = self._process
            if (
                process is None
                or process.poll() is not None
                or process.stdin is None
                or (not allow_unready and not self.ready)
            ):
                self._ready = False
                raise JavaCoreUnavailable("Java core is unavailable")
            self._counter += 1
            correlation_id = f"desktop-{self._counter}"
            request = {
                "version": PROTOCOL_VERSION,
                "type": request_type,
                "correlationId": correlation_id,
                "payload": payload,
            }
            frame = json.dumps(request, ensure_ascii=True, separators=(",", ":"))
            if len(frame) > MAX_FRAME_CHARACTERS:
                raise JavaCoreProtocolError("Java core request is too large")
            try:
                process.stdin.write(frame + "\n")
                process.stdin.flush()
                line = self._responses.get(timeout=self._timeout_seconds)
            except (OSError, UnicodeError, Empty) as exc:
                self._ready = False
                raise JavaCoreUnavailable("Java core did not answer") from exc
            if line is None:
                self._ready = False
                raise JavaCoreUnavailable("Java core stopped")
            if len(line) > MAX_FRAME_CHARACTERS:
                raise JavaCoreProtocolError("Java core response is too large")
            try:
                response = json.loads(line)
            except (json.JSONDecodeError, UnicodeError) as exc:
                raise JavaCoreProtocolError("Java core response is not JSON") from exc
            if (
                not isinstance(response, dict)
                or response.get("version") != PROTOCOL_VERSION
                or response.get("correlationId") != correlation_id
                or response.get("type") != expected_type
                or response.get("ok") is not True
                or set(response) != {"version", "type", "correlationId", "ok", "payload"}
            ):
                raise JavaCoreProtocolError("Java core response envelope is invalid")
            return response

    def __enter__(self) -> JavaCorePolicyClient:
        self.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()
