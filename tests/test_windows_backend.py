from __future__ import annotations

import base64
import json
import socket
import threading
import time

import pytest

from voice_assistant.express_intake import CONNECTOR_ID
from voice_assistant.integrations import IntegrationIntent, IntegrationRequest
from voice_assistant.java_core import (
    JavaActionClaim,
    JavaActionCompletion,
    JavaActionExecution,
    JavaActionInspection,
    JavaRouteDecision,
)
from voice_assistant.store import AssistantStore
from voice_assistant.windows_backend import (
    EventEmitter,
    OpenAIChatClient,
    OmniVoiceLoopbackClient,
    PortableWindowsVoiceRuntime,
    WindowsPilotBackend,
    _abort_http_connection,
    normalize_loopback_service_url,
    normalize_openai_base_url,
    openai_url_is_loopback,
)


class CapturingEmitter:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []

    def emit(self, event_type: str, **payload: object) -> None:
        self.events.append({"type": event_type, **payload})


class FakeExpressIntake:
    def __init__(self) -> None:
        self.connected = False
        self.cursors: list[str | None] = []

    def diagnostics(self):  # noqa: ANN201
        return {
            "connector_id": CONNECTOR_ID,
            "configured": True,
            "connected": self.connected,
            "read_only": True,
            "write_back_available": False,
            "delivery_mode": "POLLING",
            "last_success_at": "2026-08-31T10:00:00Z" if self.connected else None,
            "last_error_code": None,
            "reason_code": (
                "CORPORATE_READ_ONLY_CONNECTED"
                if self.connected
                else "CORPORATE_INTAKE_NOT_VERIFIED"
            ),
        }

    def sync(self, *, workspace_id, cursor=None):  # noqa: ANN001, ANN201
        assert workspace_id
        self.cursors.append(cursor)
        self.connected = True
        return {
            "status": "succeeded",
            "connector": self.diagnostics(),
            "imported": [],
            "processed": 0,
            "added": 0,
            "deduplicated": 0,
            "next_cursor": "cursor-win-1",
            "watermark": "2026-08-31T10:00:00Z",
            "has_more": False,
        }

    def sync_until_idle(  # noqa: ANN201
        self, *, workspace_id, cursor, commit_checkpoint, max_pages=10  # noqa: ANN001
    ):
        del max_pages
        result = self.sync(workspace_id=workspace_id, cursor=cursor)
        commit_checkpoint(result["next_cursor"], result["watermark"])
        result["pages"] = 1
        return result


def test_windows_jsonl_events_are_console_encoding_independent(capsys) -> None:
    EventEmitter().emit("status", message="Готово №1")

    wire = capsys.readouterr().out
    wire.encode("ascii")
    assert json.loads(wire) == {"type": "status", "message": "Готово №1"}


class FakeChat:
    def __init__(self, base_url: str, model: str, api_key: str) -> None:
        self.base_url = base_url
        self.model = model
        self.api_key_seen = api_key
        self.ready = True
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True

    def stream_reply(self, prompt, *, history, system_prompt, cancel_event):  # noqa: ANN001
        assert "Рабочее пространство" in prompt
        assert isinstance(history, list)
        assert "Windows pilot" in system_prompt
        assert not cancel_event.is_set()
        yield "Готовый "
        yield "ответ"


class InterruptedChat(FakeChat):
    def stream_reply(self, prompt, *, history, system_prompt, cancel_event):  # noqa: ANN001
        del prompt, history, system_prompt
        yield "Частичный ответ"
        cancel_event.set()
        raise OSError("closed connection")


class VoiceChat(FakeChat):
    def stream_reply(self, prompt, *, history, system_prompt, cancel_event):  # noqa: ANN001
        del prompt, history, system_prompt
        assert not cancel_event.is_set()
        yield "Короткий голосовой ответ. "
        yield "В чате остаётся полное подробное продолжение."


class FakeVoiceRuntime:
    def __init__(self, *, ready: bool = True) -> None:
        self.configured = ready
        self.ready = ready
        self.cancelled = 0
        self.transcribed_audio = b""
        self.transcribed_file = None
        self.spoken_texts: list[str] = []

    def load(self) -> None:
        return

    def diagnostics(self):  # noqa: ANN201
        return {
            "state": "ready" if self.ready else "unconfigured",
            "stt": {"ready": self.ready, "detail": "STT готов" if self.ready else "STT не настроен"},
            "tts": {"ready": self.ready, "detail": "TTS готов" if self.ready else "TTS не настроен"},
            "capture": {"ready": None, "detail": "Проверяется в Electron"},
        }

    def transcribe_pcm16(self, audio, sample_rate, cancel_event):  # noqa: ANN001, ANN201
        assert sample_rate == 16_000
        assert not cancel_event.is_set()
        self.transcribed_audio = audio
        return "Подготовь ответ голосом"

    def transcribe_file(self, path, cancel_event):  # noqa: ANN001, ANN201
        assert not cancel_event.is_set()
        self.transcribed_file = path
        return (
            "[00:01] Анна: Решили запустить пилот.\n"
            "[00:14] Иван: Подготовлю список участников до пятницы."
        )

    def synthesize(self, text, cancel_event):  # noqa: ANN001, ANN201
        assert not cancel_event.is_set()
        self.spoken_texts.append(text)
        yield b"\x00\x01" * 400, 24_000
        yield b"\x02\x03" * 200, 24_000

    def cancel(self) -> None:
        self.cancelled += 1

    def close(self) -> None:
        return


class FakeCorePolicy:
    def __init__(
        self,
        *,
        ready: bool = True,
        decision: JavaRouteDecision | None = None,
    ) -> None:
        self.configured = True
        self.ready = ready
        self.decision = decision
        self.calls: list[dict[str, object]] = []
        self.closed = False

    def start(self) -> bool:
        return self.ready

    def diagnostics(self):  # noqa: ANN201
        return {
            "configured": self.configured,
            "ready": self.ready,
            "protocol_version": "1.0" if self.ready else None,
            "policy": "java21" if self.ready else "python_fallback",
        }

    def decide_route(self, **kwargs):  # noqa: ANN003, ANN201
        self.calls.append(dict(kwargs))
        if self.decision is not None:
            return self.decision
        route = str(kwargs["preference"]).upper()
        return JavaRouteDecision(
            status="SELECTED",
            route=route,
            reason=f"{route}_SELECTED",
            local_fallback_before_first_output=False,
        )

    def close(self) -> None:
        self.closed = True


