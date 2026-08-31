"""Safe local JSONL client for the Java 21 policy companion.

Only bounded policy metadata crosses this boundary. Prompts, transcripts,
documents, credentials and provider responses remain in the Python/native
runtime.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
from queue import Empty, Full, Queue
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
