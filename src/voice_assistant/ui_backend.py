from __future__ import annotations

import argparse
from collections import deque
from contextlib import redirect_stdout
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
import queue
import re
import sys
import threading
import time
from typing import Any

from .attention import AttentionEngine, render_attention
from .app import VoiceAssistant
from .audio import (
    BargeInDetector,
    Microphone,
    PlaybackReference,
    PushToTalkDurationExceededError,
    UtteranceDetector,
    trailing_silence_seconds,
)
from .backends import OpenAICompatibleChat, normalize_openai_base_url, openai_url_is_loopback
from .config import Config
from .orchestrator import LocalOrchestrator, RoutingPolicyError, TurnContext
from .store import AssistantStore
from .synapse import SynapseImportInProgressError, SynapseRepairRequiredError
from .text import concise_speech_text


@dataclass(frozen=True)
class _PendingVoiceInput:
    """A barge-in utterance plus the real timing origins captured before join."""

    audio: Any
    detected_at: float
    response_started_at: float


class EventEmitter:
    def __init__(self) -> None:
        self._lock = threading.Lock()

    def emit(self, event_type: str, **payload: Any) -> None:
        event = {"type": event_type, **payload}
        line = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            print(line, flush=True)


class UIBackend:
    _DICTATION_MAX_DURATION_S = 120.0
    _DICTATION_STT_TIMEOUT_S = 45.0
    _DICTATION_STT_POLL_S = 0.05

    def __init__(
        self,
        config: Config,
        emitter: EventEmitter,
        store: AssistantStore | None = None,
    ) -> None:
        self.config = config
        self.emitter = emitter
        self.assistant = VoiceAssistant(config)
        self._local_chat = self.assistant.local_chat
        data_path = Path(os.environ.get("LOCAL_ASSISTANT_DATA", "data/assistant.sqlite3"))
        self.store = store or AssistantStore(data_path)
        self.orchestrator = LocalOrchestrator(self.store)
        initial = self.store.snapshot()
        self.current_workspace_id = initial["current_workspace_id"]
        self.current_task_id = initial["current_task_id"]
        self.current_meeting_id = (
            initial.get("current_meeting_id")
            or (initial.get("meetings") or [{}])[0].get("id")
        )
        self._meeting_diff: list[dict[str, Any]] = []
        self._meeting_briefing: str | None = None
        self.stop_event = threading.Event()
        self.shutdown_event = threading.Event()
        self.session_thread: threading.Thread | None = None
        self.dictation_thread: threading.Thread | None = None
        self.dictation_release_event = threading.Event()
        self.dictation_cancel_event = threading.Event()
        self._dictation_event_lock = threading.Lock()
        self._dictation_stt_worker_lock = threading.Lock()
        self._dictation_stt_worker: threading.Thread | None = None
        self.text_thread: threading.Thread | None = None
        self.audio_import_thread: threading.Thread | None = None
        self.task_lock = threading.Lock()
        self._turn_lock = threading.Lock()
        self._current_turn_cancel: threading.Event | None = None
        self.automation_thread: threading.Thread | None = None
        self._external_api_key: str | None = None
        self._remote_chat: OpenAICompatibleChat | None = None
        self._restore_llm_runtime()

    def load(self) -> None:
        self.emitter.emit(
            "state",
            state="loading",
            detail="Обновляю локальный контекст встреч…",
        )
        self._backfill_meeting_sources()
        self._sync_meeting_attention()
        for label, loader in (
            ("Whisper", self.assistant.stt.load),
            # Always load the on-device model as a ready, explicit fallback.
            # If an external runtime was restored, it remains active but is
            # never contacted during application startup.
            ("LLM · локальный резерв", self._local_chat.load),
            ("TTS", self.assistant.tts.load),
        ):
            self.emitter.emit("state", state="loading", detail=f"Загрузка {label}…")
            started = time.perf_counter()
            with redirect_stdout(sys.stderr):
                loader()
            self.emitter.emit(
                "model_loaded",
                model=label,
                seconds=round(time.perf_counter() - started, 2),
            )
        runtime = self._llm_runtime()
        self.emitter.emit(
            "state",
            state="ready" if runtime["ready"] else "needs_configuration",
            detail=runtime["detail"],
        )
        self.emitter.emit("ready")
        self.emit_snapshot()
        self.automation_thread = threading.Thread(
            target=self._automation_loop,
            name="automation-scheduler",
            daemon=True,
        )
        self.automation_thread.start()

    def handle(self, command: dict[str, Any]) -> None:
        name = command.get("command")
        if name == "start":
            self.start_session()
        elif name == "stop":
            self.stop_session()
        elif name == "dictation_start":
            self.start_dictation(destination=str(command.get("destination") or "system"))
        elif name == "dictation_stop":
            self.stop_dictation()
        elif name == "text":
            text = str(command.get("text", "")).strip()
            if text:
                self.submit_text(
                    text,
                    speak=command.get("speak", True) is not False,
                    attachments=self._parse_attachments(command.get("attachments")),
                )
        elif name == "retry_speech":
            self.retry_speech()
        elif name == "configure_llm":
            self.configure_llm(command)
        elif name == "clear":
            task = self.store.create_task(self.current_workspace_id, "Новая задача")
            self.current_task_id = task["id"]
            self.emitter.emit("cleared")
            self.emit_snapshot()
        elif name == "snapshot":
            self.emit_snapshot()
        elif name == "select_workspace":
            workspace_id = str(command.get("id", ""))
            self.store.get_workspace(workspace_id)
            self._set_current_workspace(workspace_id)
            self.current_task_id = None
            self.current_meeting_id = None
            self._meeting_diff = []
            self._meeting_briefing = None
            self.emit_snapshot()
        elif name == "select_task":
            task_id = str(command.get("id", ""))
            task = self.store.get_task(task_id)
            self._set_current_workspace(task["workspace_id"])
            self.current_task_id = task_id
            self.emit_snapshot()
        elif name == "new_task":
            title = str(command.get("title", "Новая задача")).strip() or "Новая задача"
            task = self.store.create_task(self.current_workspace_id, title)
            self.current_task_id = task["id"]
            self.emit_snapshot()
        elif name == "delete_task":
            self._delete_task(str(command.get("task_id") or command.get("id") or ""))
        elif name == "update_task_plan":
            task_id = str(command.get("id", self.current_task_id or ""))
            plan = [
                str(step).strip()
                for step in command.get("plan", [])
                if str(step).strip()
            ]
            if not plan:
                raise ValueError("План не может быть пустым")
            self.store.update_task(task_id, plan=plan)
            self.store.add_task_event(task_id, "plan", "План задачи обновлён")
            self.emit_snapshot()
        elif name == "mutate_task_plan":
            if self.task_lock.locked():
                raise RuntimeError("Дождитесь завершения текущей задачи")
            text = str(command.get("text") or "").strip()
            if not text or not self._try_deterministic_request(text, speak=False):
                raise ValueError("Команда изменения плана не распознана")
        elif name == "generate_digest":
            if self.task_lock.locked():
                raise RuntimeError("Дождитесь завершения текущей задачи")
            period = self.orchestrator.digest_period(
                str(command.get("period") or command.get("kind") or "morning")
            )
            self._run_structured_digest(
                period,
                request_text=f"/digest {period}",
                speak=False,
            )
        elif name == "create_workspace":
            workspace = self.store.create_workspace(
                str(command.get("name", "Новое пространство")).strip() or "Новое пространство",
                str(command.get("description", "")),
            )
            self._set_current_workspace(workspace["id"])
            self.current_task_id = None
            self.current_meeting_id = None
            self._meeting_diff = []
            self._meeting_briefing = None
            self.emit_snapshot()
        elif name == "update_workspace":
            workspace_id = str(command.get("id", self.current_workspace_id))
            self.store.update_workspace(
                workspace_id,
                name=command.get("name"),
                description=command.get("description"),
                status=command.get("status"),
            )
            if command.get("status") == "archived":
                self._set_current_workspace(self.store.default_workspace_id())
                self.current_task_id = None
                self.current_meeting_id = None
                self._meeting_diff = []
                self._meeting_briefing = None
            self.emit_snapshot()
        elif name == "import_file":
            raw_task_id = command.get("task_id")
            task_id = str(raw_task_id) if raw_task_id else None
            source = self.orchestrator.import_file(
                Path(str(command.get("path", ""))),
                workspace_id=str(command.get("workspace_id", self.current_workspace_id)),
                kind=command.get("kind"),
                task_id=task_id,
            )
            self.emitter.emit("source_imported", source=source)
            if source["kind"] == "meeting":
                self.current_meeting_id = source.get("meeting_id")
                self._meeting_diff = []
                self._meeting_briefing = None
                self._register_meeting_event(source)
                self._sync_meeting_attention()
            self.emit_snapshot()
            event_automations = (
                self.store.event_automations(source["workspace_id"])
                if source["visibility"] == "workspace"
                else []
            )
            if event_automations:
                threading.Thread(
                    target=self._run_event_automations,
                    args=(event_automations, source),
                    name="source-event-automations",
                    daemon=True,
                ).start()
        elif name == "import_meeting_audio":
            self.import_meeting_audio(
                Path(str(command.get("path", ""))),
                workspace_id=str(
                    command.get("workspace_id") or self.current_workspace_id
                ),
            )
        elif name == "import_synapse_package":
            try:
                result = self.orchestrator.import_synapse_meeting_package(
                    Path(str(command.get("path", ""))),
                    workspace_id=str(
                        command.get("workspace_id") or self.current_workspace_id
                    ),
                )
            except SynapseRepairRequiredError as error:
                self.emitter.emit(
                    "synapse_package_repair_required",
                    code=error.code,
                    message=str(error),
                )
                return
            except SynapseImportInProgressError as error:
                self.emitter.emit(
                    "synapse_package_import_in_progress",
                    code=error.code,
                    message=str(error),
                )
                return
            primary = self.store.get_source(str(result["source_id"]))
            self._set_current_workspace(str(primary["workspace_id"]))
            self.current_meeting_id = str(result["meeting_id"])
            self._meeting_diff = []
            self._meeting_briefing = None
            primary["meeting_id"] = result["meeting_id"]
            if result["status"] == "imported":
                self._register_meeting_event(primary)
                self._sync_meeting_attention()
            self.emitter.emit("synapse_package_imported", result=result)
            self.emit_snapshot()
        elif name == "delete_source":
            self._delete_source(
                str(command.get("source_id") or command.get("id") or "")
            )
        elif name == "select_meeting":
            meeting_id = str(command.get("meeting_id") or command.get("id") or "")
            meeting = self.store.get_meeting(meeting_id)
            self._set_current_workspace(meeting["workspace_id"])
            self.current_meeting_id = meeting_id
            self._meeting_diff = []
            self._meeting_briefing = None
            self.emit_snapshot()
        elif name == "reanalyze_meeting":
            meeting_id = str(command.get("meeting_id") or command.get("id") or "")
            meeting = self.store.get_meeting(meeting_id)
            updated = self.store.analyze_meeting(
                meeting["source_id"],
                title=meeting["title"],
                occurred_at=meeting.get("occurred_at"),
            )
            self._set_current_workspace(updated["workspace_id"])
            self.current_meeting_id = updated["id"]
            self._meeting_diff = []
            self._meeting_briefing = None
            self._sync_meeting_attention()
            self.emit_snapshot()
        elif name == "compare_meetings":
            before_id = str(command.get("meeting_id") or command.get("id") or "")
            after_id = str(
                command.get("other_meeting_id") or command.get("other_id") or ""
            )
            comparison = self.store.compare_meetings(before_id, after_id)
            self._meeting_diff = self._meeting_diff_rows(comparison)
            self._register_meeting_changes(comparison)
            self.emit_snapshot()
        elif name == "meeting_item_status":
            item_id = str(command.get("item_id") or command.get("id") or "")
            item = self.store.update_meeting_item_status(
                item_id,
                str(command.get("status", "open")),
            )
            self.current_meeting_id = item["meeting_id"]
            self._sync_meeting_attention()
            self.emit_snapshot()
        elif name == "prepare_briefing":
            meeting_id = str(command.get("meeting_id") or command.get("id") or "")
            meeting = self.store.get_meeting(meeting_id, include_items=True)
            self._set_current_workspace(meeting["workspace_id"])
            self.current_meeting_id = meeting_id
            since = (datetime.now(UTC) - timedelta(days=180)).isoformat()
            briefing = self.store.briefing_data(
                meeting["workspace_id"],
                since=since,
                limit=12,
            )
            self._meeting_briefing = self._render_meeting_briefing(meeting, briefing)
            self.emit_snapshot()
        elif name == "explain_attention":
            self.submit_text(
                "Что требует моего внимания сейчас?",
                speak=command.get("speak", True) is not False,
            )
        elif name == "search":
            results = self.orchestrator.search(
                str(command.get("query", "")),
                workspace_id=self.current_workspace_id,
                global_scope=bool(command.get("global", False)),
                task_id=self.current_task_id,
            )
            self.emitter.emit("search_results", query=command.get("query", ""), results=results)
        elif name == "save_memory":
            self.store.remember(
                str(command.get("title", "Заметка")),
                str(command.get("content", "")),
                workspace_id=self.current_workspace_id,
                kind=str(command.get("kind", "note")),
            )
            self.emit_snapshot()
        elif name == "delete_memory":
            self.store.delete_memory(str(command.get("id", "")))
            self.emit_snapshot()
        elif name == "update_memory":
            self.store.update_memory(
                str(command.get("id", "")),
                str(command.get("title", "Заметка")),
                str(command.get("content", "")),
                kind=str(command.get("kind", "")) or None,
            )
            self.emit_snapshot()
        elif name == "save_skill":
            self.store.create_or_update_skill(
                str(command.get("name", "Новый skill")),
                str(command.get("command_name", "/skill")),
                str(command.get("description", "")),
                str(command.get("instruction", "")),
                scope=str(command.get("scope", "personal")),
                workspace_id=command.get("workspace_id"),
                skill_id=command.get("id"),
            )
            self.emit_snapshot()
        elif name == "create_automation":
            self.store.create_automation(
                self.current_workspace_id,
                str(command.get("name", "Автоматизация")),
                str(command.get("prompt", "")),
                str(command.get("schedule", "")),
            )
            self.emit_snapshot()
        elif name == "update_automation":
            self.store.update_automation(
                str(command.get("id", "")),
                name=str(command.get("name", "Автоматизация")),
                prompt=str(command.get("prompt", "")),
                schedule=str(command.get("schedule", "")),
            )
            self.emit_snapshot()
        elif name == "delete_automation":
            self.store.delete_automation(str(command.get("id", "")))
            self.emit_snapshot()
        elif name == "toggle_automation":
            self.store.set_automation_enabled(
                str(command.get("id", "")), bool(command.get("enabled", True))
            )
            self.emit_snapshot()
        elif name == "resolve_approval":
            self._resolve_approval(
                str(command.get("id", "")), str(command.get("status", "rejected"))
            )
        elif name == "update_approval":
            payload = command.get("payload", {})
            if isinstance(payload, str):
                payload = json.loads(payload)
            if not isinstance(payload, dict):
                raise ValueError("Параметры действия должны быть JSON-объектом")
            updated = self.store.update_approval_payload(
                str(command.get("id", "")),
                payload,
                actor="local-user",
                origin="approval_center",
            )
            if updated.get("task_id"):
                self.store.update_task(updated["task_id"], status="needs_user")
                self.store.add_task_event(
                    updated["task_id"],
                    "approval_replanned",
                    "Параметры внешнего действия изменены",
                    f"Шаг {updated['step_index']} · {updated['action_type']}",
                )
            self.emitter.emit(
                "approval_replanned",
                id=updated["id"],
                revision=updated["revision"],
                status=updated["status"],
            )
            self.emit_snapshot()
        elif name == "update_artifact":
            self.store.update_artifact(
                str(command.get("id", "")),
                str(command.get("content", "")),
            )
            self.emit_snapshot()
        elif name == "delete_artifact":
            self._delete_artifact(
                str(command.get("artifact_id") or command.get("id") or "")
            )
        elif name == "artifact_versions":
            artifact_id = str(command.get("artifact_id") or command.get("id") or "")
            self.emitter.emit(
                "artifact_versions",
                artifact=self.store.get_artifact(artifact_id),
                versions=self.store.artifact_versions(artifact_id),
                relations=self.store.artifact_relations(artifact_id),
            )
        elif name == "restore_artifact":
            artifact_id = str(command.get("artifact_id") or command.get("id") or "")
            artifact = self.store.restore_artifact(
                artifact_id,
                int(command.get("version", 0)),
            )
            self.emitter.emit(
                "artifact_restored",
                artifact=artifact,
                versions=self.store.artifact_versions(artifact_id),
                relations=self.store.artifact_relations(artifact_id),
            )
            self.emit_snapshot()
        elif name == "artifact_relations":
            artifact_id = str(command.get("artifact_id") or command.get("id") or "")
            raw_version = command.get("version")
            self.emitter.emit(
                "artifact_relations",
                artifact_id=artifact_id,
                relations=self.store.artifact_relations(
                    artifact_id,
                    version=int(raw_version) if raw_version is not None else None,
                ),
            )
        elif name == "quick_action":
            task_id = str(command.get("task_id") or self.current_task_id or "")
            action = str(command.get("action") or command.get("id") or "")
            result = self.orchestrator.run_quick_action(
                action,
                task_id=task_id,
                title=str(command["title"]) if command.get("title") else None,
            )
            self.emitter.emit("quick_action_completed", result=result)
            self.emit_snapshot()
        elif name == "inbox_status":
            self.store.set_inbox_status(
                str(command.get("id", "")), str(command.get("status", "read"))
            )
            self.emit_snapshot()
        elif name == "set_classification":
            entity_type = str(command.get("entity_type") or "").strip().casefold()
            entity_id = str(command.get("entity_id") or command.get("id") or "")
            classification = str(command.get("classification") or "")
            updated = self.store.set_classification(
                entity_type,
                entity_id,
                classification,
                reason="ui",
            )
            self.emitter.emit(
                "classification_updated",
                entity_type=entity_type,
                entity_id=entity_id,
                classification=updated["classification"],
            )
            self.emit_snapshot()
        elif name == "setting":
            key = str(command.get("key", ""))
            normalized_key = key.casefold().replace("-", "_")
            if normalized_key in {
                "model_mode",
                "llm_base_url",
                "llm_model",
                "external_provider_type",
                "auto_remote_policy",
                "default_classification",
            }:
                raise ValueError(
                    "Маршрут модели и политика данных меняются только специальными командами"
                )
            if any(
                marker in normalized_key
                for marker in ("api_key", "apikey", "password", "secret", "token")
            ):
                raise ValueError("Секреты нельзя сохранять в настройках приложения")
            if normalized_key == "external_context_scope":
                value = str(command.get("value", "task")).strip().casefold()
                if value not in {"task", "workspace"}:
                    raise ValueError("Контекст внешней модели должен быть task или workspace")
                settings = self.store.settings()
                if value == "workspace":
                    auto_remote_enabled = (
                        settings.get("model_mode") == "auto"
                        and settings.get("auto_remote_policy") == "eligible"
                    )
                    if (
                        settings.get("model_mode") != "external"
                        and not auto_remote_enabled
                    ) or not settings.get("llm_base_url"):
                        raise ValueError(
                            "Сначала настройте внешний endpoint, затем разрешите контекст пространства"
                        )
                    self.store.set_settings(
                        {
                            "external_context_scope": "workspace",
                            "external_context_scope_endpoint": settings["llm_base_url"],
                            "external_context_scope_workspace": self.current_workspace_id,
                        }
                    )
                else:
                    self.store.set_settings(
                        {
                            "external_context_scope": "task",
                            "external_context_scope_endpoint": "",
                            "external_context_scope_workspace": "",
                        }
                    )
                self.emit_snapshot()
                return
            self.store.set_setting(key, str(command.get("value", "")))
            self.emit_snapshot()
        elif name == "ping":
            self.emitter.emit("pong")
        elif name == "quit":
            self.shutdown_event.set()
            self.cancel_dictation()
            self.stop_session()
        else:
            self.emitter.emit("error", message=f"Неизвестная команда: {name}")

    def _delete_task(self, task_id: str) -> None:
        if not task_id:
            raise ValueError("Не указана задача для удаления")
        if not self.task_lock.acquire(blocking=False):
            self.emitter.emit(
                "error",
                message="Дождитесь завершения текущей задачи перед удалением",
            )
            return
        try:
            result = self.store.delete_task(task_id)
            if self.current_task_id == task_id:
                self.current_task_id = None
        finally:
            self.task_lock.release()
        self.emitter.emit(
            "entity_deleted",
            entity_type="task",
            entity_id=task_id,
            title=result["title"],
            recovery="trash" if result["trashed_files"] else "database_only",
        )
        self.emit_snapshot()

    def _delete_source(self, source_id: str) -> None:
        if not source_id:
            raise ValueError("Не указан файл для удаления")
        if not self.task_lock.acquire(blocking=False):
            self.emitter.emit(
                "error",
                message="Дождитесь завершения текущей задачи перед удалением",
            )
            return
        try:
            source = self.store.get_source(source_id)
            result = self.store.delete_source_package_aware(source_id)
            deleted_meeting_ids = {
                str(item) for item in result.get("deleted_meeting_ids", [])
            }
            if self.current_meeting_id in deleted_meeting_ids:
                self.current_meeting_id = None
                self._meeting_diff = []
                self._meeting_briefing = None
        finally:
            self.task_lock.release()
        if source["kind"] == "meeting":
            self._sync_meeting_attention()
        self.emitter.emit(
            "entity_deleted",
            entity_type="source",
            entity_id=source_id,
            title=result["title"],
            recovery="trash" if result["trashed_files"] else "database_only",
            deleted_sources=result.get("deleted_sources", 1),
            package_aware=bool(result.get("package_aware")),
        )
        self.emit_snapshot()

    def _delete_artifact(self, artifact_id: str) -> None:
        if not artifact_id:
            raise ValueError("Не указан материал для удаления")
        if not self.task_lock.acquire(blocking=False):
            self.emitter.emit(
                "error",
                message="Дождитесь завершения текущей задачи перед удалением",
            )
            return
        try:
            result = self.store.delete_artifact(artifact_id)
        finally:
            self.task_lock.release()
        self.emitter.emit(
            "entity_deleted",
            entity_type="artifact",
            entity_id=artifact_id,
            title=result["title"],
            recovery="trash" if result["trashed_files"] else "database_only",
        )
        self.emit_snapshot()

    def import_meeting_audio(self, path: Path, *, workspace_id: str) -> None:
        if self.audio_import_thread is not None and self.audio_import_thread.is_alive():
            self.emitter.emit(
                "audio_import_error",
                message="Дождитесь завершения текущей транскрибации",
                retryable=True,
            )
            return
        if self.task_lock.locked():
            self.emitter.emit(
                "audio_import_error",
                message="Дождитесь завершения текущей задачи",
                retryable=True,
            )
            return
        if not path.expanduser().is_file():
            self.emitter.emit(
                "audio_import_error",
                message=f"Аудиофайл не найден: {path}",
                retryable=True,
            )
            return
        thread = threading.Thread(
            target=self._import_meeting_audio_turn,
            args=(path, workspace_id),
            name="meeting-audio-import",
            daemon=True,
        )
        self.audio_import_thread = thread
        thread.start()

    def _import_meeting_audio_turn(self, path: Path, workspace_id: str) -> None:
        if not self.task_lock.acquire(blocking=False):
            self.emitter.emit(
                "audio_import_error",
                message="Дождитесь завершения текущей задачи",
                retryable=True,
            )
            return
        event_automations: list[dict[str, Any]] = []
        source: dict[str, Any] | None = None
        try:
            self.emitter.emit(
                "audio_import_started",
                filename=path.name,
                workspace_id=workspace_id,
            )
            self.emitter.emit(
                "transcription_started",
                filename=path.name,
                engine="Whisper · локально",
            )
            transcript = self.assistant.stt.transcribe_file(path)
            self.emitter.emit(
                "transcription_completed",
                filename=path.name,
                transcript_chars=len(transcript),
            )
            source = self.orchestrator.import_meeting_audio(
                path,
                transcript,
                workspace_id=workspace_id,
            )
            self._set_current_workspace(source["workspace_id"])
            self.current_meeting_id = source.get("meeting_id")
            self._meeting_diff = []
            self._meeting_briefing = None
            self._register_meeting_event(source)
            self._sync_meeting_attention()
            event_automations = self.store.event_automations(source["workspace_id"])
            self.emitter.emit("source_imported", source=source)
            self.emitter.emit(
                "audio_import_completed",
                source=source,
                meeting_id=source.get("meeting_id"),
                transcript_chars=source.get("transcript_chars", len(transcript)),
            )
            self.emit_snapshot()
        except BaseException as exc:
            self.emitter.emit(
                "audio_import_error",
                message=str(exc),
                retryable=True,
            )
        finally:
            self.task_lock.release()
        if source is not None and event_automations:
            self._run_event_automations(event_automations, source)

    def configure_llm(self, command: dict[str, Any]) -> None:
        """Atomically switch the active LLM runtime.

        The external API key is intentionally excluded from SQLite, audit
        events, snapshots, and emitted configuration events.  Omitting it, or
        leaving a password-style field blank, preserves the current in-memory
        key only when the endpoint and model have not changed.
        """

        has_active_thread = (
            self.text_thread is not None and self.text_thread.is_alive()
        ) or (self.session_thread is not None and self.session_thread.is_alive()) or (
            self.audio_import_thread is not None and self.audio_import_thread.is_alive()
        ) or (
            self.dictation_thread is not None and self.dictation_thread.is_alive()
        )
        acquired = False if has_active_thread else self.task_lock.acquire(blocking=False)
        if not acquired:
            self.store.audit(
                None,
                "llm.configure",
                str(command.get("mode", "unknown")) or "unknown",
                "error",
                "Активна другая задача",
            )
            self.emitter.emit(
                "llm_configuration_error",
                message="Дождитесь завершения текущей задачи перед сменой модели",
                retryable=True,
            )
            return
        try:
            self._configure_llm_unlocked(command)
        finally:
            self.task_lock.release()

    def _configure_llm_unlocked(self, command: dict[str, Any]) -> None:
        mode = str(command.get("mode", "")).strip().casefold()
        try:
            if mode == "local":
                self.store.set_settings({"model_mode": "local"})
                self.assistant.chat = self._local_chat
                self._remote_chat = None
                self._external_api_key = None
            elif mode in {"auto", "external"}:
                current_settings = self.store.settings()
                auto_remote_policy = str(
                    command.get("auto_remote_policy")
                    or current_settings.get("auto_remote_policy")
                    or "local_only"
                ).strip().casefold()
                if auto_remote_policy not in {"local_only", "eligible"}:
                    raise ValueError(
                        "Политика Auto должна быть local_only или eligible"
                    )
                base_url_value = str(
                    command.get("base_url")
                    or current_settings.get("llm_base_url")
                    or ""
                ).strip()
                model = str(
                    command.get("model")
                    or current_settings.get("llm_model")
                    or ""
                ).strip()
                provider_type = str(
                    command.get("provider_type")
                    or current_settings.get("external_provider_type")
                    or "external"
                ).strip().casefold()
                if provider_type not in {"external", "corporate"}:
                    raise ValueError(
                        "Тип провайдера должен быть corporate или external"
                    )
                if not base_url_value or not model:
                    if mode == "auto" and auto_remote_policy == "local_only":
                        self.store.set_settings(
                            {
                                "model_mode": "auto",
                                "auto_remote_policy": "local_only",
                            }
                        )
                        self.assistant.chat = self._local_chat
                        self._remote_chat = None
                        self._external_api_key = None
                        runtime = self._llm_runtime()
                        self._emit_llm_configured(runtime)
                        return
                    raise ValueError("Укажите название модели внешнего API")
                base_url = normalize_openai_base_url(base_url_value)
                current = self._remote_chat
                same_runtime = (
                    isinstance(current, OpenAICompatibleChat)
                    and current.base_url == base_url
                    and current.model_name == model
                )
                provided_api_key = str(command.get("api_key") or "").strip()
                api_key = provided_api_key or (
                    self._external_api_key if same_runtime else ""
                )
                if not api_key and not openai_url_is_loopback(base_url):
                    raise ValueError(
                        "Для удалённой внешней модели нужен API-ключ. "
                        "Он хранится только до закрытия приложения"
                    )
                external = OpenAICompatibleChat(
                    self.config.llm,
                    base_url=base_url,
                    model=model,
                    api_key=api_key,
                )
                # Persist only non-secret routing data, after all validation
                # has succeeded and before publishing the new active runtime.
                self.store.set_settings(
                    self._external_route_settings(
                        external,
                        provider_type,
                        mode=mode,
                        auto_remote_policy=auto_remote_policy,
                    )
                )
                self._external_api_key = api_key or None
                self._remote_chat = external
                self.assistant.chat = external if mode == "external" else self._local_chat
            else:
                raise ValueError("Режим модели должен быть local, auto или external")
        except (TypeError, ValueError) as exc:
            self.store.audit(
                None,
                "llm.configure",
                mode or "unknown",
                "error",
                str(exc),
            )
            self.emitter.emit(
                "llm_configuration_error",
                message=str(exc),
                retryable=True,
            )
            return

        self._emit_llm_configured(self._llm_runtime())

    def _emit_llm_configured(self, runtime: dict[str, Any]) -> None:
        self.store.audit(
            None,
            "llm.configure",
            runtime["mode"],
            "success",
            (
                f"{runtime['provider']} · {runtime['model']} · {runtime['base_url']}"
                if runtime["mode"] in {"auto", "external"}
                else f"{runtime['provider']} · {runtime['model']}"
            ),
        )
        self.emitter.emit(
            "llm_configured",
            mode=runtime["mode"],
            provider_type=runtime.get("provider_type", runtime["mode"]),
            base_url=runtime["base_url"],
            model=runtime["model"],
            active_model=(
                f"{runtime['model']} · "
                + (
                    "локальный API"
                    if runtime.get("provider_type") == "local"
                    else "корпоративный API"
                    if runtime.get("provider_type") == "corporate"
                    else "внешний API"
                )
                if runtime["mode"] == "external"
                else (
                    f"Auto · {runtime['model']} · local MLX"
                    if runtime["mode"] == "auto"
                    else f"{runtime['model']} · local MLX"
                )
            ),
            ready=runtime["ready"],
            status=runtime["status"],
            runtime=runtime,
        )
        self.emitter.emit(
            "state",
            state="ready" if runtime["ready"] else "needs_configuration",
            detail=runtime["detail"],
        )
        self.emit_snapshot()

    def _restore_llm_runtime(self) -> None:
        settings = self.store.settings()
        mode = settings.get("model_mode", "local")
        self._remote_chat = None
        if mode not in {"auto", "external"}:
            self.assistant.chat = self._local_chat
            return
        base_url = settings.get("llm_base_url", "")
        model = settings.get("llm_model", "")
        if not base_url or not model:
            self.assistant.chat = self._local_chat
            return
        # Remote credentials never survive a restart.  Keeping the external
        # runtime active-but-not-ready makes that visible and prevents an
        # accidental, silent request to the local model.
        self._remote_chat = OpenAICompatibleChat(
            self.config.llm,
            base_url=base_url,
            model=model,
            api_key=None,
        )
        self.assistant.chat = (
            self._remote_chat if mode == "external" else self._local_chat
        )

    def _external_route_settings(
        self,
        external: OpenAICompatibleChat,
        provider_type: str,
        *,
        mode: str,
        auto_remote_policy: str,
    ) -> dict[str, str]:
        settings = self.store.settings()
        values = {
            "llm_base_url": external.base_url,
            "llm_model": external.model_name,
            "model_mode": mode,
            "auto_remote_policy": auto_remote_policy,
            "external_provider_type": provider_type,
        }
        if (
            settings.get("llm_base_url") != external.base_url
            or settings.get("external_provider_type", "external") != provider_type
        ):
            values.update(
                {
                    "external_context_scope": "task",
                    "external_context_scope_endpoint": "",
                    "external_context_scope_workspace": "",
                }
            )
        return values

    def _set_current_workspace(self, workspace_id: str) -> None:
        if workspace_id == self.current_workspace_id:
            return
        self.current_workspace_id = workspace_id
        settings = self.store.settings()
        if (
            settings.get("model_mode") in {"auto", "external"}
            and settings.get("external_context_scope") == "workspace"
        ):
            self.store.set_settings(
                {
                    "external_context_scope": "task",
                    "external_context_scope_endpoint": "",
                    "external_context_scope_workspace": "",
                }
            )

    def _llm_runtime(self) -> dict[str, Any]:
        local_ready = bool(
            self._local_chat.model is not None
            and self._local_chat.tokenizer is not None
        )
        settings = self.store.settings()
        configured_mode = settings.get("model_mode", "local")
        active = self._remote_chat
        if configured_mode in {"auto", "external"} and isinstance(
            active, OpenAICompatibleChat
        ):
            configured_type = settings.get("external_provider_type", "external")
            is_loopback = openai_url_is_loopback(active.base_url)
            route_type = (
                "local"
                if is_loopback
                else "corporate"
                if configured_type == "corporate"
                else "external"
            )
            ready = active.ready
            status = "error" if active.last_error else (
                "ready" if ready else "needs_api_key"
            )
            if active.last_error:
                detail = f"Ошибка внешней модели: {active.last_error}"
            elif ready:
                detail = (
                    f"Корпоративная модель настроена: {active.model_name}"
                    if route_type == "corporate"
                    else f"Локальный API настроен: {active.model_name}"
                    if route_type == "local"
                    else f"Внешняя модель настроена: {active.model_name}"
                )
            else:
                detail = "Введите API-ключ внешней модели"
            remote_runtime = {
                "mode": configured_mode,
                "provider": (
                    "Локальный OpenAI-compatible API"
                    if route_type == "local"
                    else "Корпоративный OpenAI-compatible API"
                    if route_type == "corporate"
                    else "Внешний OpenAI-compatible API"
                ),
                "provider_type": route_type,
                "configured_provider_type": configured_type,
                "data_policy_max": (
                    "restricted"
                    if route_type == "local"
                    else "confidential"
                    if route_type == "corporate"
                    else "internal"
                ),
                "model": active.model_name,
                "base_url": active.base_url,
                "ready": ready and active.last_error is None,
                "status": status,
                "detail": detail,
                "requires_api_key": not openai_url_is_loopback(active.base_url),
                "has_api_key": active.has_api_key,
                "local_fallback_ready": local_ready,
                "auto_remote_policy": settings.get(
                    "auto_remote_policy", "local_only"
                ),
                "remote_ready": ready and active.last_error is None,
            }
            if configured_mode == "auto":
                local_model_name = Path(self.config.llm.model).name
                remote_runtime.update(
                    {
                        "provider": "Auto · MLX с разрешённым удалённым маршрутом",
                        "provider_type": "auto",
                        "model": local_model_name,
                        "remote_model": active.model_name,
                        "ready": local_ready,
                        "status": "ready" if local_ready else "loading",
                        "detail": (
                            "Auto готов: по умолчанию локальная модель; "
                            + (
                                "удалённый маршрут доступен для допустимых данных"
                                if remote_runtime["remote_ready"]
                                and settings.get("auto_remote_policy") == "eligible"
                                else "удалённый маршрут не используется"
                            )
                        ),
                    }
                )
            return remote_runtime

        model_name = Path(self.config.llm.model).name
        if configured_mode == "auto":
            return {
                "mode": "auto",
                "provider": "Auto · локальная модель",
                "provider_type": "auto",
                "configured_provider_type": settings.get(
                    "external_provider_type", "external"
                ),
                "data_policy_max": "restricted",
                "model": model_name,
                "remote_model": settings.get("llm_model", ""),
                "base_url": settings.get("llm_base_url") or None,
                "ready": local_ready,
                "status": "ready" if local_ready else "loading",
                "detail": (
                    "Auto готов: используется локальная модель"
                    if local_ready
                    else "Локальная модель для Auto загружается"
                ),
                "requires_api_key": False,
                "has_api_key": False,
                "local_fallback_ready": local_ready,
                "auto_remote_policy": settings.get(
                    "auto_remote_policy", "local_only"
                ),
                "remote_ready": False,
            }
        return {
            "mode": "local",
            "provider": "MLX",
            "provider_type": "local",
            "configured_provider_type": "local",
            "data_policy_max": "restricted",
            "model": model_name,
            "base_url": None,
            "ready": local_ready,
            "status": "ready" if local_ready else "loading",
            "detail": (
                f"Локальная модель готова: {model_name}"
                if local_ready
                else "Локальная модель загружается"
            ),
            "requires_api_key": False,
            "has_api_key": False,
            "local_fallback_ready": local_ready,
            "auto_remote_policy": settings.get(
                "auto_remote_policy", "local_only"
            ),
            "remote_ready": False,
        }

    def _local_chat_ready(self) -> bool:
        return bool(
            getattr(self._local_chat, "model", None) is not None
            and getattr(self._local_chat, "tokenizer", None) is not None
        )

    def _chat_backend_for_turn(
        self,
        turn: TurnContext,
    ) -> tuple[Any, dict[str, Any]]:
        settings = self.store.settings()
        mode = settings.get("model_mode", "local")
        policy_route = turn.policy.route if turn.policy is not None else "local"
        remote: Any = self._remote_chat
        if remote is None and mode == "external":
            candidate = getattr(self.assistant, "chat", None)
            if candidate is not None and candidate is not self._local_chat:
                remote = candidate

        selection_reason = "configured_local"
        backend: Any = self._local_chat
        if mode == "external" and remote is not None:
            backend = remote
            selection_reason = "configured_remote"
        elif mode == "auto":
            remote_ready = bool(getattr(remote, "ready", False))
            remote_is_loopback = bool(
                isinstance(remote, OpenAICompatibleChat)
                and openai_url_is_loopback(remote.base_url)
            )
            remote_allowed = (
                settings.get("auto_remote_policy") == "eligible"
                and (policy_route in {"corporate", "external"} or remote_is_loopback)
            )
            if remote is not None and remote_ready and remote_allowed:
                backend = remote
                selection_reason = "auto_eligible"
            elif remote_allowed:
                selection_reason = "remote_not_ready"
            elif settings.get("auto_remote_policy") != "eligible":
                selection_reason = "auto_local_only"
            else:
                selection_reason = "data_policy_local"

        if backend is self._local_chat:
            actual_route = "local_mlx"
            provider_type = "local"
            model = Path(self.config.llm.model).name
        else:
            base_url = str(getattr(backend, "base_url", ""))
            configured_type = settings.get("external_provider_type", "external")
            if base_url and openai_url_is_loopback(base_url):
                actual_route = "local_api"
                provider_type = "local"
            elif configured_type == "corporate":
                actual_route = "corporate_api"
                provider_type = "corporate"
            else:
                actual_route = "external_api"
                provider_type = "external"
            model = str(getattr(backend, "model_name", settings.get("llm_model", "")))

        return backend, {
            "configured_mode": mode,
            "policy_route": policy_route,
            "actual_route": actual_route,
            "provider_type": provider_type,
            "model": model,
            "selection_reason": selection_reason,
            "fallback_used": False,
        }

    def start_session(self) -> None:
        if self.session_thread is not None and self.session_thread.is_alive():
            return
        if self.task_lock.locked():
            self.emitter.emit("error", message="Дождитесь завершения текущего ответа")
            self.emitter.emit("session_stopped")
            return
        self.stop_event.clear()
        self.session_thread = threading.Thread(
            target=self._voice_loop,
            name="voice-session",
            daemon=True,
        )
        self.session_thread.start()

    def start_dictation(self, *, destination: str = "system") -> None:
        """Start a push-to-talk capture that never invokes the LLM or TTS."""

        if destination not in {"system", "composer"}:
            self.emitter.emit(
                "dictation_error",
                message="Неизвестное назначение диктовки",
                retryable=False,
                destination=destination,
            )
            return
        if self.dictation_thread is not None and self.dictation_thread.is_alive():
            return
        with self._dictation_stt_worker_lock:
            stalled_worker = self._dictation_stt_worker
        if stalled_worker is not None and stalled_worker.is_alive():
            self.emitter.emit(
                "dictation_error",
                message=(
                    "Предыдущее распознавание ещё завершается. "
                    "Подождите или перезапустите приложение"
                ),
                retryable=True,
                code="stt_worker_busy",
                destination=destination,
            )
            return
        if self.session_thread is not None and self.session_thread.is_alive():
            self.emitter.emit(
                "dictation_error",
                message="Сначала остановите голосовой разговор",
                retryable=True,
                destination=destination,
            )
            return
        if self.task_lock.locked():
            self.emitter.emit(
                "dictation_error",
                message="Дождитесь завершения текущей задачи",
                retryable=True,
                destination=destination,
            )
            return
        self.dictation_release_event.clear()
        self.dictation_cancel_event.clear()
        thread = threading.Thread(
            target=self._dictation_loop,
            args=(destination,),
            name="push-to-talk-dictation",
            daemon=True,
        )
        self.dictation_thread = thread
        thread.start()

    def stop_dictation(self) -> None:
        """Finish the held-key capture and transcribe everything recorded."""

        self.dictation_release_event.set()

    def cancel_dictation(self) -> None:
        """Abort dictation without returning partial text (shutdown/error path)."""

        # Serialize cancellation with the final ready event. Once this method
        # returns, a worker can no longer publish text for the cancelled press.
        with self._dictation_event_lock:
            self.dictation_cancel_event.set()
            self.dictation_release_event.set()

    def _transcribe_dictation(self, audio: Any) -> tuple[str, float] | None:
        """Run STT without letting a native/model stall retain ``task_lock``.

        Python cannot safely kill a thread that is blocked in a native model.
        The worker is therefore isolated as a daemon while this lifecycle
        thread remains responsive to cancellation and a strict timeout.
        """

        results: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=1)
        started = time.perf_counter()

        def transcribe() -> None:
            try:
                text = self.assistant.stt.transcribe(
                    audio,
                    self.config.audio.sample_rate,
                )
            except BaseException as exc:
                results.put(("error", exc))
            else:
                results.put(("ready", "" if text is None else str(text).strip()))
            finally:
                current = threading.current_thread()
                with self._dictation_stt_worker_lock:
                    if self._dictation_stt_worker is current:
                        self._dictation_stt_worker = None

        worker = threading.Thread(
            target=transcribe,
            name="push-to-talk-stt",
            daemon=True,
        )
        with self._dictation_stt_worker_lock:
            self._dictation_stt_worker = worker
        worker.start()
        deadline = time.monotonic() + self._DICTATION_STT_TIMEOUT_S
        while True:
            if self.dictation_cancel_event.is_set():
                return None
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Превышено время распознавания диктовки")
            try:
                status, payload = results.get(
                    timeout=min(self._DICTATION_STT_POLL_S, remaining)
                )
            except queue.Empty:
                continue
            # Cancellation can race the model's return. It wins over a result
            # that has not yet been exposed to the desktop shell.
            if self.dictation_cancel_event.is_set():
                return None
            if status == "error":
                raise payload
            return str(payload), time.perf_counter() - started

    def _dictation_loop(self, destination: str) -> None:
        acquired = self.task_lock.acquire(blocking=False)
        if not acquired:
            # Terminal status must be the last event.  If ``stopped`` follows
            # ``dictation_error``, the desktop shell can incorrectly return to
            # a permanent "transcribing" state after it already handled the
            # failure.
            self.emitter.emit("dictation_stopped", destination=destination)
            self.emitter.emit("state", state="ready", detail="Готов к работе")
            self.emitter.emit(
                "dictation_error",
                message="Дождитесь завершения текущей задачи",
                retryable=True,
                destination=destination,
            )
            return
        capture_stopped_emitted = False
        try:
            with Microphone(self.config.audio) as microphone:
                self.emitter.emit("dictation_started", destination=destination)
                audio = microphone.record_until_release(
                    self.dictation_release_event,
                    cancel_event=self.dictation_cancel_event,
                    max_duration_s=self._DICTATION_MAX_DURATION_S,
                )
            self.emitter.emit("dictation_stopped", destination=destination)
            capture_stopped_emitted = True
            if self.dictation_cancel_event.is_set():
                self.emitter.emit("state", state="ready", detail="Готов к работе")
                return
            if audio is None:
                self.emitter.emit("state", state="ready", detail="Готов к работе")
                self.emitter.emit(
                    "dictation_error",
                    message="Удерживайте клавишу чуть дольше и произнесите текст",
                    retryable=True,
                    destination=destination,
                )
                return
            self.emitter.emit(
                "state",
                state="transcribing",
                detail="Распознаю диктовку…",
            )
            transcription = self._transcribe_dictation(audio)
            if transcription is None or self.dictation_cancel_event.is_set():
                self.emitter.emit("state", state="ready", detail="Готов к работе")
                return
            text, elapsed = transcription
            if not text:
                self.emitter.emit("state", state="ready", detail="Готов к работе")
                self.emitter.emit(
                    "dictation_error",
                    message="Речь не распознана",
                    retryable=True,
                    destination=destination,
                )
                return
            with self._dictation_event_lock:
                if self.dictation_cancel_event.is_set():
                    self.emitter.emit("state", state="ready", detail="Готов к работе")
                    return
                self.emitter.emit("state", state="ready", detail="Готов к работе")
                self.emitter.emit(
                    "dictation_ready",
                    text=text,
                    seconds=round(elapsed, 3),
                    editable=True,
                    destination=destination,
                )
        except PushToTalkDurationExceededError as exc:
            if not capture_stopped_emitted:
                self.emitter.emit("dictation_stopped", destination=destination)
                capture_stopped_emitted = True
            self.emitter.emit("state", state="ready", detail="Готов к работе")
            self.emitter.emit(
                "dictation_error",
                message=(
                    "Диктовка остановлена: превышен лимит записи. "
                    "Отпустите клавишу и повторите коротким фрагментом"
                ),
                retryable=True,
                error_type=type(exc).__name__,
                code="duration_limit",
                destination=destination,
            )
        except TimeoutError as exc:
            if not capture_stopped_emitted:
                self.emitter.emit("dictation_stopped", destination=destination)
                capture_stopped_emitted = True
            self.emitter.emit("state", state="ready", detail="Готов к работе")
            self.emitter.emit(
                "dictation_error",
                message=(
                    "Распознавание заняло слишком много времени. "
                    "Попробуйте более короткий фрагмент"
                ),
                retryable=True,
                error_type=type(exc).__name__,
                code="stt_timeout",
                destination=destination,
            )
        except BaseException as exc:
            if not capture_stopped_emitted:
                self.emitter.emit("dictation_stopped", destination=destination)
                capture_stopped_emitted = True
            self.emitter.emit("state", state="ready", detail="Готов к работе")
            self.emitter.emit(
                "dictation_error",
                message="Диктовка недоступна на этом устройстве",
                retryable=True,
                error_type=type(exc).__name__,
                destination=destination,
            )
        finally:
            self.task_lock.release()
            if not capture_stopped_emitted:
                self.emitter.emit("dictation_stopped", destination=destination)

    def stop_session(self) -> None:
        self.stop_event.set()
        with self._turn_lock:
            if self._current_turn_cancel is not None:
                self._current_turn_cancel.set()
        self.emitter.emit("state", state="stopping", detail="Останавливаюсь…")

    @staticmethod
    def _parse_attachments(value: Any) -> list[dict[str, str | None]]:
        if value in (None, []):
            return []
        if not isinstance(value, list):
            raise ValueError("Вложения должны быть переданы списком")
        if len(value) > 20:
            raise ValueError("К одному запросу можно добавить не более 20 файлов")
        attachments: list[dict[str, str | None]] = []
        seen: set[str] = set()
        for raw in value:
            if isinstance(raw, str):
                path = raw.strip()
                kind = None
            elif isinstance(raw, dict):
                path = str(raw.get("path", "")).strip()
                raw_kind = raw.get("kind")
                kind = str(raw_kind).strip() if raw_kind else None
            else:
                raise ValueError("Некорректное описание вложения")
            if not path:
                raise ValueError("Для вложения не указан путь к файлу")
            expanded = Path(path).expanduser()
            lexical_path = str(expanded.absolute())
            dedupe_key = str(expanded.resolve())
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            attachments.append({"path": lexical_path, "kind": kind})
        return attachments

    def submit_text(
        self,
        text: str,
        *,
        speak: bool = True,
        attachments: list[dict[str, str | None]] | None = None,
    ) -> None:
        if self.session_thread is not None and self.session_thread.is_alive():
            self.emitter.emit("error", message="Остановите голосовой режим перед текстовым запросом")
            return
        if self.task_lock.locked():
            self.emitter.emit(
                "error",
                message="Дождитесь завершения текущей задачи или автоматизации",
            )
            return
        self.stop_event.clear()
        thread = threading.Thread(
            target=self._text_turn,
            args=(text, speak, list(attachments or [])),
            name="text-turn",
            daemon=True,
        )
        self.text_thread = thread
        thread.start()

    def retry_speech(self) -> None:
        """Replay the latest assistant message without starting a new LLM turn."""

        if self.session_thread is not None and self.session_thread.is_alive():
            self.emitter.emit(
                "speech_error",
                message="Остановите голосовой режим перед повтором ответа",
                task_id=self.current_task_id,
                retryable=True,
            )
            return
        if self.task_lock.locked():
            self.emitter.emit(
                "speech_error",
                message="Дождитесь завершения текущего ответа",
                task_id=self.current_task_id,
                retryable=True,
            )
            return
        self.stop_event.clear()
        thread = threading.Thread(
            target=self._retry_speech_turn,
            name="retry-speech",
            daemon=True,
        )
        thread.start()

    def _retry_speech_turn(self) -> None:
        cancel_event = threading.Event()
        playback_started = threading.Event()
        task_id = self.current_task_id
        try:
            with self.task_lock:
                self._set_current_turn(cancel_event)
                message = self._latest_assistant_message(task_id)
                if message is None:
                    self.emitter.emit(
                        "speech_error",
                        message="Нет ответа ассистента, который можно повторить",
                        task_id=task_id,
                        retryable=False,
                    )
                    return

                def on_start() -> None:
                    playback_started.set()
                    self.emitter.emit("state", state="speaking", detail="Повторяю ответ…")

                try:
                    try:
                        message_metadata = json.loads(message.get("metadata") or "{}")
                    except (TypeError, json.JSONDecodeError):
                        message_metadata = {}
                    speech_text = str(message_metadata.get("spoken_text") or "").strip()
                    if not speech_text:
                        speech_text = concise_speech_text(
                            message["content"],
                            max_chars=self.config.assistant.max_tts_chars,
                            max_segments=self.config.assistant.max_tts_segments,
                        )
                    self.assistant.player.play(
                        self.assistant.tts.synthesize(
                            speech_text,
                            cancel_event=cancel_event,
                        ),
                        cancel_event=cancel_event,
                        on_start=on_start,
                    )
                    if not cancel_event.is_set() and not playback_started.is_set():
                        raise RuntimeError("TTS не вернул аудио для повторного воспроизведения")
                except BaseException as exc:
                    if not cancel_event.is_set():
                        self.emitter.emit(
                            "speech_error",
                            message=str(exc) or type(exc).__name__,
                            task_id=task_id,
                            retryable=True,
                        )
                    return

                if not cancel_event.is_set():
                    self.emitter.emit(
                        "speech_recovered",
                        text=message["content"],
                        task_id=task_id,
                    )
        finally:
            self._clear_current_turn(cancel_event)
            self.emitter.emit("state", state="ready", detail="Готов к работе")

    def _latest_assistant_message(
        self,
        task_id: str | None,
    ) -> dict[str, Any] | None:
        if not task_id:
            return None
        return next(
            (
                message
                for message in reversed(self.store.messages(task_id))
                if message["role"] == "assistant" and str(message["content"]).strip()
            ),
            None,
        )

    def _try_deterministic_request(
        self,
        text: str,
        *,
        speak: bool,
        cancel_event: threading.Event | None = None,
    ) -> bool:
        """Handle bounded local commands before any LLM route is selected."""

        try:
            plan_command = self.orchestrator.plan_command(text)
        except ValueError as exc:
            self.emitter.emit("error", message=str(exc))
            return True
        if plan_command is not None:
            if not self.current_task_id:
                self.emitter.emit(
                    "error",
                    message="Сначала выберите задачу, план которой нужно изменить",
                )
                return True
            try:
                result = self.orchestrator.mutate_task_plan(
                    text,
                    self.current_task_id,
                )
            except (KeyError, ValueError) as exc:
                self.emitter.emit("error", message=str(exc))
                return True
            if result is None:
                return False
            task = self.store.get_task(self.current_task_id)
            classification = str(task["classification"])
            self.store.add_message(
                task["id"],
                "user",
                text,
                classification=classification,
            )
            self.store.add_message(
                task["id"],
                "assistant",
                str(result["message"]),
                metadata={
                    "deterministic": True,
                    "local_command": "task_plan",
                    "plan_action": result["action"],
                    "plan_index": result["index"],
                },
                classification=classification,
            )
            self._set_current_workspace(str(task["workspace_id"]))
            self.emitter.emit("plan_updated", result=result)
            self._emit_deterministic_response(
                task_id=str(task["id"]),
                user_text=text,
                reply=str(result["message"]),
                skill=None,
                sources=[],
                artifact=None,
                speak=speak,
                cancel_event=cancel_event,
                event="task_plan",
            )
            return True

        try:
            period = self.orchestrator.digest_command(text)
        except ValueError as exc:
            self.emitter.emit("error", message=str(exc))
            return True
        if period is None:
            return False
        self._run_structured_digest(
            period,
            request_text=text,
            speak=speak,
            cancel_event=cancel_event,
        )
        return True

    def _run_structured_digest(
        self,
        period: str,
        *,
        request_text: str,
        speak: bool,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        digest = self.orchestrator.persist_digest(
            self.current_workspace_id,
            period,
            request_text=request_text,
        )
        task = digest["task"]
        self._set_current_workspace(str(task["workspace_id"]))
        self.current_task_id = str(task["id"])
        self.emitter.emit("digest_generated", digest=digest)
        self._emit_deterministic_response(
            task_id=self.current_task_id,
            user_text=request_text,
            reply=str(digest["text"]),
            skill="Дайджест",
            sources=list(digest["source_references"]),
            artifact=digest["artifact"],
            speak=speak,
            cancel_event=cancel_event,
            event="digest",
        )
        return digest

    def _emit_deterministic_response(
        self,
        *,
        task_id: str,
        user_text: str,
        reply: str,
        skill: str | None,
        sources: list[dict[str, Any]],
        artifact: dict[str, Any] | None,
        speak: bool,
        cancel_event: threading.Event | None,
        event: str,
    ) -> None:
        started = time.perf_counter()
        cancel_event = cancel_event or threading.Event()
        self.emitter.emit("user", text=user_text, task_id=task_id)
        self.emitter.emit(
            "assistant_start",
            task_id=task_id,
            skill=skill,
            sources=sources,
            deterministic=True,
        )
        self.emitter.emit("assistant_delta", text=reply)
        spoken = False
        spoken_text = ""
        tts_error: str | None = None
        if speak and reply and not cancel_event.is_set():
            spoken_text = concise_speech_text(
                reply,
                max_chars=self.config.assistant.max_tts_chars,
                max_segments=self.config.assistant.max_tts_segments,
            )
            playback_started = threading.Event()

            def on_start() -> None:
                playback_started.set()
                self.emitter.emit(
                    "state",
                    state="speaking",
                    detail="Озвучиваю локальный результат…",
                )

            try:
                self.assistant.player.play(
                    self.assistant.tts.synthesize(
                        spoken_text,
                        cancel_event=cancel_event,
                    ),
                    cancel_event=cancel_event,
                    on_start=on_start,
                )
                spoken = playback_started.is_set() and not cancel_event.is_set()
            except BaseException as exc:
                if not cancel_event.is_set():
                    tts_error = str(exc) or type(exc).__name__
                    self.emitter.emit(
                        "speech_error",
                        message=tts_error,
                        task_id=task_id,
                        retryable=True,
                    )
        self.emitter.emit(
            "assistant_end",
            text=reply,
            seconds=round(time.perf_counter() - started, 2),
            interrupted=cancel_event.is_set(),
            task_id=task_id,
            artifact=artifact,
            spoken=spoken,
            spoken_text=spoken_text,
            tts_error=tts_error,
            deterministic=True,
            local_event=event,
            quick_actions=self.orchestrator.quick_actions(
                task_id,
                artifact["id"] if artifact else None,
            ),
        )
        self.emit_snapshot()

    def _voice_loop(self) -> None:
        active_turn: TurnContext | None = None
        try:
            with self.task_lock, Microphone(self.config.audio) as microphone:
                start_adaptive = getattr(microphone, "start_adaptive", None)
                if callable(start_adaptive):
                    threshold = start_adaptive()
                    calibration_mode = "adaptive"
                else:
                    # Compatibility path for injected/test microphones and
                    # older platform adapters. Production compact voice uses
                    # immediate adaptive calibration and does not consume the
                    # user's first second of speech.
                    threshold = microphone.calibrate()
                    calibration_mode = "blocking"
                if self.stop_event.is_set():
                    return
                self.emitter.emit(
                    "calibrated",
                    threshold=round(threshold, 4),
                    mode=calibration_mode,
                )
                pending_input: _PendingVoiceInput | None = None
                while not self.stop_event.is_set():
                    if pending_input is None:
                        self.emitter.emit("state", state="listening", detail="Слушаю…")
                        audio = microphone.listen(self.stop_event)
                        detected_at = time.perf_counter()
                        response_started_at: float | None = None
                        response_timing_origin = "estimated_speech_end"
                    else:
                        audio = pending_input.audio
                        detected_at = pending_input.detected_at
                        # Keep the clock captured at cancel_event.set().  In
                        # particular, do not reset it after the interrupted
                        # answer worker has been joined: releasing that worker
                        # is part of the observable barge-in recovery latency.
                        response_started_at = pending_input.response_started_at
                        response_timing_origin = "barge_in_interrupt"
                        pending_input = None
                    if audio is None or self.stop_event.is_set():
                        break

                    speech_tail = trailing_silence_seconds(
                        audio,
                        sample_rate=self.config.audio.sample_rate,
                        block_ms=self.config.audio.block_ms,
                        threshold=threshold,
                        limit_ms=self.config.audio.silence_ms,
                    )
                    voice_turn_started_at = detected_at - speech_tail
                    if response_started_at is None:
                        response_started_at = voice_turn_started_at
                    self.emitter.emit("state", state="transcribing", detail="Распознаю речь…")
                    started = time.perf_counter()
                    text = self.assistant.stt.transcribe(audio, self.config.audio.sample_rate)
                    stt_seconds = time.perf_counter() - started
                    if not text:
                        self.emitter.emit("notice", message="Речь не распознана")
                        continue
                    self.emitter.emit("metric", name="stt", seconds=round(stt_seconds, 2))
                    self.emitter.emit(
                        "metric",
                        name="voice_transcript_ready",
                        seconds=round(time.perf_counter() - voice_turn_started_at, 3),
                    )

                    if text.casefold().strip(" .!?") in {
                        phrase.casefold() for phrase in self.config.assistant.exit_phrases
                    }:
                        break
                    if self.store.settings().get("voice_review_before_send") == "true":
                        self.emitter.emit(
                            "dictation_ready",
                            text=text,
                            seconds=round(stt_seconds, 2),
                            editable=True,
                        )
                        break
                    if self._try_deterministic_request(
                        text,
                        speak=True,
                        cancel_event=self.stop_event,
                    ):
                        microphone.discard_pending()
                        continue
                    turn = self._prepare_turn(text, spoken=True)
                    active_turn = turn
                    microphone.discard_pending()
                    pending_input = self._answer_with_barge_in(
                        turn,
                        microphone,
                        threshold,
                        response_started_at=response_started_at,
                        response_timing_origin=response_timing_origin,
                        stt_seconds=stt_seconds,
                    )
                    active_turn = None
                    if pending_input is None:
                        microphone.discard_pending()
        except BaseException as exc:
            if active_turn is not None:
                self._fail_running_turn(active_turn.task_id, exc)
            self.emitter.emit("error", message=str(exc))
        finally:
            self.stop_event.set()
            self.emitter.emit("state", state="ready", detail="Готов к работе")
            self.emitter.emit("session_stopped")

    def _text_turn(
        self,
        text: str,
        speak: bool = True,
        attachments: list[dict[str, str | None]] | None = None,
    ) -> None:
        cancel_event = threading.Event()
        turn: TurnContext | None = None
        try:
            with self.task_lock:
                self._set_current_turn(cancel_event)
                if not attachments and self._try_deterministic_request(
                    text,
                    speak=speak,
                    cancel_event=cancel_event,
                ):
                    return
                turn = self._prepare_turn_with_attachments(
                    text,
                    attachments or [],
                )
                self._answer(turn, cancel_event=cancel_event, speak=speak)
        except BaseException as exc:
            if turn is not None:
                self._fail_running_turn(turn.task_id, exc)
            self.emitter.emit("error", message=str(exc))
        finally:
            self._clear_current_turn(cancel_event)
            self.emitter.emit("state", state="ready", detail="Готов к работе")

    def _prepare_turn_with_attachments(
        self,
        text: str,
        attachments: list[dict[str, str | None]],
    ) -> TurnContext:
        if not attachments:
            return self._prepare_turn(text, spoken=False)

        previous_task_id = self.current_task_id
        skill = self.store.find_skill(text.strip(), self.current_workspace_id)
        task = self.orchestrator._ensure_task(
            text.strip(),
            self.current_workspace_id,
            previous_task_id,
            skill,
        )
        task_id = str(task["id"])
        created_task = task_id != previous_task_id
        self.current_task_id = task_id
        imported: list[dict[str, Any]] = []
        try:
            for attachment in attachments:
                imported.append(
                    self.orchestrator.import_file(
                        Path(str(attachment["path"])),
                        workspace_id=self.current_workspace_id,
                        kind=attachment.get("kind"),
                        task_id=task_id,
                    )
                )
        except BaseException:
            for source in reversed(imported):
                try:
                    self.store.rollback_source_import(str(source["id"]))
                except BaseException:
                    pass
            if created_task:
                try:
                    self.store.delete_task(task_id)
                    self.current_task_id = previous_task_id
                except BaseException:
                    pass
            raise

        try:
            turn = self._prepare_turn(
                text,
                spoken=False,
                task_id_override=task_id,
            )
        except BaseException:
            for source in reversed(imported):
                try:
                    self.store.rollback_source_import(str(source["id"]))
                except BaseException:
                    pass
            raise

        for source in imported:
            self.emitter.emit("source_imported", source=source)
            if source["kind"] == "meeting":
                self.current_meeting_id = source.get("meeting_id")
                self._meeting_diff = []
                self._meeting_briefing = None
                self._register_meeting_event(source)
        if any(source["kind"] == "meeting" for source in imported):
            self._sync_meeting_attention()
        return turn

    def _answer_with_barge_in(
        self,
        turn: TurnContext,
        microphone: Microphone,
        threshold: float,
        *,
        response_started_at: float | None = None,
        response_timing_origin: str | None = None,
        stt_seconds: float | None = None,
    ) -> _PendingVoiceInput | None:
        cancel_event = threading.Event()
        answer_done = threading.Event()
        speaking = threading.Event()
        playback_reference = PlaybackReference()
        errors: list[BaseException] = []

        def run_answer() -> None:
            try:
                self._answer(
                    turn,
                    cancel_event=cancel_event,
                    speaking_event=speaking,
                    playback_reference=playback_reference,
                    response_started_at=response_started_at,
                    response_timing_origin=response_timing_origin,
                    stt_seconds=stt_seconds,
                )
            except BaseException as exc:
                errors.append(exc)
            finally:
                answer_done.set()

        self._set_current_turn(cancel_event)
        worker = threading.Thread(target=run_answer, name="answer-with-barge-in", daemon=True)
        worker.start()

        interrupted = False
        captured_audio = None
        captured_at: float | None = None
        interrupted_at: float | None = None
        barge_detector = BargeInDetector(self.config.audio, threshold)
        # Retain the configured acoustic pre-roll *and* every block spent
        # confirming the barge-in. Otherwise the trigger delay itself cuts the
        # first syllable before the audio reaches Whisper.
        capture_history_ms = (
            self.config.audio.barge_in_pre_roll_ms
            + self.config.audio.barge_in_trigger_ms
        )
        pre_roll_blocks = max(
            1,
            (
                capture_history_ms
                + self.config.audio.block_ms
                - 1
            )
            // self.config.audio.block_ms,
        )
        recent_blocks = deque(maxlen=pre_roll_blocks)
        capture_detector: UtteranceDetector | None = None

        try:
            while not self.stop_event.is_set():
                if answer_done.is_set() and not interrupted:
                    break
                block = microphone.read_block(timeout=0.1)
                if block is None:
                    continue

                if capture_detector is not None:
                    captured_audio = capture_detector.feed(block)
                    if captured_audio is not None:
                        captured_at = time.perf_counter()
                        break
                    continue

                recent_blocks.append(block)
                should_interrupt = (
                    self.config.assistant.barge_in_enabled
                    and barge_detector.feed(
                        block,
                        speaker_active=speaking.is_set(),
                        speaker_level=playback_reference.recent_level(),
                    )
                )
                if not should_interrupt:
                    continue

                interrupted = True
                interrupted_at = time.perf_counter()
                cancel_event.set()
                self.emitter.emit("interrupted")
                self.emitter.emit(
                    "state",
                    state="listening",
                    detail="Перебили — слушаю…",
                )
                capture_config = replace(
                    self.config.audio,
                    pre_roll_ms=self.config.audio.barge_in_pre_roll_ms,
                    min_utterance_ms=self.config.audio.barge_in_min_utterance_ms,
                )
                capture_detector = UtteranceDetector(capture_config, threshold)
                for buffered_block in recent_blocks:
                    captured_audio = capture_detector.feed(buffered_block)
                    if captured_audio is not None:
                        captured_at = time.perf_counter()
                        break
                recent_blocks.clear()
                if captured_audio is not None:
                    break
        finally:
            if self.stop_event.is_set():
                cancel_event.set()
            join_timeout = 0.75 if cancel_event.is_set() else 30.0
            worker.join(timeout=join_timeout)
            if worker.is_alive():
                cancel_event.set()
                self.emitter.emit(
                    "metric",
                    name="answer_cancel_timeout",
                    seconds=round(join_timeout, 3),
                )
            self._clear_current_turn(cancel_event)

        if errors:
            raise errors[0]
        if captured_audio is None:
            return None
        return _PendingVoiceInput(
            audio=captured_audio,
            detected_at=(
                captured_at if captured_at is not None else time.perf_counter()
            ),
            response_started_at=(
                interrupted_at
                if interrupted_at is not None
                else time.perf_counter()
            ),
        )

    def _answer(
        self,
        turn: TurnContext,
        *,
        cancel_event: threading.Event,
        speaking_event: threading.Event | None = None,
        playback_reference: PlaybackReference | None = None,
        speak: bool = True,
        response_started_at: float | None = None,
        response_timing_origin: str | None = None,
        stt_seconds: float | None = None,
    ) -> None:
        started = (
            response_started_at
            if response_started_at is not None
            else time.perf_counter()
        )
        speech_started = False
        spoken_text = ""
        tts_error: str | None = None
        first_token_seconds: float | None = None
        first_audio_seconds: float | None = None
        response_parts: list[str] = []
        response_incomplete = False
        is_attention = self._is_attention_query(turn.user_text)
        if is_attention:
            chat_backend: Any = self._local_chat
            route_metadata: dict[str, Any] = {
                "configured_mode": self.store.settings().get("model_mode", "local"),
                "policy_route": "local",
                "actual_route": "local_deterministic",
                "provider_type": "local",
                "model": "attention-engine",
                "selection_reason": "deterministic_attention",
                "fallback_used": False,
            }
        else:
            chat_backend, route_metadata = self._chat_backend_for_turn(turn)

        self.emitter.emit(
            "assistant_start",
            task_id=turn.task_id,
            skill=turn.skill["name"] if turn.skill else None,
            sources=[self.orchestrator.source_reference(source) for source in turn.sources],
            llm_route=route_metadata,
        )

        def on_phase(phase: str) -> None:
            nonlocal speech_started, first_audio_seconds
            if phase == "speaking":
                speech_started = True
                if first_audio_seconds is None:
                    first_audio_seconds = round(time.perf_counter() - started, 3)
                    self.emitter.emit(
                        "metric",
                        name="voice_first_audio",
                        seconds=first_audio_seconds,
                        task_id=turn.task_id,
                    )
            if phase == "speaking" and speaking_event is not None:
                speaking_event.set()
            elif phase == "thinking" and speaking_event is not None:
                speaking_event.clear()
            self.emitter.emit(
                "state",
                state=phase,
                detail="Думаю…" if phase == "thinking" else "Говорю…",
            )

        def on_speech_error(error: BaseException) -> None:
            nonlocal tts_error
            if cancel_event.is_set() or tts_error is not None:
                return
            tts_error = str(error) or type(error).__name__
            if speaking_event is not None:
                speaking_event.clear()
            self.emitter.emit(
                "speech_error",
                message=tts_error,
                task_id=turn.task_id,
                retryable=True,
            )

        def capture_spoken_text(text: str) -> None:
            nonlocal spoken_text
            spoken_text = text.strip()

        def on_token(token: str) -> None:
            nonlocal first_token_seconds
            if token:
                response_parts.append(token)
                if first_token_seconds is None:
                    first_token_seconds = round(time.perf_counter() - started, 3)
                    self.emitter.emit(
                        "metric",
                        name="llm_first_token",
                        seconds=first_token_seconds,
                        task_id=turn.task_id,
                    )
            self.emitter.emit("assistant_delta", text=token)

        def invoke_model(backend: Any, prompt: str) -> str:
            return self.assistant.answer(
                prompt,
                on_token=on_token,
                on_phase=on_phase,
                cancel_event=cancel_event,
                echo=False,
                chat_history=turn.history,
                remember=False,
                speak=speak,
                chat_backend=backend,
                model_route=str(route_metadata.get("actual_route") or ""),
                on_playback_block=(
                    playback_reference.update if playback_reference is not None else None
                ),
                on_playback_end=lambda: on_phase("thinking"),
                on_speech_error=on_speech_error,
                on_speech_text=capture_spoken_text,
            )

        if is_attention:
            on_phase("thinking")
            reply = render_attention(self._rank_attention(), limit=8)
            if not cancel_event.is_set():
                on_token(reply)
            if speak and reply and not cancel_event.is_set():
                spoken_text = concise_speech_text(
                    reply,
                    max_chars=self.config.assistant.max_tts_chars,
                    max_segments=self.config.assistant.max_tts_segments,
                )
                try:
                    self.assistant.player.play(
                        self.assistant.tts.synthesize(
                            spoken_text,
                            cancel_event=cancel_event,
                        ),
                        cancel_event=cancel_event,
                        on_start=lambda: on_phase("speaking"),
                        on_block=(
                            playback_reference.update
                            if playback_reference is not None
                            else None
                        ),
                    )
                    if not cancel_event.is_set():
                        on_phase("thinking")
                except BaseException as exc:
                    on_speech_error(exc)
        else:
            try:
                reply = invoke_model(chat_backend, turn.prompt)
            except BaseException as exc:
                partial_reply = "".join(response_parts).strip()
                remote_failed_after_text = (
                    chat_backend is not self._local_chat
                    and bool(partial_reply)
                    and not cancel_event.is_set()
                )
                if remote_failed_after_text:
                    # Retrying locally here would append a second answer to
                    # text the user has already seen.  Keep the visible text
                    # as an explicitly incomplete result and expose only a
                    # typed/generic failure marker, never the remote body.
                    response_incomplete = True
                    failed_route = str(
                        route_metadata.get("actual_route") or "remote"
                    )
                    error_type = type(exc).__name__
                    route_metadata = {
                        **route_metadata,
                        "response_incomplete": True,
                        "completion_state": "incomplete",
                        "incomplete_reason": "remote_stream_interrupted_after_first_token",
                    }
                    self.store.audit(
                        turn.task_id,
                        "llm.partial",
                        failed_route,
                        "incomplete",
                        f"error_type={error_type};after_first_token=true",
                    )
                    self.emitter.emit(
                        "notice",
                        task_id=turn.task_id,
                        message=(
                            "Ответ внешней модели получен не полностью. "
                            "Частичный текст сохранён — запрос можно повторить."
                        ),
                    )
                    reply = partial_reply
                else:
                    can_fallback = (
                        chat_backend is not self._local_chat
                        and not partial_reply
                        and not cancel_event.is_set()
                        and self._local_chat_ready()
                    )
                    if not can_fallback:
                        raise
                    failed_route = str(route_metadata.get("actual_route") or "remote")
                    error_type = type(exc).__name__
                    route_metadata = {
                        **route_metadata,
                        "actual_route": "local_mlx",
                        "provider_type": "local",
                        "model": Path(self.config.llm.model).name,
                        "selection_reason": "remote_failure_before_first_token",
                        "fallback_used": True,
                        "fallback_from": failed_route,
                        "fallback_reason": "remote_error_before_first_token",
                        "fallback_error_type": error_type,
                    }
                    self.store.add_task_event(
                        turn.task_id,
                        "routing_fallback",
                        "Удалённая модель недоступна — продолжено локально",
                        f"Исходный маршрут: {failed_route}; тип ошибки: {error_type}",
                    )
                    self.store.audit(
                        turn.task_id,
                        "llm.fallback",
                        "local_mlx",
                        "success",
                        f"from={failed_route};error_type={error_type};before_first_token=true",
                    )
                    self.emitter.emit(
                        "routing_fallback",
                        task_id=turn.task_id,
                        from_route=failed_route,
                        to_route="local_mlx",
                        message="Удалённая модель не ответила. Продолжаю локально; выбранный режим не изменён.",
                        llm_route=route_metadata,
                    )
                    local_prompt = (
                        turn.prompt.rstrip()
                        + "\n\nСлужебное уточнение: удалённый маршрут завершился ошибкой до "
                        "первого токена, поэтому этот ответ формируется локальной моделью. "
                        "Не утверждай, что текст был создан внешним провайдером."
                    )
                    reply = invoke_model(self._local_chat, local_prompt)
        interrupted = cancel_event.is_set()
        spoken = bool(
            speak
            and reply
            and speech_started
            and tts_error is None
            and not interrupted
        )
        total_seconds = round(time.perf_counter() - started, 3)
        performance_metadata = {
            "total_seconds": total_seconds,
            "timing_origin": (
                response_timing_origin
                or (
                    "estimated_speech_end"
                    if response_started_at is not None
                    else "answer_start"
                )
            ),
            **(
                {"stt_seconds": round(stt_seconds, 3)}
                if stt_seconds is not None
                else {}
            ),
            **(
                {"first_token_seconds": first_token_seconds}
                if first_token_seconds is not None
                else {}
            ),
            **(
                {"first_audio_seconds": first_audio_seconds}
                if first_audio_seconds is not None
                else {}
            ),
        }
        self.emitter.emit(
            "metric",
            name="response_total",
            seconds=total_seconds,
            task_id=turn.task_id,
        )
        artifact = self.orchestrator.finish_turn(
            turn,
            reply,
            interrupted=interrupted,
            incomplete=response_incomplete,
            spoken=spoken,
            spoken_text=spoken_text,
            tts_error=tts_error,
            route_metadata=route_metadata,
            performance_metadata=performance_metadata,
        )
        self.emitter.emit(
            "assistant_end",
            text=reply,
            seconds=total_seconds,
            interrupted=interrupted,
            incomplete=response_incomplete,
            task_id=turn.task_id,
            artifact=artifact,
            spoken=spoken,
            spoken_text=spoken_text,
            tts_error=tts_error,
            llm_route=route_metadata,
            performance=performance_metadata,
            quick_actions=(
                []
                if response_incomplete
                else self.orchestrator.quick_actions(
                    turn.task_id,
                    artifact["id"] if artifact else None,
                )
            ),
        )
        self.emit_snapshot()

    def _prepare_turn(
        self,
        text: str,
        *,
        spoken: bool,
        task_id_override: str | None = None,
    ) -> TurnContext:
        # A meta-query about priorities must not turn the currently waiting
        # task back into ``running`` before AttentionEngine has ranked it.
        task_id = (
            task_id_override
            if task_id_override is not None
            else None if self._is_attention_query(text) else self.current_task_id
        )
        try:
            turn = self.orchestrator.prepare_turn(
                text,
                workspace_id=self.current_workspace_id,
                task_id=task_id,
                spoken=spoken,
            )
        except RoutingPolicyError as exc:
            self._set_current_workspace(exc.workspace_id)
            self.current_task_id = exc.task_id
            self.emitter.emit("user", text=exc.user_text, task_id=exc.task_id)
            self.emitter.emit(
                "routing_blocked",
                task_id=exc.task_id,
                route=exc.route,
                allowed_max=exc.allowed_max,
                effective_classification=exc.effective_classification,
                blocked_refs=list(exc.blocked_refs),
                suggested_actions=[
                    "switch_local",
                    "remove_sensitive_context",
                    "review_classification",
                ],
                message=str(exc),
            )
            self.emit_snapshot()
            raise
        self._set_current_workspace(turn.workspace_id)
        self.current_task_id = turn.task_id
        self.emitter.emit("user", text=turn.user_text, task_id=turn.task_id)
        self.emitter.emit(
            "task_context",
            task_id=turn.task_id,
            skill=turn.skill["name"] if turn.skill else None,
            sources=[self.orchestrator.source_reference(source) for source in turn.sources],
        )
        if turn.policy is not None and turn.policy.filtered_refs:
            self.emitter.emit(
                "routing_filtered",
                task_id=turn.task_id,
                route=turn.policy.route,
                allowed_max=turn.policy.allowed_max,
                effective_classification=turn.policy.effective_classification,
                filtered_refs=list(turn.policy.filtered_refs),
                message="Чувствительный автоматически подобранный контекст не передан модели",
            )
        return turn

    def _fail_running_turn(self, task_id: str, error: BaseException) -> None:
        try:
            if self.store.get_task(task_id)["status"] == "running":
                self.orchestrator.fail_turn(task_id, str(error))
                self.emit_snapshot()
        except BaseException as persistence_error:
            self.emitter.emit(
                "error",
                message=f"Не удалось сохранить ошибку задачи: {persistence_error}",
            )

    @staticmethod
    def _is_attention_query(text: str) -> bool:
        normalized = re.sub(r"[^\wа-яё]+", " ", text.casefold()).strip()
        return any(
            phrase in normalized
            for phrase in (
                "что требует внимания",
                "что требует моего внимания",
                "чему уделить внимание",
                "какие приоритеты",
                "мои приоритеты",
                "что срочно",
                "что просрочено",
            )
        )

    def _rank_attention(self) -> list[dict[str, Any]]:
        workspace_id = self.current_workspace_id
        meeting_items = self.store.list_meeting_items(workspace_id)
        tasks = self.store._rows(
            "SELECT * FROM tasks WHERE workspace_id=? ORDER BY updated_at DESC",
            (workspace_id,),
        )
        inbox = self.store._rows(
            """
            SELECT * FROM inbox
            WHERE (workspace_id IS NULL OR workspace_id=?)
              AND kind != 'meeting_attention'
            ORDER BY created_at DESC
            """,
            (workspace_id,),
        )
        approvals = self.store._rows(
            """
            SELECT a.*, t.workspace_id AS workspace_id
            FROM approvals a LEFT JOIN tasks t ON t.id=a.task_id
            WHERE a.task_id IS NULL OR t.workspace_id=?
            ORDER BY a.created_at DESC
            """,
            (workspace_id,),
        )
        automations = self.store._rows(
            """
            SELECT * FROM automations
            WHERE workspace_id IS NULL OR workspace_id=?
            ORDER BY updated_at DESC
            """,
            (workspace_id,),
        )
        proactivity = self.store.settings().get("proactivity", "balanced")
        if proactivity not in {"quiet", "balanced", "proactive"}:
            proactivity = "balanced"
        events = AttentionEngine().rank(
            meeting_items=meeting_items,
            tasks=tasks,
            inbox=inbox,
            approvals=approvals,
            automations=automations,
            proactivity=proactivity,
            workspace_id=workspace_id,
        )
        # Repeated automation/artifact notifications with the same title are
        # one user concern, not dozens of independent priorities.  Preserve
        # the highest-ranked representative while keeping distinct tasks and
        # meeting obligations separate.
        compact_events: list[dict[str, Any]] = []
        seen_inbox_concerns: set[tuple[str, str, str]] = set()
        for event in events:
            if event["kind"].startswith("inbox_"):
                concern = (
                    event["kind"],
                    re.sub(r"\s+", " ", event["title"].casefold()).strip(),
                    event["reason"].casefold(),
                )
                if concern in seen_inbox_concerns:
                    continue
                seen_inbox_concerns.add(concern)
            compact_events.append(event)
        events = compact_events

        meeting_items_by_id = {item["id"]: item for item in meeting_items}
        source_paths = {
            row["id"]: row.get("path")
            for row in self.store._rows(
                "SELECT id, path FROM sources WHERE workspace_id=?",
                (workspace_id,),
            )
        }
        for event in events:
            entity_id = event["key"].split(":", 1)[-1]
            source_id = event.get("source_ref")
            source_path = source_paths.get(str(source_id)) if source_id else None
            if event["kind"].startswith("meeting_"):
                meeting_item = meeting_items_by_id.get(entity_id)
                if meeting_item:
                    source_id = meeting_item.get("source_id")
                    source_path = meeting_item.get("source_path")
            event.update(
                {
                    "entity_id": entity_id,
                    "source_id": source_id or "",
                    "source_path": source_path,
                    "action_label": (
                        "Открыть источник"
                        if source_path
                        else "Проверить"
                    ),
                }
            )
        return events

    def _backfill_meeting_sources(self) -> None:
        sources = self.store._rows(
            """
            SELECT s.id, s.workspace_id, s.title, s.created_at
            FROM sources s
            LEFT JOIN meetings m ON m.source_id=s.id
            WHERE s.kind='meeting' AND m.id IS NULL
            ORDER BY s.created_at
            """
        )
        for source in sources:
            try:
                self.store.analyze_meeting(
                    source["id"],
                    title=Path(source["title"]).stem,
                    occurred_at=source["created_at"],
                )
            except BaseException as exc:
                self.store.upsert_inbox_event(
                    f"meeting-analysis-error:{source['id']}",
                    f"Не удалось разобрать встречу: {source['title']}",
                    str(exc),
                    3,
                    "error",
                    source["workspace_id"],
                    source["id"],
                )

    def _register_meeting_event(self, source: dict[str, Any]) -> None:
        self.store.upsert_inbox_event(
            f"meeting-analyzed:{source['id']}",
            f"Встреча проанализирована: {source['title']}",
            "Темы, решения, поручения, обязательства, риски и вопросы доступны в разделе «Встречи».",
            1,
            "meeting",
            source["workspace_id"],
            source.get("meeting_id") or source["id"],
        )

    def _sync_meeting_attention(self) -> None:
        workspace_id = self.current_workspace_id
        items = self.store.list_meeting_items(workspace_id)
        events = AttentionEngine().rank(
            meeting_items=items,
            proactivity="proactive",
            workspace_id=workspace_id,
        )
        active_keys: set[str] = set()
        for event in events:
            if not event["kind"].startswith("meeting_") or event["score"] < 55:
                continue
            active_keys.add(event["key"])
            priority = 3 if event["severity"] == "critical" else 2 if event["severity"] == "high" else 1
            detail = event["reason"]
            if event.get("owner"):
                detail += f" Исполнитель: {event['owner']}."
            self.store.upsert_inbox_event(
                event["key"],
                event["title"],
                detail,
                priority,
                "meeting_attention",
                workspace_id,
                event["key"],
            )
        for row in self.store._rows(
            """
            SELECT id, source_ref FROM inbox
            WHERE workspace_id=? AND kind='meeting_attention' AND status='new'
            """,
            (workspace_id,),
        ):
            if row["source_ref"] not in active_keys:
                self.store.set_inbox_status(row["id"], "read")

    @staticmethod
    def _meeting_diff_rows(comparison: dict[str, Any]) -> list[dict[str, Any]]:
        labels = {
            "topic": "Тема",
            "decision": "Решение",
            "action": "Поручение",
            "commitment": "Обязательство",
            "risk": "Риск",
            "question": "Вопрос",
        }
        rows: list[dict[str, Any]] = []
        for status, title_prefix in (("added", "Добавлено"), ("removed", "Убрано")):
            for item in comparison[status]:
                rows.append(
                    {
                        "id": f"{status}:{item['id']}",
                        "title": f"{title_prefix}: {labels.get(item['kind'], item['kind'])}",
                        "kind": item["kind"],
                        "detail": item["text"],
                        "status": status,
                    }
                )
        for change in comparison["changed"]:
            before = change["before"]
            after = change["after"]
            detail = f"Было: {before['text']}\nСтало: {after['text']}"
            if before.get("due_at") != after.get("due_at"):
                detail += f"\nСрок: {before.get('due_at') or 'не указан'} → {after.get('due_at') or 'не указан'}"
            rows.append(
                {
                    "id": f"changed:{before['id']}:{after['id']}",
                    "title": f"Изменено: {labels.get(after['kind'], after['kind'])}",
                    "kind": after["kind"],
                    "detail": detail,
                    "status": "changed",
                }
            )
        return rows

    def _register_meeting_changes(self, comparison: dict[str, Any]) -> None:
        changed_items: list[tuple[str, dict[str, Any], str]] = []
        for change in comparison["changed"]:
            before, after = change["before"], change["after"]
            if after["kind"] == "decision":
                changed_items.append(("Решение встречи изменилось", after, before["text"]))
            if before.get("due_at") != after.get("due_at"):
                changed_items.append(("Срок поручения изменился", after, before.get("due_at") or "без срока"))
        for item in comparison["added"]:
            if item["kind"] == "decision":
                changed_items.append(("Появилось новое решение встречи", item, ""))
        for title, item, previous in changed_items:
            detail = item["text"]
            if previous:
                detail += f" Предыдущее значение: {previous}."
            self.store.upsert_inbox_event(
                f"meeting-change:{item['id']}",
                title,
                detail,
                2,
                "meeting_change",
                self.current_workspace_id,
                f"meeting-change:{item['id']}",
            )

    def _render_meeting_briefing(
        self,
        meeting: dict[str, Any],
        data: dict[str, Any],
    ) -> str:
        lines = [f"Брифинг к следующей встрече по «{meeting['title']}».\n"]

        def section(title: str, items: list[dict[str, Any]], limit: int = 6) -> None:
            lines.append(title)
            if not items:
                lines.append("— Нет данных.")
                return
            for item in items[-limit:]:
                owner = f" — {item['owner']}" if item.get("owner") else ""
                due = f", срок {item['due_at']}" if item.get("due_at") else ""
                lines.append(f"— {item['text']}{owner}{due}")

        section("Последние решения", data["recent_decisions"])
        section("Незакрытые поручения и обязательства", data["open_actions"])
        section("Риски", data["risks"])
        section("Открытые вопросы", data["questions"])

        try:
            synapse_context = self.orchestrator.synapse_meeting_context(
                str(meeting["source_id"])
            )
        except (KeyError, OSError, ValueError):
            synapse_context = None
        if synapse_context:
            supporting = synapse_context["supporting_context"]
            supporting_items = [
                item
                for item in [
                    supporting.get("description"),
                    *supporting.get("attachments", []),
                ]
                if item
            ]
            if supporting_items:
                lines.append(
                    "Дополнительный контекст eXpress (Синапс) "
                    "— не смешан с фактами транскрипта"
                )
                for item in supporting_items:
                    provenance = item["provenance"]
                    relative_path = provenance["part"].get("relative_path", "")
                    lines.append(
                        f"— {item['title']} "
                        f"[источник {provenance['source_id']}; {relative_path}]: "
                        f"{item['snippet']}"
                    )

        topics = list(dict.fromkeys(item.get("topic") for item in meeting["items"] if item.get("topic")))
        if topics:
            lines.append("История тем")
            for topic in topics[:5]:
                timeline = self.store.topic_timeline(
                    meeting["workspace_id"], topic, limit=4
                )
                lines.append(f"— {topic}: {len(timeline)} связанных упоминаний")
                for item in timeline[-2:]:
                    date = (item.get("occurred_at") or item["created_at"])[:10]
                    lines.append(f"  {date}: {item['text']}")
        return "\n".join(lines)

    def emit_snapshot(self) -> None:
        snapshot = self.store.snapshot(
            workspace_id=self.current_workspace_id,
            task_id=self.current_task_id,
            meeting_id=self.current_meeting_id,
        )
        self.current_task_id = snapshot["current_task_id"]
        if snapshot["current_meeting_id"] is None and snapshot["meetings"]:
            self.current_meeting_id = snapshot["meetings"][0]["id"]
            snapshot = self.store.snapshot(
                workspace_id=self.current_workspace_id,
                task_id=self.current_task_id,
                meeting_id=self.current_meeting_id,
            )
        else:
            self.current_meeting_id = snapshot["current_meeting_id"]
        attention = self._rank_attention()
        snapshot["attention_events"] = attention
        snapshot["today"]["attention"] = len(attention)
        snapshot["meeting_diff"] = self._meeting_diff
        snapshot["meeting_briefing"] = self._meeting_briefing or ""
        snapshot["quick_actions"] = self.orchestrator.quick_actions(
            self.current_task_id
        )
        runtime = self._llm_runtime()
        snapshot["settings"]["llm_mode"] = snapshot["settings"].get(
            "model_mode", "local"
        )
        snapshot["settings"]["external_llm_base_url"] = snapshot[
            "settings"
        ].get("llm_base_url", "")
        snapshot["settings"]["external_llm_model"] = snapshot["settings"].get(
            "llm_model", ""
        )
        snapshot["llm"] = runtime
        snapshot["model"] = (
            f"{runtime['model']} · "
            + (
                "локальный API"
                if runtime.get("provider_type") == "local"
                else "корпоративный API"
                if runtime.get("provider_type") == "corporate"
                else "внешний API"
            )
            if runtime["mode"] == "external"
            else f"{runtime['model']} · local MLX"
        )
        self.emitter.emit("snapshot", data=snapshot)

    def _resolve_approval(self, approval_id: str, status: str) -> None:
        rows = self.store._rows("SELECT * FROM approvals WHERE id = ?", (approval_id,))
        if not rows:
            raise KeyError(approval_id)
        approval = rows[0]
        payload = json.loads(approval["payload"] or "{}")
        if not isinstance(payload, dict):
            raise ValueError("Сохранённые параметры действия повреждены")

        if status == "rejected":
            resolved = self.store.resolve_approval(
                approval_id,
                "rejected",
                actor="local-user",
                origin="approval_center",
            )
            self.store.cancel_approval_dependents(
                approval_id,
                actor="system",
                origin="workflow",
            )
            if resolved.get("task_id"):
                self.store.add_task_event(
                    resolved["task_id"],
                    "approval_rejected",
                    "Внешнее действие отклонено",
                    f"Шаг {resolved['step_index']} · {resolved['action_type']}",
                )
            self._sync_task_after_approvals(resolved.get("task_id"))
            self.emitter.emit(
                "approval_resolved",
                id=approval_id,
                status="rejected",
            )
            self.emit_snapshot()
            return

        if status != "approved":
            raise ValueError("Статус должен быть approved или rejected")

        approved = self.store.resolve_approval(
            approval_id,
            "approved",
            actor="local-user",
            origin="approval_center",
        )
        capability = str(payload.get("capability") or approved["action_type"])
        result_code = "executor_not_connected"
        result = (
            f"{capability} не подключён. Действие не выполнено; "
            "измените план или подключите исполнитель и повторите."
        )
        try:
            self.store.begin_approval_execution(
                approval_id,
                actor="system",
                origin="local_action_router",
            )
            if payload.get("connected") is True:
                # A payload flag is not an executor.  Until a real callable is
                # registered, treating it as success would create a false
                # corporate side effect.
                result_code = "executor_unavailable"
                result = (
                    f"Для {capability} не зарегистрирован исполнитель. "
                    "Действие не выполнено."
                )
        except ValueError as exc:
            result_code = "workflow_predecessor_blocked"
            result = str(exc)
        failed = self.store.complete_approval_execution(
            approval_id,
            success=False,
            result_code=result_code,
            result=result,
            actor="system",
            origin="local_action_router",
        )
        if failed.get("task_id"):
            task = self.store.get_task(failed["task_id"])
            self.store.add_task_event(
                failed["task_id"],
                "approval_error",
                "Внешнее действие не выполнено",
                f"Шаг {failed['step_index']} · {failed['action_type']}: {result}",
            )
            self.store.upsert_inbox_event(
                f"approval-error:{approval_id}",
                f"Не выполнено: {capability}",
                result,
                3,
                "error",
                task["workspace_id"],
                approval_id,
            )
        self._sync_task_after_approvals(failed.get("task_id"))
        self.emitter.emit(
            "approval_execution_failed",
            id=approval_id,
            status="error",
            result=result,
        )
        self.emitter.emit("error", message=result)
        self.emit_snapshot()

    def _sync_task_after_approvals(self, task_id: str | None) -> None:
        if not task_id:
            return
        rows = self.store._rows(
            "SELECT status FROM approvals WHERE task_id=?",
            (task_id,),
        )
        if not rows:
            return
        statuses = {str(item["status"]) for item in rows}
        if statuses & {"pending", "approved", "executing", "error"}:
            self.store.update_task(task_id, status="needs_user")
            return
        self.store.update_task(task_id, status="done")
        self.store.add_task_event(
            task_id,
            "approval_resolved",
            "План внешних действий завершён",
            ", ".join(sorted(statuses)),
        )

    def _set_current_turn(self, cancel_event: threading.Event) -> None:
        with self._turn_lock:
            self._current_turn_cancel = cancel_event

    def _clear_current_turn(self, cancel_event: threading.Event) -> None:
        with self._turn_lock:
            if self._current_turn_cancel is cancel_event:
                self._current_turn_cancel = None

    def _automation_loop(self) -> None:
        while not self.shutdown_event.wait(10):
            if self.task_lock.locked():
                continue
            for automation in self.store.due_automations():
                if self.shutdown_event.is_set():
                    return
                self._run_automation(automation)

    def _run_event_automations(
        self,
        automations: list[dict[str, Any]],
        source: dict[str, Any],
    ) -> None:
        for automation in automations:
            if self.shutdown_event.is_set():
                return
            enriched = {
                **automation,
                "prompt": (
                    f"{automation['prompt']}\n\n"
                    f"Событие: в рабочее пространство добавлен новый источник «{source['title']}»."
                ),
            }
            self._run_automation(enriched)

    def _run_automation(self, automation: dict[str, Any]) -> None:
        if not self.task_lock.acquire(blocking=False):
            return
        task_id = None
        try:
            digest_period = self.orchestrator.digest_command(automation["prompt"])
            if digest_period is not None:
                workspace_id = str(
                    automation.get("workspace_id") or self.current_workspace_id
                )
                digest = self.orchestrator.persist_digest(
                    workspace_id,
                    digest_period,
                    request_text=automation["prompt"],
                )
                task_id = str(digest["task"]["id"])
                self.emitter.emit(
                    "automation_started",
                    id=automation["id"],
                    name=automation["name"],
                    task_id=task_id,
                )
                self.store.mark_automation_run(
                    automation["id"], automation["schedule"]
                )
                self.emitter.emit(
                    "automation_completed",
                    id=automation["id"],
                    task_id=task_id,
                    deterministic=True,
                    local_event="digest",
                )
                self.emit_snapshot()
                return
            turn = self.orchestrator.prepare_turn(
                automation["prompt"],
                workspace_id=automation["workspace_id"],
                task_id=None,
                model_mode_override="local",
            )
            task_id = turn.task_id
            self.emitter.emit(
                "automation_started",
                id=automation["id"],
                name=automation["name"],
                task_id=turn.task_id,
            )
            reply = self.assistant.answer(
                turn.prompt,
                chat_history=turn.history,
                remember=False,
                speak=False,
                echo=False,
                chat_backend=self._local_chat,
            )
            artifact = self.orchestrator.finish_turn(turn, reply)
            self.store.mark_automation_run(automation["id"], automation["schedule"])
            self.store.add_inbox(
                automation["workspace_id"],
                "automation",
                f"Автоматизация завершена: {automation['name']}",
                reply[:500],
                priority=2,
                source_ref=artifact["id"] if artifact else turn.task_id,
            )
            self.emitter.emit("automation_completed", id=automation["id"], task_id=turn.task_id)
            self.emit_snapshot()
        except BaseException as exc:
            if task_id:
                self.orchestrator.fail_turn(task_id, str(exc))
            self.store.mark_automation_run(automation["id"], automation["schedule"])
            self.store.add_inbox(
                automation["workspace_id"],
                "error",
                f"Ошибка автоматизации: {automation['name']}",
                str(exc),
                priority=3,
            )
            self.emitter.emit("error", message=f"Автоматизация «{automation['name']}»: {exc}")
        finally:
            self.task_lock.release()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="JSON backend for the macOS voice assistant UI")
    parser.add_argument("--config", type=Path, default=Path("config.toml"))
    parser.add_argument("--data", type=Path, default=Path("data/assistant.sqlite3"))
    return parser


def main() -> None:
    args = build_parser().parse_args()
    emitter = EventEmitter()
    backend: UIBackend | None = None
    try:
        backend = UIBackend(Config.load(args.config), emitter, AssistantStore(args.data))
        backend.load()
        for line in sys.stdin:
            if backend.shutdown_event.is_set():
                break
            try:
                command = json.loads(line)
                if not isinstance(command, dict):
                    raise ValueError("Команда должна быть JSON-объектом")
                backend.handle(command)
            except (json.JSONDecodeError, ValueError) as exc:
                emitter.emit("error", message=f"Некорректная команда: {exc}")
            except Exception as exc:
                emitter.emit("error", message=str(exc))
            if backend.shutdown_event.is_set():
                break
    except BaseException as exc:
        emitter.emit("fatal", message=str(exc))
        raise SystemExit(1) from exc
    finally:
        if backend is not None:
            backend.assistant.close()


if __name__ == "__main__":
    main()