class FakeActionCorePolicy(FakeCorePolicy):
    def __init__(self) -> None:
        super().__init__()
        self.action_calls: list[dict[str, object]] = []
        self.result: JavaActionExecution | None = None

    def inspect_action(self, **kwargs):  # noqa: ANN003, ANN201
        self.action_calls.append({"method": "inspect", **kwargs})
        return JavaActionInspection(
            "COMPLETED" if self.result else "NOT_FOUND",
            None,
            self.result,
        )

    def claim_action(self, **kwargs):  # noqa: ANN003, ANN201
        self.action_calls.append({"method": "claim", **kwargs})
        if self.result is not None:
            return JavaActionClaim("REPLAY", None, self.result)
        return JavaActionClaim(
            "CLAIMED",
            "00000000-0000-4000-8000-000000000001",
            None,
        )

    def complete_action(self, **kwargs):  # noqa: ANN003, ANN201
        self.action_calls.append({"method": "complete", **kwargs})
        self.result = JavaActionExecution(
            outcome=str(kwargs["outcome"]),
            result_code=str(kwargs["result_code"]),
            external_reference=None,
            completed_at="2026-08-31T18:00:00Z",
        )
        return JavaActionCompletion("RECORDED", self.result)


class DictationOnlyVoiceRuntime(FakeVoiceRuntime):
    def __init__(self) -> None:
        super().__init__(ready=False)
        self.configured = False
        self.stt_configured = True
        self.stt_ready = True

    def diagnostics(self):  # noqa: ANN201
        return {
            "state": "partial",
            "stt": {"ready": True, "detail": "Faster-Whisper готов"},
            "tts": {"ready": False, "detail": "OmniVoice-Fast не настроен"},
            "capture": {"ready": None, "detail": "Проверяется в Electron"},
        }


def fake_factory(base_url: str, model: str, api_key: str) -> FakeChat:
    return FakeChat(base_url, model, api_key)


def interrupted_factory(base_url: str, model: str, api_key: str) -> InterruptedChat:
    return InterruptedChat(base_url, model, api_key)


@pytest.mark.parametrize(
    "value,expected",
    [
        ("http://127.0.0.1:11434/v1/chat/completions", "http://127.0.0.1:11434/v1"),
        ("http://localhost:1234/v1/", "http://localhost:1234/v1"),
        ("https://llm.corp.example/v1", "https://llm.corp.example/v1"),
    ],
)
def test_windows_endpoint_normalization(value: str, expected: str) -> None:
    assert normalize_openai_base_url(value) == expected


def test_windows_endpoint_rejects_remote_plaintext_and_embedded_credentials() -> None:
    with pytest.raises(ValueError, match="HTTPS"):
        normalize_openai_base_url("http://llm.corp.example/v1")
    with pytest.raises(ValueError, match="нельзя помещать"):
        normalize_openai_base_url("https://user:secret@llm.corp.example/v1")


def test_loopback_detection_supports_ipv4_ipv6_and_localhost() -> None:
    assert openai_url_is_loopback("http://localhost:11434/v1")
    assert openai_url_is_loopback("http://127.0.0.1:11434/v1")
    assert openai_url_is_loopback("http://[::1]:11434/v1")
    assert not openai_url_is_loopback("https://llm.corp.example/v1")


def test_local_endpoint_configuration_is_persisted_without_a_secret(tmp_path) -> None:
    emitter = CapturingEmitter()
    backend = WindowsPilotBackend(
        tmp_path / "assistant.sqlite3",
        emitter,
        chat_factory=fake_factory,
    )

    backend.configure_llm(
        {
            "base_url": "http://127.0.0.1:11434/v1",
            "model": "qwen3:4b",
            "provider_type": "local",
            "api_key": "",
        }
    )

    settings = backend.store.settings()
    assert settings["llm_base_url"] == "http://127.0.0.1:11434/v1"
    assert settings["llm_model"] == "qwen3:4b"
    assert settings["external_provider_type"] == "local"
    assert not any("key" in key.casefold() for key in settings)
    snapshot = next(event for event in reversed(emitter.events) if event["type"] == "snapshot")
    assert snapshot["data"]["platform"]["voice_available"] is False
    assert snapshot["data"]["llm"]["actual_route"] == "local_api"


def test_corporate_key_is_memory_only_and_must_be_reentered_after_restart(tmp_path) -> None:
    data_path = tmp_path / "assistant.sqlite3"
    emitter = CapturingEmitter()
    backend = WindowsPilotBackend(data_path, emitter)
    backend.configure_llm(
        {
            "base_url": "https://llm.corp.example/v1",
            "model": "corp-chat",
            "provider_type": "corporate",
            "api_key": "super-secret-value",
        }
    )

    serialized_events = json.dumps(emitter.events, ensure_ascii=False)
    settings = backend.store.settings()
    assert "super-secret-value" not in serialized_events
    assert "super-secret-value" not in json.dumps(settings)
    assert backend._runtime()["ready"] is True

    restarted = WindowsPilotBackend(data_path, CapturingEmitter())
    assert restarted._runtime()["ready"] is False
    assert "заново" in restarted._runtime()["detail"]


def test_provider_type_must_match_endpoint_trust_boundary(tmp_path) -> None:
    backend = WindowsPilotBackend(
        tmp_path / "assistant.sqlite3",
        CapturingEmitter(),
        chat_factory=fake_factory,
    )
    with pytest.raises(ValueError, match="loopback"):
        backend.configure_llm(
            {
                "base_url": "https://llm.corp.example/v1",
                "model": "corp-chat",
                "provider_type": "local",
            }
        )
    with pytest.raises(ValueError, match="loopback"):
        backend.configure_llm(
            {
                "base_url": "http://localhost:11434/v1",
                "model": "qwen",
                "provider_type": "corporate",
            }
        )


def test_text_turn_uses_shared_store_and_json_event_contract(tmp_path) -> None:
    emitter = CapturingEmitter()
    backend = WindowsPilotBackend(
        tmp_path / "assistant.sqlite3",
        emitter,
        chat_factory=fake_factory,
    )
    backend.configure_llm(
        {
            "base_url": "http://localhost:11434/v1",
            "model": "qwen3:4b",
            "provider_type": "local",
        }
    )

    backend._run_text_turn("Подготовь короткий ответ", threading.Event())

    event_types = [event["type"] for event in emitter.events]
    assert "user" in event_types
    assert "assistant_start" in event_types
    assert event_types.count("assistant_delta") == 2
    answer = next(event for event in emitter.events if event["type"] == "assistant_end")
    assert answer["text"] == "Готовый ответ"
    assert answer["spoken"] is False
    assert answer["llm_route"]["actual_route"] == "local_api"
    task = backend.store.get_task(backend.current_task_id)
    assert task["status"] == "done"
    messages = backend.store.messages(task["id"])
    metadata = json.loads(messages[-1]["metadata"])
    assert messages[-1]["content"] == "Готовый ответ"
    assert metadata["llm_route"]["provider_type"] == "local"
    assert metadata["spoken"] is False
    usage = backend.store.pilot_usage_summary(platform="windows")
    assert usage["text_turns"] == 1
    assert usage["voice_turns"] == 0


def test_windows_pilot_feedback_command_is_bounded_and_content_free(tmp_path) -> None:
    emitter = CapturingEmitter()
    backend = WindowsPilotBackend(tmp_path / "assistant.sqlite3", emitter)

    backend.handle({"command": "set_pilot_feedback", "usefulness_rating": 5})

    summary = backend.store.pilot_usage_summary(platform="windows")
    assert summary["usefulness_rating"] == 5
    assert summary["criteria"]["rating_at_least_4"] is True
    assert any(event["type"] == "pilot_feedback_saved" for event in emitter.events)
    with pytest.raises(ValueError, match="от 1 до 5"):
        backend.handle({"command": "set_pilot_feedback", "usefulness_rating": 0})


def test_windows_text_turn_is_gated_by_java_core_before_llm(tmp_path) -> None:
    emitter = CapturingEmitter()
    core = FakeCorePolicy()
    backend = WindowsPilotBackend(
        tmp_path / "assistant.sqlite3",
        emitter,
        chat_factory=fake_factory,
        core_policy=core,
    )
    backend.configure_llm(
        {
            "base_url": "http://127.0.0.1:11434/v1",
            "model": "qwen3:4b",
            "provider_type": "local",
        }
    )

    backend._run_text_turn("Проверь маршрут", threading.Event())

    assert core.calls == [
        {
            "classification": "internal",
            "preference": "LOCAL",
            "local_available": True,
            "corporate_available": False,
            "external_available": False,
            "corporate_scope_authorized": False,
            "explicit_external_consent": False,
        }
    ]
    answer = next(event for event in emitter.events if event["type"] == "assistant_end")
    assert answer["llm_route"]["policy_engine"] == "java21"
    assert answer["llm_route"]["java_core_reason"] == "LOCAL_SELECTED"


def test_java_core_route_disagreement_blocks_api_call_fail_closed(tmp_path) -> None:
    emitter = CapturingEmitter()
    core = FakeCorePolicy(
        decision=JavaRouteDecision(
            status="BLOCKED",
            route=None,
            reason="CLASSIFICATION_BLOCKS_CORPORATE",
            local_fallback_before_first_output=False,
        )
    )
    backend = WindowsPilotBackend(
        tmp_path / "assistant.sqlite3",
        emitter,
        chat_factory=fake_factory,
        core_policy=core,
    )
    backend.configure_llm(
        {
            "base_url": "http://127.0.0.1:11434/v1",
            "model": "qwen3:4b",
            "provider_type": "local",
        }
    )

    backend._run_text_turn("Не отправляй запрос", threading.Event())

    assert any(event["type"] == "routing_blocked" for event in emitter.events)
    assert not any(event["type"] == "assistant_start" for event in emitter.events)
    assert backend.store.get_task(backend.current_task_id)["status"] == "needs_user"


def test_unavailable_java_core_uses_visible_python_policy_fallback(tmp_path) -> None:
    emitter = CapturingEmitter()
    core = FakeCorePolicy(ready=False)
    backend = WindowsPilotBackend(
        tmp_path / "assistant.sqlite3",
        emitter,
        chat_factory=fake_factory,
        core_policy=core,
    )
    backend.configure_llm(
        {
            "base_url": "http://127.0.0.1:11434/v1",
            "model": "qwen3:4b",
            "provider_type": "local",
        }
    )

    backend._run_text_turn("Используй fallback", threading.Event())

    answer = next(event for event in emitter.events if event["type"] == "assistant_end")
    assert answer["llm_route"]["policy_engine"] == "python_fallback"
    assert answer["llm_route"]["java_core_ready"] is False
    snapshot = next(event for event in reversed(emitter.events) if event["type"] == "snapshot")
    assert snapshot["data"]["platform"]["java_core_policy"]["ready"] is False


def test_core_policy_probe_contains_only_safe_contract_metadata(tmp_path) -> None:
    emitter = CapturingEmitter()
    backend = WindowsPilotBackend(
        tmp_path / "assistant.sqlite3",
        emitter,
        core_policy=FakeCorePolicy(),
    )

    backend.handle({"command": "core_policy_probe"})

    diagnostic = emitter.events[-1]
    assert diagnostic == {
        "type": "diagnostic",
        "component": "java_core",
        "check": "route_policy",
        "measured": True,
        "configured": True,
        "ready": True,
        "protocol_version": "1.0",
        "status": "SELECTED",
        "route": "LOCAL",
        "reason": "LOCAL_SELECTED",
    }


def test_core_action_journal_probe_exercises_replay_without_user_content(
    tmp_path,
) -> None:  # noqa: ANN001
    emitter = CapturingEmitter()
    core = FakeActionCorePolicy()
    backend = WindowsPilotBackend(
        tmp_path / "assistant.sqlite3",
        emitter,
        core_policy=core,
    )

    backend.handle({"command": "core_action_journal_probe"})

    diagnostic = emitter.events[-1]
    assert diagnostic["check"] == "action_journal"
    assert diagnostic["ready"] is True
    assert diagnostic["initial_state"] == "NOT_FOUND"
    assert diagnostic["claim"] == "CLAIMED"
    assert diagnostic["completion"] == "RECORDED"
    assert diagnostic["replay"] == "REPLAY"
    assert diagnostic["result_code"] == "PROBE.SUCCESS"
    assert diagnostic["content_transmitted"] is False
    serialized = json.dumps(core.action_calls, ensure_ascii=False)
    assert "prompt" not in serialized.casefold()
    assert "transcript" not in serialized.casefold()


def test_windows_approval_command_is_visible_and_never_claims_disconnected_success(
    tmp_path,
) -> None:  # noqa: ANN001
    emitter = CapturingEmitter()
    backend = WindowsPilotBackend(
        tmp_path / "assistant.sqlite3",
        emitter,
        core_policy=FakeCorePolicy(),
    )
    task = backend.store.create_task(
        backend.current_workspace_id,
        "Создать задачу во внешней системе",
    )
    approval = backend.integration_hub.stage(
        IntegrationRequest(
            "jira",
            "issue.create",
            IntegrationIntent.WRITE,
            {"summary": "Проверить пилот"},
        ),
        task_id=task["id"],
    )

    backend.handle(
        {
            "command": "resolve_approval",
            "id": approval["id"],
            "status": "approved",
        }
    )

    row = backend.store._rows(
        "SELECT status, result FROM approvals WHERE id=?",
        (approval["id"],),
    )[0]
    assert row["status"] == "error"
    assert "не подключена" in row["result"]
    assert any(event["type"] == "approval_execution_failed" for event in emitter.events)
    snapshot = next(event for event in reversed(emitter.events) if event["type"] == "snapshot")
    visible = {item["id"]: item for item in snapshot["data"]["approvals"]}
    assert visible[approval["id"]]["status"] == "error"
    assert backend.store.get_task(task["id"])["status"] == "needs_user"


def test_voice_command_reports_actionable_diagnostics_when_runtime_is_absent(tmp_path) -> None:
    emitter = CapturingEmitter()
    backend = WindowsPilotBackend(
        tmp_path / "assistant.sqlite3",
        emitter,
        chat_factory=fake_factory,
    )

    backend.handle({"command": "start"})

    assert [event["type"] for event in emitter.events] == [
        "capability_unavailable",
        "session_stopped",
    ]
    unavailable = emitter.events[0]
    assert unavailable["capability"] == "voice"
    assert unavailable["diagnostics"]["state"] == "unconfigured"
    assert "WHISPER_MODEL" in unavailable["diagnostics"]["stt"]["detail"]
    assert "OMNIVOICE_URL" in unavailable["diagnostics"]["tts"]["detail"]


def test_windows_snapshot_overrides_macos_voice_capability_claims(tmp_path) -> None:
    emitter = CapturingEmitter()
    backend = WindowsPilotBackend(tmp_path / "assistant.sqlite3", emitter)
    backend.emit_snapshot()
    snapshot = emitter.events[-1]["data"]
    capabilities = {item["id"]: item for item in snapshot["capabilities"]}
    assert capabilities["dictation"]["status"] == "not_connected"
    assert capabilities["voice"]["status"] == "not_connected"
    assert "WHISPER_MODEL" in capabilities["dictation"]["description"]
    assert "OMNIVOICE_URL" in capabilities["voice"]["description"]
    assert snapshot["model"].endswith("не настроено")


def test_cancelled_stream_is_preserved_as_interrupted_not_failed(tmp_path) -> None:
    emitter = CapturingEmitter()
    backend = WindowsPilotBackend(
        tmp_path / "assistant.sqlite3",
        emitter,
        chat_factory=interrupted_factory,
    )
    backend.configure_llm(
        {
            "base_url": "http://localhost:11434/v1",
            "model": "qwen3:4b",
            "provider_type": "local",
        }
    )

    backend._run_text_turn("Останови ответ", threading.Event())

    answer = next(event for event in emitter.events if event["type"] == "assistant_end")
    assert answer["interrupted"] is True
    assert answer["text"] == "Частичный ответ"
    assert backend.store.get_task(backend.current_task_id)["status"] == "needs_user"
    assert not any(event["type"] == "error" for event in emitter.events)


def test_real_chat_client_requires_key_only_for_remote_endpoint() -> None:
    assert OpenAIChatClient("http://127.0.0.1:11434/v1", "qwen").ready
    assert not OpenAIChatClient("https://llm.corp.example/v1", "corp").ready
    assert OpenAIChatClient(
        "https://llm.corp.example/v1", "corp", "memory-only-key"
    ).ready


def test_store_remains_readable_by_shared_assistant_store(tmp_path) -> None:
    data_path = tmp_path / "assistant.sqlite3"
    backend = WindowsPilotBackend(data_path, CapturingEmitter(), chat_factory=fake_factory)
    backend.handle({"command": "new_task", "title": "Windows pilot"})

    reopened = AssistantStore(data_path)
    assert reopened.get_task(backend.current_task_id)["title"] == "Windows pilot"


def test_synapse_package_import_runs_without_llm_and_emits_traceable_result(
    monkeypatch, tmp_path
) -> None:  # noqa: ANN001
    emitter = CapturingEmitter()
    backend = WindowsPilotBackend(tmp_path / "assistant.sqlite3", emitter)
    source = backend.store.add_source(
        backend.current_workspace_id,
        "meeting",
        "Нейтральная встреча",
        "Обсудили следующий этап пилота.",
    )
    captured: dict[str, object] = {}

    def fake_import(path, *, workspace_id):  # noqa: ANN001, ANN202
        captured["path"] = path
        captured["workspace_id"] = workspace_id
        return {
            "status": "imported",
            "source_id": source["id"],
            "meeting_id": "meeting-demo",
            "analysis": {"decisions": [], "actions": [], "risks": [], "questions": []},
            "provenance": {"primary_source": {"id": source["id"]}},
        }

    monkeypatch.setattr(
        backend.orchestrator,
        "import_synapse_meeting_package",
        fake_import,
    )
    package = tmp_path / "meeting.zip"
    backend.handle(
        {
            "command": "import_synapse_package",
            "path": str(package),
            "workspace_id": backend.current_workspace_id,
        }
    )

    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and not any(
        event["type"] == "synapse_package_imported" for event in emitter.events
    ):
        time.sleep(0.005)

    assert captured == {
        "path": package,
        "workspace_id": backend.current_workspace_id,
    }
    imported = next(
        event for event in emitter.events if event["type"] == "synapse_package_imported"
    )
    assert imported["result"]["source_id"] == source["id"]
    assert any(
        event["type"] == "state" and event.get("state") == "importing_meeting"
        for event in emitter.events
    )
    assert not any(event["type"] in {"user", "assistant_start"} for event in emitter.events)


def test_synapse_package_validation_error_has_dedicated_event(
    monkeypatch, tmp_path
) -> None:  # noqa: ANN001
    emitter = CapturingEmitter()
    backend = WindowsPilotBackend(tmp_path / "assistant.sqlite3", emitter)

    def reject(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        raise ValueError("manifest.json содержит некорректный JSON")

    monkeypatch.setattr(
        backend.orchestrator,
        "import_synapse_meeting_package",
        reject,
    )
    backend.handle({"command": "import_synapse_package", "path": "meeting.zip"})

    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and not any(
        event["type"] == "synapse_package_import_error" for event in emitter.events
    ):
        time.sleep(0.005)

    error = next(
        event for event in emitter.events if event["type"] == "synapse_package_import_error"
    )
    assert error["message"] == "manifest.json содержит некорректный JSON"
    assert error["retryable"] is True


def test_downloaded_express_transcript_imports_as_analyzed_meeting(tmp_path) -> None:
    emitter = CapturingEmitter()
    backend = WindowsPilotBackend(tmp_path / "assistant.sqlite3", emitter)
    transcript = tmp_path / "express-meeting.txt"
    transcript.write_text(
        "[00:01] Анна: Решили начать пилот.\n"
        "[00:12] Иван: Подготовлю список участников до пятницы.\n",
        encoding="utf-8",
    )

    backend.handle(
        {
            "command": "import_meeting_transcript",
            "path": str(transcript),
            "workspace_id": backend.current_workspace_id,
        }
    )

    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and not any(
        event["type"] == "meeting_transcript_imported" for event in emitter.events
    ):
        time.sleep(0.005)

    imported = next(
        event for event in emitter.events if event["type"] == "meeting_transcript_imported"
    )
    assert imported["source_system"] == "express"
    assert imported["import_mode"] == "LOCAL_TRANSCRIPT_IMPORT"
    assert imported["meeting_id"]
    assert backend.store.get_source(imported["source"]["id"])["kind"] == "meeting"
    assert backend.store.get_meeting(imported["meeting_id"], include_items=True)["items"]
    assert backend.current_meeting_id == imported["meeting_id"]
    assert "Брифинг к следующей встрече" in backend._meeting_briefing
    readiness = next(
        event for event in emitter.events if event["type"] == "meeting_briefing_ready"
    )
    assert readiness["meeting_id"] == imported["meeting_id"]
    assert readiness["scope_mode"] == "single_meeting"
    snapshot = [event for event in emitter.events if event["type"] == "snapshot"][-1][
        "data"
    ]
    assert snapshot["current_meeting_id"] == imported["meeting_id"]
    assert snapshot["meeting_items"]
    assert "Открытые вопросы" in snapshot["meeting_briefing"]
    assert not any(event["type"] in {"user", "assistant_start"} for event in emitter.events)


def test_windows_meeting_can_be_selected_briefed_and_deleted(tmp_path) -> None:
    emitter = CapturingEmitter()
    backend = WindowsPilotBackend(tmp_path / "assistant.sqlite3", emitter)
    source = backend.store.add_source(
        backend.current_workspace_id,
        "meeting",
        "Проектный комитет",
        "Анна: Решили продолжить пилот. Иван подготовит план до пятницы.",
    )
    meeting = backend.store.analyze_meeting(source["id"])

    backend.handle({"command": "select_meeting", "meeting_id": meeting["id"]})
    backend.handle({"command": "prepare_briefing", "meeting_id": meeting["id"]})

    snapshot = [event for event in emitter.events if event["type"] == "snapshot"][-1][
        "data"
    ]
    assert snapshot["current_meeting_id"] == meeting["id"]
    assert "Брифинг к следующей встрече" in snapshot["meeting_briefing"]

    backend.handle({"command": "delete_meeting", "meeting_id": meeting["id"]})

    with pytest.raises(KeyError):
        backend.store.get_meeting(meeting["id"])
    deleted = next(event for event in emitter.events if event["type"] == "meeting_deleted")
    assert deleted["meeting_id"] == meeting["id"]
    assert deleted["deleted_sources"] == 1
    final_snapshot = [
        event for event in emitter.events if event["type"] == "snapshot"
    ][-1]["data"]
    assert final_snapshot["current_meeting_id"] is None
    assert final_snapshot["meetings"] == []


def test_express_transcript_import_rejects_unknown_format_before_worker(tmp_path) -> None:
    emitter = CapturingEmitter()
    backend = WindowsPilotBackend(tmp_path / "assistant.sqlite3", emitter)
    unsupported = tmp_path / "meeting.exe"
    unsupported.write_bytes(b"not a transcript")

    backend.handle({"command": "import_meeting_transcript", "path": str(unsupported)})

    assert emitter.events[-1] == {
        "type": "meeting_transcript_import_error",
        "message": "Формат транскрипта не поддерживается",
        "retryable": True,
    }
    assert backend._meeting_import_worker is None


def test_downloaded_express_audio_is_transcribed_locally_and_analyzed(tmp_path) -> None:
    emitter = CapturingEmitter()
    voice = FakeVoiceRuntime()
    backend = WindowsPilotBackend(
        tmp_path / "assistant.sqlite3",
        emitter,
        voice_runtime=voice,
    )
    recording = tmp_path / "recordings-bot.aac"
    recording.write_bytes(b"fake-aac-payload")

    backend.handle(
        {
            "command": "import_meeting_audio",
            "path": str(recording),
            "workspace_id": backend.current_workspace_id,
        }
    )

    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and not any(
        event["type"] == "meeting_audio_imported" for event in emitter.events
    ):
        time.sleep(0.005)

    imported = next(
        event for event in emitter.events if event["type"] == "meeting_audio_imported"
    )
    assert voice.transcribed_file == recording
    assert imported["source_system"] == "express"
    assert imported["import_mode"] == "LOCAL_AUDIO_TRANSCRIPTION"
    assert imported["meeting_id"]
    managed_audio = imported["source"]["metadata"]["managed_audio_path"]
    assert managed_audio.endswith(".aac")
    assert any(
        event["type"] == "metric" and event.get("name") == "meeting_transcription"
        for event in emitter.events
    )


def test_express_audio_import_requires_stt_but_not_tts(tmp_path) -> None:
    emitter = CapturingEmitter()
    voice = FakeVoiceRuntime(ready=False)
    backend = WindowsPilotBackend(
        tmp_path / "assistant.sqlite3",
        emitter,
        voice_runtime=voice,
    )
    recording = tmp_path / "meeting.aac"
    recording.write_bytes(b"fake-aac-payload")

    backend.handle({"command": "import_meeting_audio", "path": str(recording)})

    error = emitter.events[-1]
    assert error["type"] == "meeting_audio_import_error"
    assert error["retryable"] is True
    assert "STT" in str(error["message"])


def test_voice_jsonl_contract_runs_stt_llm_and_short_streaming_tts(tmp_path) -> None:
    emitter = CapturingEmitter()
    voice = FakeVoiceRuntime()
    backend = WindowsPilotBackend(
        tmp_path / "assistant.sqlite3",
        emitter,
        chat_factory=lambda base_url, model, api_key: VoiceChat(base_url, model, api_key),
        voice_runtime=voice,
    )
    backend.configure_llm(
        {
            "base_url": "http://127.0.0.1:11434/v1",
            "model": "qwen3:4b",
            "provider_type": "local",
        }
    )
    backend.handle({"command": "voice_session_start", "sample_rate": 16_000})
    backend.handle(
        {
            "command": "voice_utterance_start",
            "sample_rate": 16_000,
            "encoding": "pcm_s16le",
            "channels": 1,
        }
    )
    pcm = b"\x10\x00" * 6_400
    backend.handle(
        {
            "command": "voice_audio_chunk",
            "sequence": 0,
            "data": base64.b64encode(pcm).decode("ascii"),
        }
    )
    backend.handle({"command": "voice_utterance_end", "duration_ms": 400})

    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and not any(
        event["type"] == "assistant_end" for event in emitter.events
    ):
        time.sleep(0.01)

    assert voice.transcribed_audio == pcm
    assert voice.spoken_texts == ["Короткий голосовой ответ."]
    event_types = [event["type"] for event in emitter.events]
    assert event_types.index("dictation_ready") < event_types.index("assistant_start")
    assert event_types.index("audio_start") < event_types.index("audio_chunk")
    assert event_types.index("audio_chunk") < event_types.index("audio_end")
    chunks = [event for event in emitter.events if event["type"] == "audio_chunk"]
    assert b"".join(base64.b64decode(event["data"]) for event in chunks) == (
        b"\x00\x01" * 400 + b"\x02\x03" * 200
    )
    answer = next(event for event in emitter.events if event["type"] == "assistant_end")
    assert answer["text"] == (
        "Короткий голосовой ответ. В чате остаётся полное подробное продолжение."
    )
    assert answer["spoken"] is True
    assert answer["spoken_text"] == "Короткий голосовой ответ."


def test_voice_audio_contract_rejects_bad_sequence_and_base64(tmp_path) -> None:
    backend = WindowsPilotBackend(
        tmp_path / "assistant.sqlite3",
        CapturingEmitter(),
        voice_runtime=FakeVoiceRuntime(),
    )
    backend.start_voice_session({"sample_rate": 16_000})
    backend.start_voice_utterance(
        {"sample_rate": 16_000, "encoding": "pcm_s16le", "channels": 1}
    )
    with pytest.raises(RuntimeError, match="уже записывается"):
        backend.start_voice_utterance(
            {"sample_rate": 16_000, "encoding": "pcm_s16le", "channels": 1}
        )
    with pytest.raises(ValueError, match="порядок"):
        backend.append_voice_chunk({"sequence": 1, "data": "AAAA"})
    with pytest.raises(ValueError, match="base64"):
        backend.append_voice_chunk({"sequence": 0, "data": "not-base64!"})


def test_voice_cancel_aborts_chat_and_tts_runtime(tmp_path) -> None:
    voice = FakeVoiceRuntime()
    backend = WindowsPilotBackend(
        tmp_path / "assistant.sqlite3",
        CapturingEmitter(),
        chat_factory=fake_factory,
        voice_runtime=voice,
    )
    backend.configure_llm(
        {"base_url": "http://localhost:11434/v1", "model": "qwen3:4b"}
    )
    backend._cancel_event = threading.Event()
    backend.cancel_turn()
    assert backend._cancel_event.is_set()
    assert backend._chat.cancelled
    assert voice.cancelled == 1


def test_portable_voice_runtime_requires_explicit_both_sides() -> None:
    runtime = PortableWindowsVoiceRuntime.from_environment(
        {"RND_WORKBENCH_WINDOWS_WHISPER_MODEL": "small"}
    )
    assert runtime.configured is False
    assert runtime.ready is False
    diagnostics = runtime.diagnostics()
    assert diagnostics["stt"]["ready"] is False
    assert "OMNIVOICE_URL" in diagnostics["tts"]["detail"]


def test_omnivoice_service_url_is_restricted_to_loopback() -> None:
    assert normalize_loopback_service_url("http://127.0.0.1:8080") == (
        "http://127.0.0.1:8080"
    )
    with pytest.raises(ValueError, match="loopback"):
        normalize_loopback_service_url("https://voice.example.com")


def test_omnivoice_client_resolves_one_stable_profile_and_seed() -> None:
    client = OmniVoiceLoopbackClient(
        "http://127.0.0.1:8080",
        voice="",
        model_name="omnivoice-fast",
    )
    assert client.voice == "female, young adult, moderate pitch, russian accent"
    assert client.seed == 42


def test_renderer_voice_diagnostics_are_allowlisted_and_bounded(tmp_path) -> None:
    emitter = CapturingEmitter()
    backend = WindowsPilotBackend(tmp_path / "assistant.sqlite3", emitter)
    backend.handle(
        {
            "command": "voice_diagnostic",
            "kind": "capture_signal",
            "peak": 0.91,
            "clipped_samples": 3,
            "total_samples": 16000,
            "hardware_measured": True,
            "local_path": "C:\\secret\\audio.wav",
        }
    )
    event = emitter.events[-1]
    assert event == {
        "type": "diagnostic",
        "component": "electron_audio",
        "check": "capture_signal",
        "metrics": {
            "peak": 0.91,
            "clipped_samples": 3,
            "total_samples": 16000,
            "hardware_measured": True,
        },
    }
    summary = backend.store.pilot_metrics_summary(platform="windows")
    assert summary["metrics"]["input_peak"]["p50"] == 0.91
    assert summary["metrics"]["input_clipping_ratio"]["p50"] == 0.000188
    with pytest.raises(ValueError, match="Неизвестный"):
        backend.handle({"command": "voice_diagnostic", "kind": "arbitrary"})


def test_renderer_voice_timing_diagnostics_are_persisted_without_content(tmp_path) -> None:
    backend = WindowsPilotBackend(
        tmp_path / "assistant.sqlite3",
        CapturingEmitter(),
        voice_runtime=FakeVoiceRuntime(),
    )
    backend.handle(
        {
            "command": "voice_diagnostic",
            "kind": "listen_ready",
            "seconds": 0.21,
            "hardware_measured": True,
            "transcript": "НЕ СОХРАНЯТЬ",
        }
    )
    backend.handle(
        {
            "command": "voice_diagnostic",
            "kind": "playback_first_audio",
            "seconds": 2.4,
            "hardware_measured": True,
            "prompt": "НЕ СОХРАНЯТЬ",
        }
    )
    summary = backend.store.pilot_metrics_summary(platform="windows")
    database_bytes = (tmp_path / "assistant.sqlite3").read_bytes()

    assert summary["metrics"]["listen_ready_seconds"]["p50"] == 0.21
    assert summary["metrics"]["first_audio_seconds"]["p50"] == 2.4
    assert b"\xd0\x9d\xd0\x95 \xd0\xa1\xd0\x9e\xd0\xa5\xd0\xa0\xd0\x90\xd0\x9d\xd0\xaf\xd0\xa2\xd0\xac" not in database_bytes


def test_voice_self_check_does_not_claim_unmeasured_windows_hardware_slo(tmp_path) -> None:
    emitter = CapturingEmitter()
    backend = WindowsPilotBackend(
        tmp_path / "assistant.sqlite3",
        emitter,
        voice_runtime=FakeVoiceRuntime(),
    )
    backend.handle({"command": "voice_self_check"})
    diagnostic = next(event for event in emitter.events if event["type"] == "diagnostic")
    assert diagnostic["check"] == "hardware_slo"
    assert diagnostic["measured"] is False

    backend.handle({"command": "voice_dependency_probe"})
    dependency_probe = next(
        event
        for event in reversed(emitter.events)
        if event.get("check") == "runtime_dependencies"
    )
    assert dependency_probe["measured"] is True
    assert isinstance(dependency_probe["ready"], bool)
    assert set(dependency_probe["components"]) == {
        "faster_whisper",
        "ctranslate2",
        "tokenizers",
    }
    assert all(isinstance(value, bool) for value in dependency_probe["components"].values())


def test_windows_pilot_preflight_is_dynamic_content_free_and_honest(tmp_path) -> None:
    emitter = CapturingEmitter()
    backend = WindowsPilotBackend(
        tmp_path / "assistant.sqlite3",
        emitter,
        chat_factory=fake_factory,
        voice_runtime=FakeVoiceRuntime(),
        core_policy=FakeCorePolicy(),
    )
    backend.configure_llm(
        {
            "base_url": "http://127.0.0.1:11434/v1",
            "model": "qwen3:4b",
            "provider_type": "local",
        }
    )
    backend.accept_voice_diagnostic(
        {
            "kind": "capture_ready",
            "browser_sample_rate": 48_000,
            "target_sample_rate": 16_000,
            "hardware_measured": True,
            "transcript": "НЕ СОХРАНЯТЬ",
        }
    )

    backend.handle({"command": "pilot_preflight"})

    event = next(item for item in emitter.events if item["type"] == "pilot_preflight")
    result = event["result"]
    statuses = {check["id"]: check["status"] for check in result["checks"]}
    assert result["overall"] == "limited"
    assert result["content_transmitted"] is False
    assert statuses["storage"] == "pass"
    assert statuses["llm"] == "pass"
    assert statuses["stt"] == "pass"
    assert statuses["tts"] == "pass"
    assert statuses["microphone"] == "pass"
    assert statuses["voice_slo"] == "unverified"
    assert statuses["corporate_connectors"] == "warn"
    assert "НЕ СОХРАНЯТЬ" not in json.dumps(result, ensure_ascii=False)

    backend.emit_snapshot()
    snapshot = [item for item in emitter.events if item["type"] == "snapshot"][-1][
        "data"
    ]
    assert snapshot["pilot_onboarding"]["stage"] == "first_voice_result"
    assert snapshot["pilot_onboarding"]["action_id"] == "start_voice"
    assert snapshot["pilot_onboarding"]["content_transmitted"] is False
    assert "НЕ СОХРАНЯТЬ" not in json.dumps(
        snapshot["pilot_onboarding"], ensure_ascii=False
    )


def test_windows_backend_marks_pilot_session_clean_only_on_quit(tmp_path) -> None:
    emitter = CapturingEmitter()
    backend = WindowsPilotBackend(
        tmp_path / "assistant.sqlite3",
        emitter,
        core_policy=FakeCorePolicy(),
    )

    backend._begin_pilot_session()
    active = backend.store.pilot_usage_summary(platform="windows")
    backend.handle({"command": "quit"})
    finished = backend.store.pilot_usage_summary(platform="windows")

    assert active["sessions"] == 1
    assert active["observed_session_exits"] == 0
    assert active["crash_free_session_rate"] is None
    assert finished["clean_session_exits"] == 1
    assert finished["unclean_session_exits"] == 0
    assert finished["crash_free_session_rate"] == 1.0


def test_windows_express_sync_persists_checkpoint_and_updates_preflight(tmp_path) -> None:
    emitter = CapturingEmitter()
    intake = FakeExpressIntake()
    backend = WindowsPilotBackend(
        tmp_path / "assistant.sqlite3",
        emitter,
        voice_runtime=FakeVoiceRuntime(),
        express_intake=intake,
    )

    backend.handle({"command": "sync_express_meetings"})
    worker = backend._meeting_import_worker
    assert worker is not None
    worker.join(timeout=2)

    checkpoint = backend.store.connector_checkpoint(
        CONNECTOR_ID,
        backend.current_workspace_id,
    )
    assert checkpoint is not None
    assert checkpoint["cursor"] == "cursor-win-1"
    assert intake.cursors == [None]
    completed = next(
        event for event in emitter.events if event["type"] == "express_sync_completed"
    )
    snapshot = [event for event in emitter.events if event["type"] == "snapshot"][-1][
        "data"
    ]
    assert completed["processed"] == 0
    assert snapshot["express_connector"]["connected"] is True
    statuses = {
        check["id"]: check["status"]
        for check in snapshot["pilot_preflight"]["checks"]
    }
    assert statuses["corporate_connectors"] == "pass"


def test_push_to_talk_capability_requires_only_local_stt(tmp_path) -> None:
    emitter = CapturingEmitter()
    backend = WindowsPilotBackend(
        tmp_path / "assistant.sqlite3",
        emitter,
        voice_runtime=DictationOnlyVoiceRuntime(),
    )

    backend.emit_voice_capability()
    backend.emit_dictation_capability()

    voice = next(
        event for event in reversed(emitter.events)
        if event["type"] == "capability" and event["id"] == "windows_voice"
    )
    dictation = next(
        event for event in reversed(emitter.events)
        if event["type"] == "capability" and event["id"] == "windows_push_to_talk"
    )
    assert voice["available"] is False
    assert dictation["available"] is True
    assert dictation["key"] == "F8"
    assert set(dictation["diagnostics"]) == {"stt", "capture", "insertion"}


def test_push_to_talk_runs_local_stt_without_creating_chat_turn(tmp_path) -> None:
    emitter = CapturingEmitter()
    voice = DictationOnlyVoiceRuntime()
    backend = WindowsPilotBackend(
        tmp_path / "assistant.sqlite3",
        emitter,
        voice_runtime=voice,
    )
    request_id = "ptt-contract-1"
    pcm = b"\x10\x00" * 6_400

    backend.handle(
        {
            "command": "ptt_dictation_start",
            "request_id": request_id,
            "sample_rate": 16_000,
            "encoding": "pcm_s16le",
            "channels": 1,
        }
    )
    backend.handle(
        {
            "command": "ptt_audio_chunk",
            "request_id": request_id,
            "sequence": 0,
            "data": base64.b64encode(pcm).decode("ascii"),
        }
    )
    backend.handle({"command": "ptt_dictation_end", "request_id": request_id})

    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and not any(
        event["type"] == "dictation_result" for event in emitter.events
    ):
        time.sleep(0.01)

    result = next(event for event in emitter.events if event["type"] == "dictation_result")
    assert result["request_id"] == request_id
    assert result["text"] == "Подготовь ответ голосом"
    assert result["local"] is True
    assert isinstance(result["seconds"], float)
    assert voice.transcribed_audio == pcm
    assert not any(event["type"] in {"user", "assistant_start"} for event in emitter.events)


def test_rejected_ptt_press_cannot_cancel_another_request_being_transcribed(
    tmp_path,
) -> None:
    emitter = CapturingEmitter()
    backend = WindowsPilotBackend(
        tmp_path / "assistant.sqlite3",
        emitter,
        voice_runtime=DictationOnlyVoiceRuntime(),
    )
    active_cancel = threading.Event()
    backend._dictation_cancel_event = active_cancel
    backend._dictation_worker_request_id = "active-request"

    backend.handle(
        {
            "command": "ptt_dictation_cancel",
            "request_id": "rejected-request",
            "reason": "dictation_busy",
        }
    )
    assert not active_cancel.is_set()

    backend.handle(
        {
            "command": "ptt_dictation_cancel",
            "request_id": "active-request",
            "reason": "user_cancel",
        }
    )
    assert active_cancel.is_set()
    terminal = next(
        event for event in emitter.events
        if event["type"] == "dictation_state" and event["state"] == "cancelled"
    )
    assert terminal["request_id"] == "active-request"


def test_http_abort_uses_socket_shutdown_before_close() -> None:
    calls: list[object] = []

    class FakeSocket:
        def shutdown(self, how) -> None:  # noqa: ANN001
            calls.append(("shutdown", how))

        def close(self) -> None:
            calls.append("socket_close")

    class FakeConnection:
        sock = FakeSocket()

        def close(self) -> None:
            calls.append("connection_close")

    _abort_http_connection(FakeConnection())  # type: ignore[arg-type]

    assert calls[0][0] == "shutdown"
    assert calls[1:] == ["socket_close", "connection_close"]


def test_http_abort_wakes_a_blocking_socket_read_without_waiting_for_timeout() -> None:
    reader, peer = socket.socketpair()
    released = threading.Event()

    class SocketConnection:
        sock = reader

        def close(self) -> None:
            reader.close()

    def blocking_read() -> None:
        try:
            reader.recv(1)
        except OSError:
            pass
        finally:
            released.set()

    worker = threading.Thread(target=blocking_read, daemon=True)
    worker.start()
    try:
        _abort_http_connection(SocketConnection())  # type: ignore[arg-type]
        assert released.wait(0.5)
    finally:
        peer.close()


def test_barge_in_queue_releases_after_bounded_cancel_wait(tmp_path) -> None:
    backend = WindowsPilotBackend(
        tmp_path / "assistant.sqlite3",
        CapturingEmitter(),
        voice_runtime=FakeVoiceRuntime(),
    )
    release_old = threading.Event()
    old_worker = threading.Thread(target=lambda: release_old.wait(1), daemon=True)
    old_worker.start()
    replacement_started = threading.Event()
    received: list[bytes] = []

    def replacement(audio, _cancel_event, _response_started_at) -> None:  # noqa: ANN001
        received.append(audio)
        replacement_started.set()

    backend._VOICE_CANCEL_WAIT_SECONDS = 0.02
    backend._run_voice_turn = replacement  # type: ignore[method-assign]
    backend._voice_session_active = True
    backend._pending_voice_audio = (b"new utterance", time.perf_counter())
    backend._worker = old_worker

    started = time.monotonic()
    backend._wait_for_voice_slot()
    elapsed = time.monotonic() - started
    assert replacement_started.wait(0.5)
    release_old.set()

    assert elapsed < 0.5
    assert received == [b"new utterance"]
