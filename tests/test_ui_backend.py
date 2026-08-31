import json
import threading
import time

import numpy as np
import pytest
import voice_assistant.ui_backend as ui_backend_module

from voice_assistant.audio import (
    Microphone,
    PushToTalkDurationExceededError,
    UtteranceDetector,
)
from voice_assistant.config import AudioConfig, Config
from voice_assistant.integrations import IntegrationIntent, IntegrationRequest
from voice_assistant.java_core import JavaRouteDecision
from voice_assistant.store import AssistantStore
from voice_assistant.text import concise_speech_text
from voice_assistant.ui_backend import EventEmitter, UIBackend


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
            local_fallback_before_first_output=bool(kwargs["local_available"]),
        )

    def close(self) -> None:
        self.closed = True


def test_event_emitter_writes_single_json_line(capsys) -> None:
    EventEmitter().emit("state", state="ready", detail="Готов")
    payload = json.loads(capsys.readouterr().out)
    assert payload == {"type": "state", "state": "ready", "detail": "Готов"}


def test_unknown_ui_command_is_reported(capsys, tmp_path) -> None:
    backend = UIBackend(
        Config.defaults(), EventEmitter(), AssistantStore(tmp_path / "assistant.sqlite3")
    )
    backend.handle({"command": "does-not-exist"})
    payload = json.loads(capsys.readouterr().out)
    assert payload["type"] == "error"
    assert "does-not-exist" in payload["message"]


def test_macos_approval_command_uses_integration_hub_and_never_claims_false_success(
    capsys,
    tmp_path,
) -> None:  # noqa: ANN001
    store = AssistantStore(tmp_path / "assistant.sqlite3")
    backend = UIBackend(
        Config.defaults(),
        EventEmitter(),
        store,
        core_policy=FakeCorePolicy(),
    )
    task = store.create_task(store.default_workspace_id(), "Обновить страницу")
    approval = backend.integration_hub.stage(
        IntegrationRequest(
            "confluence",
            "page.update",
            IntegrationIntent.WRITE,
            {"title": "Итоги встречи"},
        ),
        task_id=task["id"],
    )

    backend._resolve_approval(approval["id"], "approved")

    row = store._rows(
        "SELECT status, result FROM approvals WHERE id=?",
        (approval["id"],),
    )[0]
    assert row["status"] == "error"
    assert "не подключена" in row["result"]
    assert store.get_task(task["id"])["status"] == "needs_user"
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert any(event["type"] == "approval_execution_failed" for event in events)
    assert not any(
        event["type"] == "approval_resolved" and event.get("status") == "succeeded"
        for event in events
    )


def test_microphone_listen_can_be_cancelled_without_audio() -> None:
    config = AudioConfig()
    microphone = Microphone(config)
    microphone.detector = UtteranceDetector(config, threshold=0.01)
    cancelled = threading.Event()
    cancelled.set()
    assert microphone.listen(cancelled) is None


def test_stop_session_cancels_current_answer(tmp_path) -> None:
    backend = UIBackend(
        Config.defaults(), EventEmitter(), AssistantStore(tmp_path / "assistant.sqlite3")
    )
    turn = threading.Event()
    backend._set_current_turn(turn)
    backend.stop_session()
    assert turn.is_set()


def test_macos_pilot_preflight_uses_loaded_runtime_without_work_content(
    tmp_path,
) -> None:
    backend = UIBackend(
        Config.defaults(),
        EventEmitter(),
        AssistantStore(tmp_path / "assistant.sqlite3"),
        core_policy=FakeCorePolicy(),
    )
    backend.assistant.stt.model = object()
    backend._local_chat.model = object()
    backend._local_chat.tokenizer = object()

    class ReadyTTS:
        model = object()

    backend.assistant.tts = ReadyTTS()
    backend._microphone_verified = True

    result = backend._build_pilot_preflight()
    statuses = {check["id"]: check["status"] for check in result["checks"]}

    assert result["overall"] == "limited"
    assert result["content_transmitted"] is False
    assert statuses["storage"] == "pass"
    assert statuses["llm"] == "pass"
    assert statuses["stt"] == "pass"
    assert statuses["tts"] == "pass"
    assert statuses["microphone"] == "pass"
    assert statuses["voice_slo"] == "unverified"


def test_push_to_talk_dictation_returns_system_text_without_llm(
    monkeypatch, capsys, tmp_path
) -> None:
    backend = UIBackend(
        Config.defaults(), EventEmitter(), AssistantStore(tmp_path / "assistant.sqlite3")
    )

    class FakeMicrophone:
        def __init__(self, config) -> None:  # noqa: ANN001
            del config

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):  # noqa: ANN001
            del exc_type, exc, traceback

        @staticmethod
        def record_until_release(  # noqa: ANN001
            release_event, *, cancel_event=None, max_duration_s=None
        ):
            del release_event, cancel_event
            assert max_duration_s == 120.0
            return np.ones(3200, dtype=np.float32)

    class FakeSTT:
        @staticmethod
        def transcribe(audio, sample_rate):  # noqa: ANN001
            assert audio.size == 3200
            assert sample_rate == 16_000
            return "Вставь этот текст в активное поле"

    class DictationOnlyAssistant:
        stt = FakeSTT()

        @staticmethod
        def answer(*args, **kwargs):  # noqa: ANN002, ANN003
            raise AssertionError("Диктовка не должна вызывать LLM")

    monkeypatch.setattr(ui_backend_module, "Microphone", FakeMicrophone)
    backend.assistant = DictationOnlyAssistant()

    backend._dictation_loop("system")

    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    ready = next(event for event in events if event["type"] == "dictation_ready")
    assert ready["text"] == "Вставь этот текст в активное поле"
    assert ready["destination"] == "system"
    assert any(event["type"] == "dictation_started" for event in events)
    assert any(event["type"] == "dictation_stopped" for event in events)
    event_types = [event["type"] for event in events]
    assert event_types.index("dictation_started") < event_types.index("dictation_stopped")
    assert event_types.index("dictation_stopped") < event_types.index("dictation_ready")
    ready_state_index = max(
        index
        for index, event in enumerate(events)
        if event["type"] == "state" and event.get("state") == "ready"
    )
    assert ready_state_index < event_types.index("dictation_ready")
    assert not any(event["type"] in {"user", "assistant_start"} for event in events)
    assert not backend.task_lock.locked()


def test_push_to_talk_release_and_cancel_are_separate(tmp_path) -> None:
    backend = UIBackend(
        Config.defaults(), EventEmitter(), AssistantStore(tmp_path / "assistant.sqlite3")
    )

    backend.stop_dictation()
    assert backend.dictation_release_event.is_set()
    assert not backend.dictation_cancel_event.is_set()

    backend.cancel_dictation()
    assert backend.dictation_release_event.is_set()
    assert backend.dictation_cancel_event.is_set()


def test_push_to_talk_duration_limit_is_explicit_and_never_returns_text(
    monkeypatch, capsys, tmp_path
) -> None:
    backend = UIBackend(
        Config.defaults(), EventEmitter(), AssistantStore(tmp_path / "assistant.sqlite3")
    )

    class FakeMicrophone:
        def __init__(self, config) -> None:  # noqa: ANN001
            del config

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):  # noqa: ANN001
            del exc_type, exc, traceback

        @staticmethod
        def record_until_release(  # noqa: ANN001
            release_event, *, cancel_event=None, max_duration_s=None
        ):
            del release_event, cancel_event
            assert max_duration_s == backend._DICTATION_MAX_DURATION_S
            raise PushToTalkDurationExceededError("lost key-up")

    monkeypatch.setattr(ui_backend_module, "Microphone", FakeMicrophone)

    backend._dictation_loop("system")

    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert not any(event["type"] == "dictation_ready" for event in events)
    error = next(event for event in events if event["type"] == "dictation_error")
    assert error["code"] == "duration_limit"
    assert error["error_type"] == "PushToTalkDurationExceededError"
    assert not backend.task_lock.locked()


def test_cancel_during_stalled_dictation_stt_releases_shared_lock_without_ready(
    monkeypatch, capsys, tmp_path
) -> None:
    backend = UIBackend(
        Config.defaults(), EventEmitter(), AssistantStore(tmp_path / "assistant.sqlite3")
    )
    entered_stt = threading.Event()
    release_stt = threading.Event()

    class FakeMicrophone:
        def __init__(self, config) -> None:  # noqa: ANN001
            del config

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):  # noqa: ANN001
            del exc_type, exc, traceback

        @staticmethod
        def record_until_release(*args, **kwargs):  # noqa: ANN002, ANN003
            return np.ones(3200, dtype=np.float32)

    class StalledSTT:
        @staticmethod
        def transcribe(audio, sample_rate):  # noqa: ANN001
            del audio, sample_rate
            entered_stt.set()
            release_stt.wait(timeout=2)
            return "Этот текст нельзя отдавать после отмены"

    class DictationAssistant:
        stt = StalledSTT()

    monkeypatch.setattr(ui_backend_module, "Microphone", FakeMicrophone)
    backend.assistant = DictationAssistant()
    lifecycle = threading.Thread(target=backend._dictation_loop, args=("system",))
    lifecycle.start()
    assert entered_stt.wait(timeout=1)

    backend.cancel_dictation()
    lifecycle.join(timeout=1)

    assert not lifecycle.is_alive()
    assert not backend.task_lock.locked()
    release_stt.set()
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert not any(event["type"] == "dictation_ready" for event in events)


def test_cancel_observed_at_stt_return_cannot_publish_dictation_ready(
    monkeypatch, capsys, tmp_path
) -> None:
    backend = UIBackend(
        Config.defaults(), EventEmitter(), AssistantStore(tmp_path / "assistant.sqlite3")
    )

    class FakeMicrophone:
        def __init__(self, config) -> None:  # noqa: ANN001
            del config

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):  # noqa: ANN001
            del exc_type, exc, traceback

        @staticmethod
        def record_until_release(*args, **kwargs):  # noqa: ANN002, ANN003
            return np.ones(3200, dtype=np.float32)

    class CancellingSTT:
        @staticmethod
        def transcribe(audio, sample_rate):  # noqa: ANN001
            del audio, sample_rate
            backend.dictation_cancel_event.set()
            return "Результат уже отменён"

    class DictationAssistant:
        stt = CancellingSTT()

    monkeypatch.setattr(ui_backend_module, "Microphone", FakeMicrophone)
    backend.assistant = DictationAssistant()

    backend._dictation_loop("system")

    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert not any(event["type"] == "dictation_ready" for event in events)
    assert not backend.task_lock.locked()


def test_dictation_stt_timeout_releases_shared_lock_and_is_retryable(
    monkeypatch, capsys, tmp_path
) -> None:
    backend = UIBackend(
        Config.defaults(), EventEmitter(), AssistantStore(tmp_path / "assistant.sqlite3")
    )
    backend._DICTATION_STT_TIMEOUT_S = 0.05
    backend._DICTATION_STT_POLL_S = 0.005
    release_stt = threading.Event()

    class FakeMicrophone:
        def __init__(self, config) -> None:  # noqa: ANN001
            del config

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):  # noqa: ANN001
            del exc_type, exc, traceback

        @staticmethod
        def record_until_release(*args, **kwargs):  # noqa: ANN002, ANN003
            return np.ones(3200, dtype=np.float32)

    class StalledSTT:
        @staticmethod
        def transcribe(audio, sample_rate):  # noqa: ANN001
            del audio, sample_rate
            release_stt.wait(timeout=2)
            return "Слишком поздний результат"

    class DictationAssistant:
        stt = StalledSTT()

    monkeypatch.setattr(ui_backend_module, "Microphone", FakeMicrophone)
    backend.assistant = DictationAssistant()

    backend._dictation_loop("system")
    release_stt.set()

    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert not any(event["type"] == "dictation_ready" for event in events)
    error = next(event for event in events if event["type"] == "dictation_error")
    assert error["code"] == "stt_timeout"
    assert error["retryable"] is True
    assert not backend.task_lock.locked()


def test_new_dictation_waits_for_timed_out_stt_worker(capsys, tmp_path) -> None:
    backend = UIBackend(
        Config.defaults(), EventEmitter(), AssistantStore(tmp_path / "assistant.sqlite3")
    )
    release_worker = threading.Event()

    def wait_for_release() -> None:
        release_worker.wait(timeout=2)

    worker = threading.Thread(target=wait_for_release, daemon=True)
    with backend._dictation_stt_worker_lock:
        backend._dictation_stt_worker = worker
    worker.start()

    backend.start_dictation(destination="system")

    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert events[-1]["type"] == "dictation_error"
    assert events[-1]["code"] == "stt_worker_busy"
    assert backend.dictation_thread is None
    release_worker.set()
    worker.join(timeout=1)


def test_push_to_talk_busy_error_is_terminal_after_stopped_event(
    capsys, tmp_path
) -> None:  # noqa: ANN001
    backend = UIBackend(
        Config.defaults(), EventEmitter(), AssistantStore(tmp_path / "assistant.sqlite3")
    )
    backend.task_lock.acquire()
    try:
        backend._dictation_loop("system")
    finally:
        backend.task_lock.release()

    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [event["type"] for event in events] == [
        "dictation_stopped",
        "state",
        "dictation_error",
    ]
    assert events[-1]["destination"] == "system"


def test_text_submission_while_backend_is_busy_emits_error(capsys, tmp_path) -> None:
    backend = UIBackend(
        Config.defaults(), EventEmitter(), AssistantStore(tmp_path / "assistant.sqlite3")
    )
    backend.task_lock.acquire()
    try:
        backend.submit_text("Не зависай")
    finally:
        backend.task_lock.release()

    event = json.loads(capsys.readouterr().out)
    assert event == {
        "type": "error",
        "message": "Дождитесь завершения текущей задачи или автоматизации",
    }
    assert backend.text_thread is None


def test_compact_chat_can_request_silent_text_answer(tmp_path) -> None:
    backend = UIBackend(
        Config.defaults(), EventEmitter(), AssistantStore(tmp_path / "assistant.sqlite3")
    )
    captured: dict[str, object] = {}

    class FakeAssistant:
        def answer(self, text, **kwargs):  # noqa: ANN001
            captured["text"] = text
            captured.update(kwargs)
            return "Тихий ответ"

    backend.assistant = FakeAssistant()
    backend._text_turn("Проверка чата", speak=False)

    assert captured["speak"] is False
    assert captured["echo"] is False


def test_macos_text_turn_is_gated_by_java_core_before_llm(capsys, tmp_path) -> None:
    store = AssistantStore(tmp_path / "assistant.sqlite3")
    core = FakeCorePolicy()
    backend = UIBackend(
        Config.defaults(),
        EventEmitter(),
        store,
        core_policy=core,
    )
    class FakeAssistant:
        @staticmethod
        def answer(text, *, on_token, on_phase, on_speech_text, **kwargs):  # noqa: ANN001
            del text, kwargs
            assert core.calls
            on_phase("thinking")
            on_token("Проверенный ответ")
            on_speech_text("")
            return "Проверенный ответ"

    backend.assistant = FakeAssistant()
    backend._text_turn("Проверь маршрут", speak=False)

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
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    answer = next(event for event in events if event["type"] == "assistant_end")
    assert answer["llm_route"]["policy_engine"] == "java21"
    assert answer["llm_route"]["java_core_route"] == "LOCAL"
    assert answer["llm_route"]["java_core_configured"] is True


def test_macos_java_route_disagreement_blocks_llm_fail_closed(capsys, tmp_path) -> None:
    store = AssistantStore(tmp_path / "assistant.sqlite3")
    core = FakeCorePolicy(
        decision=JavaRouteDecision(
            status="BLOCKED",
            route=None,
            reason="CLASSIFICATION_BLOCKS_CORPORATE",
            local_fallback_before_first_output=False,
        )
    )
    backend = UIBackend(
        Config.defaults(),
        EventEmitter(),
        store,
        core_policy=core,
    )

    class NeverCalledAssistant:
        @staticmethod
        def answer(*args, **kwargs):  # noqa: ANN002, ANN003, ANN201
            raise AssertionError("LLM must not be called")

    backend.assistant = NeverCalledAssistant()
    backend._text_turn("Не отправляй запрос", speak=False)

    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert any(event["type"] == "routing_blocked" for event in events)
    assert not any(event["type"] == "assistant_start" for event in events)
    assert store.get_task(backend.current_task_id)["status"] == "needs_user"


def test_macos_external_route_passes_only_explicit_policy_metadata(capsys, tmp_path) -> None:
    store = AssistantStore(tmp_path / "assistant.sqlite3")
    store.set_settings(
        {
            "model_mode": "external",
            "llm_base_url": "https://api.example/v1",
            "llm_model": "public-model",
            "external_provider_type": "external",
        }
    )
    core = FakeCorePolicy()
    backend = UIBackend(
        Config.defaults(),
        EventEmitter(),
        store,
        core_policy=core,
    )
    store.set_classification("workspace", backend.current_workspace_id, "public")
    public_task = store.create_task(
        backend.current_workspace_id,
        "Публичная задача",
        classification="public",
    )
    backend.current_task_id = public_task["id"]

    class RemoteRuntime:
        ready = True
        base_url = "https://api.example/v1"
        model_name = "public-model"

    remote = RemoteRuntime()
    backend._remote_chat = remote  # type: ignore[assignment]

    class FakeAssistant:
        @staticmethod
        def answer(text, *, chat_backend, on_token, on_phase, on_speech_text, **kwargs):  # noqa: ANN001
            del text, kwargs
            assert chat_backend is remote
            on_phase("thinking")
            on_token("Внешний ответ")
            on_speech_text("")
            return "Внешний ответ"

    backend.assistant = FakeAssistant()
    backend._text_turn("Публичный вопрос", speak=False)

    assert core.calls[0]["preference"] == "EXTERNAL"
    assert core.calls[0]["classification"] == "public"
    assert core.calls[0]["external_available"] is True
    assert core.calls[0]["explicit_external_consent"] is True
    serialized = json.dumps(core.calls, ensure_ascii=False).casefold()
    assert "публичный вопрос" not in serialized
    assert "prompt" not in serialized
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    answer = next(event for event in events if event["type"] == "assistant_end")
    assert answer["llm_route"]["actual_route"] == "external_api"
    assert answer["llm_route"]["java_core_route"] == "EXTERNAL"


def test_macos_snapshot_exposes_visible_java_policy_fallback(capsys, tmp_path) -> None:
    core = FakeCorePolicy(ready=False)
    backend = UIBackend(
        Config.defaults(),
        EventEmitter(),
        AssistantStore(tmp_path / "assistant.sqlite3"),
        core_policy=core,
    )

    backend.emit_snapshot()
    backend.handle({"command": "quit"})

    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    snapshot = next(event for event in events if event["type"] == "snapshot")
    assert snapshot["data"]["platform"] == {
        "name": "macos",
        "java_core_policy": {
            "configured": True,
            "ready": False,
            "protocol_version": None,
            "policy": "python_fallback",
        },
        "java_action_journal": {
            "configured": True,
            "ready": False,
            "production_fail_closed": True,
            "content_transmitted": False,
            "recovery": {
                "journal_ready": False,
                "inspected": 0,
                "resolved": 0,
                "requires_attention": 0,
                "skipped": 0,
            },
        },
    }
    assert core.closed is True


def test_first_turn_attachments_are_task_scoped_and_reach_same_prompt(
    capsys, tmp_path
) -> None:
    store = AssistantStore(tmp_path / "assistant.sqlite3")
    backend = UIBackend(Config.defaults(), EventEmitter(), store)
    backend.current_task_id = None
    document = tmp_path / "контекст.md"
    document.write_text("Проект Север: срок запуска — октябрь.", encoding="utf-8")
    captured: dict[str, object] = {}

    class FakeAssistant:
        @staticmethod
        def answer(text, *, on_token, on_phase, on_speech_text, **kwargs):  # noqa: ANN001
            captured["prompt"] = text
            captured.update(kwargs)
            on_phase("thinking")
            on_token("Полный ответ")
            on_speech_text("")
            return "Полный ответ"

    backend.assistant = FakeAssistant()
    backend._text_turn(
        "Когда запуск?",
        speak=False,
        attachments=[{"path": str(document), "kind": None}],
    )

    task_id = backend.current_task_id
    assert task_id
    sources = store.task_sources(task_id)
    assert len(sources) == 1
    assert store.get_source(sources[0]["id"])["visibility"] == "task"
    assert store._rows(
        "SELECT task_id FROM task_sources WHERE source_id=?",
        (sources[0]["id"],),
    ) == [{"task_id": task_id}]
    assert "срок запуска — октябрь" in str(captured["prompt"])
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert sum(event["type"] == "source_imported" for event in events) == 1
    assert next(event for event in events if event["type"] == "assistant_start")[
        "task_id"
    ] == task_id


def test_attachment_batch_rolls_back_if_any_file_fails(capsys, tmp_path) -> None:
    store = AssistantStore(tmp_path / "assistant.sqlite3")
    backend = UIBackend(Config.defaults(), EventEmitter(), store)
    backend.current_task_id = None
    document = tmp_path / "доступен.txt"
    document.write_text("Временный контекст", encoding="utf-8")
    task_count_before = len(store._rows("SELECT id FROM tasks"))

    backend._text_turn(
        "Проверь пакет",
        speak=False,
        attachments=[
            {"path": str(document), "kind": None},
            {"path": str(tmp_path / "нет-файла.txt"), "kind": None},
        ],
    )

    assert store._rows("SELECT id FROM sources") == []
    assert len(store._rows("SELECT id FROM tasks")) == task_count_before
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert any(event["type"] == "error" and "Файл не найден" in event["message"] for event in events)
    assert not any(event["type"] == "assistant_start" for event in events)


def test_attachment_payload_accepts_objects_and_strings_without_duplicates() -> None:
    parsed = UIBackend._parse_attachments(
        ["/tmp/report.md", {"path": "/tmp/report.md"}, {"path": "/tmp/data.csv", "kind": "document"}]
    )

    assert parsed == [
        {"path": "/tmp/report.md", "kind": None},
        {"path": "/tmp/data.csv", "kind": "document"},
    ]


def test_silent_answer_exposes_unspoken_contract(capsys, tmp_path) -> None:
    store = AssistantStore(tmp_path / "assistant.sqlite3")
    backend = UIBackend(Config.defaults(), EventEmitter(), store)

    class FakeAssistant:
        @staticmethod
        def answer(text, *, on_token, on_phase, **kwargs):  # noqa: ANN001
            del text, kwargs
            on_phase("thinking")
            on_token("Текстовый ответ")
            return "Текстовый ответ"

    backend.assistant = FakeAssistant()
    backend._text_turn("Ответь без голоса", speak=False)

    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    answer = next(event for event in events if event["type"] == "assistant_end")
    assert answer["spoken"] is False
    assert answer["tts_error"] is None


def test_tts_failure_keeps_full_text_and_completed_task(capsys, tmp_path) -> None:
    store = AssistantStore(tmp_path / "assistant.sqlite3")
    backend = UIBackend(Config.defaults(), EventEmitter(), store)

    class FakeAssistant:
        @staticmethod
        def answer(
            text,
            *,
            on_token,
            on_phase,
            on_speech_error,
            **kwargs,
        ):  # noqa: ANN001
            del text, kwargs
            on_phase("thinking")
            on_token("Полный ")
            on_phase("speaking")
            on_speech_error(RuntimeError("CoreAudio недоступен"))
            on_token("ответ")
            return "Полный ответ"

    backend.assistant = FakeAssistant()
    backend._text_turn("Проверь TTS", speak=True)

    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    speech_errors = [event for event in events if event["type"] == "speech_error"]
    answer = next(event for event in events if event["type"] == "assistant_end")
    snapshot = [event for event in events if event["type"] == "snapshot"][-1]["data"]
    task = store.get_task(backend.current_task_id)
    assistant_message = store.messages(task["id"])[-1]
    metadata = json.loads(assistant_message["metadata"])
    snapshot_metadata = json.loads(snapshot["messages"][-1]["metadata"])

    assert task["status"] == "done"
    assert task["result"] == "Полный ответ"
    assert assistant_message["content"] == "Полный ответ"
    assert speech_errors == [
        {
            "type": "speech_error",
            "message": "CoreAudio недоступен",
            "task_id": task["id"],
            "retryable": True,
        }
    ]
    assert answer["text"] == "Полный ответ"
    assert answer["spoken"] is False
    assert answer["tts_error"] == "CoreAudio недоступен"
    assert metadata["spoken"] is False
    assert metadata["tts_error"] == "CoreAudio недоступен"
    assert snapshot_metadata["spoken"] is False
    assert snapshot_metadata["tts_error"] == "CoreAudio недоступен"
    assert not any(event["type"] == "error" for event in events)


def test_playback_end_returns_ui_and_barge_state_to_thinking(
    capsys,
    tmp_path,
) -> None:
    store = AssistantStore(tmp_path / "assistant.sqlite3")
    backend = UIBackend(Config.defaults(), EventEmitter(), store)
    speaking = threading.Event()

    class StreamingAssistant:
        @staticmethod
        def answer(
            text,
            *,
            on_token,
            on_phase,
            on_playback_end,
            on_speech_text,
            **kwargs,
        ):  # noqa: ANN001
            del text, kwargs
            on_phase("thinking")
            on_token("Короткий голосовой вывод. ")
            on_phase("speaking")
            assert speaking.is_set()
            on_playback_end()
            assert not speaking.is_set()
            on_token("Подробный хвост остаётся в чате.")
            on_speech_text("Короткий голосовой вывод.")
            return "Короткий голосовой вывод. Подробный хвост остаётся в чате."

    backend.assistant = StreamingAssistant()
    turn = backend._prepare_turn("Расскажи подробно", spoken=True)
    capsys.readouterr()

    backend._answer(
        turn,
        cancel_event=threading.Event(),
        speaking_event=speaking,
    )

    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    states = [event["state"] for event in events if event["type"] == "state"]
    second_delta_index = next(
        index
        for index, event in enumerate(events)
        if event["type"] == "assistant_delta" and event["text"].startswith("Подробный")
    )
    final_thinking_index = max(
        index
        for index, event in enumerate(events)
        if event.get("type") == "state" and event.get("state") == "thinking"
    )

    assert states == ["thinking", "speaking", "thinking"]
    assert final_thinking_index < second_delta_index
    assert not speaking.is_set()


def test_voice_answer_is_interrupted_and_next_utterance_is_captured(capsys, tmp_path) -> None:
    config = Config.defaults()
    config.audio.block_ms = 30
    config.audio.barge_in_grace_ms = 60
    config.audio.barge_in_trigger_ms = 90
    config.audio.barge_in_pre_roll_ms = 60
    config.audio.barge_in_min_utterance_ms = 90
    config.audio.silence_ms = 90
    backend = UIBackend(
        config, EventEmitter(), AssistantStore(tmp_path / "assistant.sqlite3")
    )
    cancel_observed_at: list[float] = []

    class FakeAssistant:
        def answer(self, text, *, on_token, on_phase, cancel_event, echo, **kwargs):  # noqa: ANN001
            del text, echo
            on_phase("thinking")
            on_token("часть ответа")
            on_phase("speaking")
            while not cancel_event.is_set():
                time.sleep(0.001)
            cancel_observed_at.append(time.perf_counter())
            # Model the bounded cleanup of the old TTS/playback worker.  The
            # next-turn clock must not be reset after this delay.
            time.sleep(0.04)
            return "часть ответа"

    class FakeMicrophone:
        def __init__(self) -> None:
            echo = [np.full(480, 0.02, dtype=np.float32) for _ in range(3)]
            voice = [
                np.full(480, level, dtype=np.float32)
                for level in (0.11, 0.12, 0.13, 0.14, 0.15)
            ]
            silence = [np.zeros(480, dtype=np.float32) for _ in range(3)]
            self.blocks = iter(echo + voice + silence)

        def read_block(self, timeout=0.1):  # noqa: ANN001
            del timeout
            return next(self.blocks, None)

    backend.assistant = FakeAssistant()
    turn = backend.orchestrator.prepare_turn(
        "первый вопрос",
        workspace_id=backend.current_workspace_id,
        task_id=None,
        spoken=True,
    )
    captured = backend._answer_with_barge_in(turn, FakeMicrophone(), 0.01)
    returned_at = time.perf_counter()
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]

    assert captured is not None
    captured_blocks = captured.audio.reshape(-1, 480)
    assert any(np.isclose(np.mean(item), 0.11) for item in captured_blocks)
    assert captured.response_started_at <= captured.detected_at
    assert cancel_observed_at
    assert captured.response_started_at <= cancel_observed_at[0]
    assert returned_at - captured.response_started_at >= 0.035
    assert any(event["type"] == "interrupted" for event in events)
    assert any(
        event["type"] == "assistant_end" and event["interrupted"] is True
        for event in events
    )
    interrupted_end = next(
        event
        for event in events
        if event["type"] == "assistant_end" and event["interrupted"] is True
    )
    assert interrupted_end["spoken"] is False
    assert interrupted_end["tts_error"] is None
    assert not any(event["type"] == "speech_error" for event in events)


def test_voice_loop_keeps_interrupt_timestamp_for_follow_up_recovery(
    monkeypatch,
    tmp_path,
) -> None:
    config = Config.defaults()
    config.audio.block_ms = 30
    config.audio.silence_ms = 90
    backend = UIBackend(
        config,
        EventEmitter(),
        AssistantStore(tmp_path / "assistant.sqlite3"),
    )
    sample = np.concatenate(
        [
            np.full(480 * 4, 0.1, dtype=np.float32),
            np.zeros(480 * 3, dtype=np.float32),
        ]
    )

    class FakeMicrophone:
        def __init__(self, _config) -> None:  # noqa: ANN001
            self.listened = False

        def __enter__(self):  # noqa: ANN204
            return self

        def __exit__(self, *_args) -> None:  # noqa: ANN002
            return None

        @staticmethod
        def calibrate() -> float:
            return 0.01

        def listen(self, _cancel_event):  # noqa: ANN001
            if self.listened:
                return None
            self.listened = True
            return sample

        @staticmethod
        def discard_pending() -> None:
            return None

    class FakeSTT:
        calls = 0

        def transcribe(self, _audio, _sample_rate):  # noqa: ANN001
            self.calls += 1
            return f"реплика {self.calls}"

    monkeypatch.setattr(ui_backend_module, "Microphone", FakeMicrophone)
    backend.assistant.stt = FakeSTT()
    monkeypatch.setattr(backend, "_try_deterministic_request", lambda *_a, **_k: False)

    recovery_origin = time.perf_counter() - 0.4
    calls: list[tuple[float | None, str | None]] = []

    def fake_answer(
        _turn,
        _microphone,
        _threshold,
        *,
        response_started_at=None,
        response_timing_origin=None,
        stt_seconds=None,
    ):
        del stt_seconds
        calls.append((response_started_at, response_timing_origin))
        if len(calls) == 1:
            return ui_backend_module._PendingVoiceInput(
                audio=sample,
                detected_at=time.perf_counter(),
                response_started_at=recovery_origin,
            )
        backend.stop_event.set()
        return None

    monkeypatch.setattr(backend, "_answer_with_barge_in", fake_answer)

    backend._voice_loop()

    assert len(calls) == 2
    assert calls[0][1] == "estimated_speech_end"
    assert calls[1] == (recovery_origin, "barge_in_interrupt")


def test_voice_loop_uses_immediate_adaptive_threshold(monkeypatch, capsys, tmp_path) -> None:
    config = Config.defaults()
    backend = UIBackend(
        config,
        EventEmitter(),
        AssistantStore(tmp_path / "assistant.sqlite3"),
    )
    calls: list[str] = []

    class FakeMicrophone:
        def __init__(self, _config) -> None:  # noqa: ANN001
            pass

        def __enter__(self):  # noqa: ANN204
            return self

        def __exit__(self, *_args) -> None:  # noqa: ANN002
            return None

        @staticmethod
        def start_adaptive() -> float:
            calls.append("adaptive")
            return 0.012

        @staticmethod
        def calibrate() -> float:
            raise AssertionError("compact voice must not consume a calibration second")

        @staticmethod
        def listen(stop_event):  # noqa: ANN001
            stop_event.set()
            return None

    monkeypatch.setattr(ui_backend_module, "Microphone", FakeMicrophone)

    backend._voice_loop()

    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert calls == ["adaptive"]
    calibrated = next(event for event in events if event["type"] == "calibrated")
    assert calibrated == {"type": "calibrated", "threshold": 0.012, "mode": "adaptive"}


def test_cancelled_pending_voice_start_never_reactivates_session(
    monkeypatch, capsys, tmp_path
) -> None:
    backend = UIBackend(
        Config.defaults(),
        EventEmitter(),
        AssistantStore(tmp_path / "assistant.sqlite3"),
    )

    class CancelledDuringOpenMicrophone:
        def __init__(self, _config) -> None:  # noqa: ANN001
            pass

        def __enter__(self):  # noqa: ANN204
            backend.stop_event.set()
            return self

        def __exit__(self, *_args) -> None:  # noqa: ANN002
            return None

        @staticmethod
        def start_adaptive() -> float:
            return 0.012

    monkeypatch.setattr(
        ui_backend_module,
        "Microphone",
        CancelledDuringOpenMicrophone,
    )

    backend._voice_loop()

    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert not any(event["type"] == "calibrated" for event in events)
    assert events[-1]["type"] == "session_stopped"


def test_text_cancellation_remains_interrupted_without_speech_error(
    capsys, tmp_path
) -> None:
    store = AssistantStore(tmp_path / "assistant.sqlite3")
    backend = UIBackend(Config.defaults(), EventEmitter(), store)

    class CancelledAssistant:
        @staticmethod
        def answer(text, *, on_token, on_phase, cancel_event, **kwargs):  # noqa: ANN001
            del text, kwargs
            on_phase("thinking")
            on_token("Часть ответа")
            cancel_event.set()
            return "Часть ответа"

    backend.assistant = CancelledAssistant()
    backend._text_turn("Останови ответ", speak=True)

    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    answer = next(event for event in events if event["type"] == "assistant_end")
    task = store.get_task(backend.current_task_id)
    assert task["status"] == "needs_user"
    assert answer["interrupted"] is True
    assert answer["tts_error"] is None
    assert not any(event["type"] == "speech_error" for event in events)


def test_text_turn_failure_persists_error_event_and_audit(capsys, tmp_path) -> None:
    store = AssistantStore(tmp_path / "assistant.sqlite3")
    backend = UIBackend(Config.defaults(), EventEmitter(), store)

    class FailingAssistant:
        def answer(self, text, **kwargs):  # noqa: ANN001
            del text, kwargs
            raise RuntimeError("детерминированный сбой модели")

    backend.assistant = FailingAssistant()
    backend._text_turn("Проверь обработку ошибки", speak=False)

    task = store.get_task(backend.current_task_id)
    task_events = store._rows(
        "SELECT * FROM task_events WHERE task_id = ? ORDER BY created_at",
        (task["id"],),
    )
    audit = store._rows(
        "SELECT * FROM audit_log WHERE task_id = ? ORDER BY created_at",
        (task["id"],),
    )
    emitted = [json.loads(line) for line in capsys.readouterr().out.splitlines()]

    assert task["status"] == "error"
    assert task["result"] == "детерминированный сбой модели"
    assert any(event["kind"] == "error" for event in task_events)
    assert any(
        event["action"] == "task.execute" and event["status"] == "error"
        for event in audit
    )
    assert any(
        event["type"] == "error" and "детерминированный сбой" in event["message"]
        for event in emitted
    )


def test_voice_turn_failure_marks_running_task_as_error(monkeypatch, tmp_path) -> None:
    store = AssistantStore(tmp_path / "assistant.sqlite3")
    backend = UIBackend(Config.defaults(), EventEmitter(), store)

    class FakeSTT:
        @staticmethod
        def transcribe(audio, sample_rate):  # noqa: ANN001
            del audio, sample_rate
            return "Голосовой запрос с ошибкой"

    class FailingAssistant:
        stt = FakeSTT()

        @staticmethod
        def answer(text, **kwargs):  # noqa: ANN001
            del text, kwargs
            raise RuntimeError("сбой голосового ответа")

    class FakeMicrophone:
        def __init__(self, config) -> None:  # noqa: ANN001
            del config

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):  # noqa: ANN001
            del exc_type, exc, traceback

        @staticmethod
        def calibrate() -> float:
            return 0.01

        @staticmethod
        def listen(stop_event):  # noqa: ANN001
            del stop_event
            return np.ones(1600, dtype=np.float32)

        @staticmethod
        def read_block(timeout=0.1):  # noqa: ANN001
            del timeout
            return None

        @staticmethod
        def discard_pending() -> None:
            return None

    monkeypatch.setattr(ui_backend_module, "Microphone", FakeMicrophone)
    backend.assistant = FailingAssistant()

    backend._voice_loop()

    task = store.get_task(backend.current_task_id)
    assert task["status"] == "error"
    assert task["result"] == "сбой голосового ответа"


def test_voice_dictation_can_stop_for_editable_review(
    monkeypatch, capsys, tmp_path
) -> None:
    store = AssistantStore(tmp_path / "assistant.sqlite3")
    store.set_setting("voice_review_before_send", "true")
    backend = UIBackend(Config.defaults(), EventEmitter(), store)
    task_count_before = len(store._rows("SELECT id FROM tasks"))

    class FakeSTT:
        @staticmethod
        def transcribe(audio, sample_rate):  # noqa: ANN001
            del audio, sample_rate
            return "Отредактируй этот распознанный запрос"

    class ReviewAssistant:
        stt = FakeSTT()

        @staticmethod
        def answer(*args, **kwargs):  # noqa: ANN002, ANN003
            raise AssertionError("До подтверждения диктовки LLM вызываться не должна")

    class FakeMicrophone:
        def __init__(self, config) -> None:  # noqa: ANN001
            del config

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):  # noqa: ANN001
            del exc_type, exc, traceback

        @staticmethod
        def calibrate() -> float:
            return 0.01

        @staticmethod
        def listen(stop_event):  # noqa: ANN001
            del stop_event
            return np.ones(1600, dtype=np.float32)

        @staticmethod
        def discard_pending() -> None:
            return None

    monkeypatch.setattr(ui_backend_module, "Microphone", FakeMicrophone)
    backend.assistant = ReviewAssistant()

    backend._voice_loop()

    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    preview = next(event for event in events if event["type"] == "dictation_ready")
    assert preview["text"] == "Отредактируй этот распознанный запрос"
    assert preview["editable"] is True
    assert any(event["type"] == "session_stopped" for event in events)
    assert len(store._rows("SELECT id FROM tasks")) == task_count_before


def test_turn_events_expose_durable_source_reference_contract(capsys, tmp_path) -> None:
    store = AssistantStore(tmp_path / "assistant.sqlite3")
    workspace = store.default_workspace_id()
    source = store.add_source(
        workspace,
        "document",
        "Проект Аргон",
        "Аргон находится на стадии локального пилота.",
        path="/tmp/argon.md",
    )
    capsys.readouterr()
    backend = UIBackend(Config.defaults(), EventEmitter(), store)

    class FakeAssistant:
        @staticmethod
        def answer(text, *, on_token, on_phase, **kwargs):  # noqa: ANN001
            del text, kwargs
            on_phase("thinking")
            on_token("Пилот идёт.")
            return "Пилот идёт."

    backend.assistant = FakeAssistant()
    turn = backend._prepare_turn("Что с проектом Аргон?", spoken=False)
    retrieved = turn.sources[0]
    backend._answer(turn, cancel_event=threading.Event(), speak=False)
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    expected = {
        "id": source["id"],
        "title": source["title"],
        "kind": source["kind"],
        "path": source["path"],
        "chunk_id": retrieved["chunk_id"],
        "char_start": retrieved["char_start"],
        "char_end": retrieved["char_end"],
        "excerpt": retrieved["excerpt"],
        "selection": retrieved["selection"],
    }

    for event_type in ("task_context", "assistant_start"):
        event = next(item for item in events if item["type"] == event_type)
        assert event["sources"] == [expected]


def test_snapshot_selects_meeting_and_exposes_ranked_source_linked_attention(
    capsys, tmp_path
) -> None:
    store = AssistantStore(tmp_path / "assistant.sqlite3")
    workspace = store.default_workspace_id()
    transcript = (
        "Анна: Тема: Пилот.\n"
        "Иван: Решили продолжать пилот.\n"
        "Анна: Иван подготовит смету до 2020-01-01.\n"
        "Олег: Риск: поставка задерживается."
    )
    source = store.add_source(
        workspace,
        "meeting",
        "Статус пилота",
        transcript,
        path="/tmp/status-pilot.md",
    )
    meeting = store.analyze_meeting(source["id"], occurred_at="2019-12-20")
    backend = UIBackend(Config.defaults(), EventEmitter(), store)

    backend.emit_snapshot()

    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    snapshot = [event for event in events if event["type"] == "snapshot"][-1]["data"]
    assert snapshot["current_meeting_id"] == meeting["id"]
    assert snapshot["meeting_items"]
    assert snapshot["meetings"][0]["item_counts"]["decision"] == 1
    assert snapshot["meetings"][0]["open_attention"] >= 2
    assert snapshot["attention_events"][0]["severity"] == "critical"
    assert snapshot["attention_events"][0]["source_id"] == source["id"]
    assert snapshot["attention_events"][0]["source_path"] == source["path"]
    assert snapshot["today"]["attention"] == len(snapshot["attention_events"])


def test_meeting_ui_commands_change_status_and_prepare_briefing(capsys, tmp_path) -> None:
    store = AssistantStore(tmp_path / "assistant.sqlite3")
    workspace = store.default_workspace_id()
    source = store.add_source(
        workspace,
        "meeting",
        "Проектный комитет",
        "Анна: Тема: Альфа.\nИван: Нужно подготовить смету завтра.\nАнна: Кто согласует бюджет?",
        path="/tmp/alpha.md",
    )
    meeting = store.analyze_meeting(source["id"], occurred_at="2026-08-29")
    action = next(item for item in meeting["items"] if item["kind"] == "action")
    backend = UIBackend(Config.defaults(), EventEmitter(), store)

    backend.handle(
        {"command": "meeting_item_status", "item_id": action["id"], "status": "done"}
    )
    assert store.update_meeting_item_status(action["id"], "open")["status"] == "open"
    backend.handle(
        {"command": "meeting_item_status", "item_id": action["id"], "status": "done"}
    )
    backend.handle({"command": "prepare_briefing", "meeting_id": meeting["id"]})

    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    snapshot = [event for event in events if event["type"] == "snapshot"][-1]["data"]
    assert next(item for item in snapshot["meeting_items"] if item["id"] == action["id"])[
        "status"
    ] == "done"
    assert "Брифинг к следующей встрече" in snapshot["meeting_briefing"]
    assert "Открытые вопросы" in snapshot["meeting_briefing"]


def test_attention_question_uses_deterministic_local_answer_and_tts(
    capsys, tmp_path
) -> None:
    store = AssistantStore(tmp_path / "assistant.sqlite3")
    workspace = store.default_workspace_id()
    store.create_task(workspace, "Нужен выбор")
    task = store._rows("SELECT * FROM tasks ORDER BY created_at DESC LIMIT 1")[0]
    store.update_task(task["id"], status="needs_user")
    backend = UIBackend(Config.defaults(), EventEmitter(), store)
    played: list[str] = []

    class FakeTTS:
        @staticmethod
        def synthesize(text, cancel_event=None):  # noqa: ANN001
            del cancel_event
            played.append(text)
            yield np.zeros(24, dtype=np.float32), 24_000

    class FakePlayer:
        @staticmethod
        def play(chunks, cancel_event=None, on_start=None, on_block=None):  # noqa: ANN001
            del cancel_event, on_block
            if on_start:
                on_start()
            list(chunks)

    class FakeAssistant:
        tts = FakeTTS()
        player = FakePlayer()

        @staticmethod
        def answer(*args, **kwargs):  # noqa: ANN002, ANN003
            raise AssertionError("LLM не должна вызываться для локального Attention-ответа")

    backend.assistant = FakeAssistant()
    backend._text_turn("Что требует моего внимания сейчас?", speak=True)

    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    answer = next(event for event in events if event["type"] == "assistant_end")
    assert "Нужен выбор" in answer["text"]
    assert played == [concise_speech_text(answer["text"])]
    assert answer["spoken_text"] == played[0]
    assert store.get_task(backend.current_task_id)["status"] == "done"


def test_attention_tts_failure_is_nonfatal(capsys, tmp_path) -> None:
    store = AssistantStore(tmp_path / "assistant.sqlite3")
    workspace = store.default_workspace_id()
    task = store.create_task(workspace, "Нужен выбор")
    store.update_task(task["id"], status="needs_user")
    backend = UIBackend(Config.defaults(), EventEmitter(), store)

    class FailingTTS:
        @staticmethod
        def synthesize(text, cancel_event=None):  # noqa: ANN001
            del text, cancel_event
            raise RuntimeError("OmniVoice не ответил")
            yield  # pragma: no cover

    class ConsumingPlayer:
        @staticmethod
        def play(chunks, **kwargs):  # noqa: ANN001
            del kwargs
            list(chunks)

    class FakeAssistant:
        tts = FailingTTS()
        player = ConsumingPlayer()

        @staticmethod
        def answer(*args, **kwargs):  # noqa: ANN002, ANN003
            raise AssertionError("LLM не должна вызываться")

    backend.assistant = FakeAssistant()
    backend._text_turn("Что требует моего внимания сейчас?", speak=True)

    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    answer = next(event for event in events if event["type"] == "assistant_end")
    assert "Нужен выбор" in answer["text"]
    assert answer["spoken"] is False
    assert answer["tts_error"] == "OmniVoice не ответил"
    assert store.get_task(backend.current_task_id)["status"] == "done"
    assert any(event["type"] == "speech_error" for event in events)
    assert not any(event["type"] == "error" for event in events)


def test_retry_speech_replays_latest_message_without_new_llm_turn_or_db_write(
    capsys, tmp_path
) -> None:
    store = AssistantStore(tmp_path / "assistant.sqlite3")
    task = store.create_task(store.default_workspace_id(), "Готовый ответ")
    store.update_task(task["id"], status="done", result="Последний ответ")
    store.add_message(task["id"], "assistant", "Последний ответ")
    backend = UIBackend(Config.defaults(), EventEmitter(), store)
    backend.current_task_id = task["id"]
    synthesized: list[str] = []

    class FakeTTS:
        @staticmethod
        def synthesize(text, cancel_event=None):  # noqa: ANN001
            del cancel_event
            synthesized.append(text)
            yield np.zeros(24, dtype=np.float32), 24_000

    class FakePlayer:
        @staticmethod
        def play(chunks, cancel_event=None, on_start=None):  # noqa: ANN001
            del cancel_event
            if on_start:
                on_start()
            list(chunks)

    class FakeAssistant:
        tts = FakeTTS()
        player = FakePlayer()

        @staticmethod
        def answer(*args, **kwargs):  # noqa: ANN002, ANN003
            raise AssertionError("Повтор речи не должен вызывать LLM")

    backend.assistant = FakeAssistant()
    messages_before = store.messages(task["id"])
    task_before = store.get_task(task["id"])

    backend._retry_speech_turn()

    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    recovered = next(event for event in events if event["type"] == "speech_recovered")
    assert synthesized == ["Последний ответ"]
    assert recovered["text"] == "Последний ответ"
    assert recovered["task_id"] == task["id"]
    assert not any(event["type"] == "speech_error" for event in events)
    assert store.messages(task["id"]) == messages_before
    assert store.get_task(task["id"]) == task_before


def test_retry_speech_failure_emits_nonfatal_error(capsys, tmp_path) -> None:
    store = AssistantStore(tmp_path / "assistant.sqlite3")
    task = store.create_task(store.default_workspace_id(), "Готовый ответ")
    store.update_task(task["id"], status="done", result="Последний ответ")
    store.add_message(task["id"], "assistant", "Последний ответ")
    backend = UIBackend(Config.defaults(), EventEmitter(), store)
    backend.current_task_id = task["id"]

    class FailingTTS:
        @staticmethod
        def synthesize(text, cancel_event=None):  # noqa: ANN001
            del text, cancel_event
            raise RuntimeError("повтор TTS не удался")
            yield  # pragma: no cover

    class ConsumingPlayer:
        @staticmethod
        def play(chunks, **kwargs):  # noqa: ANN001
            del kwargs
            list(chunks)

    class FakeAssistant:
        tts = FailingTTS()
        player = ConsumingPlayer()

    backend.assistant = FakeAssistant()
    backend._retry_speech_turn()

    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    error = next(event for event in events if event["type"] == "speech_error")
    assert error["message"] == "повтор TTS не удался"
    assert error["task_id"] == task["id"]
    assert error["retryable"] is True
    assert not any(event["type"] == "speech_recovered" for event in events)
    assert not any(event["type"] == "error" for event in events)
    assert store.get_task(task["id"])["status"] == "done"


def test_configure_external_llm_emits_runtime_and_never_persists_key(
    capsys,
    tmp_path,
) -> None:
    data_path = tmp_path / "assistant.sqlite3"
    store = AssistantStore(data_path)
    backend = UIBackend(Config.defaults(), EventEmitter(), store)
    backend.assistant.local_chat.model = object()
    backend.assistant.local_chat.tokenizer = object()

    backend.handle(
        {
            "command": "configure_llm",
            "mode": "external",
            "base_url": "https://api.example.com/v1/chat/completions",
            "model": "corp-model",
            "api_key": "sk-memory-only",
        }
    )

    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    configured = next(event for event in events if event["type"] == "llm_configured")
    snapshot = [event for event in events if event["type"] == "snapshot"][-1]["data"]
    settings = store.settings()
    all_persisted = json.dumps(settings, ensure_ascii=False)
    all_events = json.dumps(events, ensure_ascii=False)

    assert configured["mode"] == "external"
    assert configured["base_url"] == "https://api.example.com/v1"
    assert configured["model"] == "corp-model"
    assert configured["ready"] is True
    assert configured["runtime"]["local_fallback_ready"] is True
    assert snapshot["llm"]["mode"] == "external"
    assert snapshot["llm"]["has_api_key"] is True
    assert snapshot["model"] == "corp-model · внешний API"
    assert snapshot["settings"]["llm_mode"] == "external"
    assert snapshot["settings"]["external_llm_base_url"] == "https://api.example.com/v1"
    assert settings["model_mode"] == "external"
    assert settings["llm_base_url"] == "https://api.example.com/v1"
    assert settings["llm_model"] == "corp-model"
    assert "sk-memory-only" not in all_persisted
    assert "sk-memory-only" not in all_events
    route_audit = store._rows(
        "SELECT * FROM audit_log WHERE action='llm.configure' ORDER BY created_at DESC"
    )[0]
    assert route_audit["target"] == "external"
    assert route_audit["status"] == "success"
    assert "corp-model" in route_audit["detail"]
    assert "sk-memory-only" not in json.dumps(route_audit, ensure_ascii=False)

    store.close()
    restored = UIBackend(
        Config.defaults(),
        EventEmitter(),
        AssistantStore(data_path),
    )
    restored_runtime = restored._llm_runtime()
    assert restored_runtime["mode"] == "external"
    assert restored_runtime["status"] == "needs_api_key"
    assert restored_runtime["ready"] is False
    assert restored.assistant.chat is not restored.assistant.local_chat


def test_remote_external_llm_without_key_is_rejected_without_switching(
    capsys,
    tmp_path,
) -> None:
    store = AssistantStore(tmp_path / "assistant.sqlite3")
    backend = UIBackend(Config.defaults(), EventEmitter(), store)
    local = backend.assistant.local_chat

    backend.handle(
        {
            "command": "configure_llm",
            "mode": "external",
            "base_url": "https://api.example.com/v1",
            "model": "remote-model",
            "api_key": "",
        }
    )

    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    error = next(
        event for event in events if event["type"] == "llm_configuration_error"
    )
    assert "API-ключ" in error["message"]
    assert error["retryable"] is True
    assert backend.assistant.chat is local
    assert store.settings()["model_mode"] == "local"
    assert not any(event["type"] == "llm_configured" for event in events)


def test_loopback_external_llm_needs_no_key_and_can_switch_back_local(
    capsys,
    tmp_path,
) -> None:
    store = AssistantStore(tmp_path / "assistant.sqlite3")
    backend = UIBackend(Config.defaults(), EventEmitter(), store)
    local = backend.assistant.local_chat

    backend.handle(
        {
            "command": "configure_llm",
            "mode": "external",
            "base_url": "http://127.0.0.1:11434/v1",
            "model": "local-server-model",
        }
    )
    assert backend._llm_runtime()["ready"] is True
    assert backend._llm_runtime()["has_api_key"] is False
    assert backend.assistant.chat is not local

    backend.handle({"command": "configure_llm", "mode": "local"})
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    configured = [event for event in events if event["type"] == "llm_configured"][-1]
    assert configured["mode"] == "local"
    assert backend.assistant.chat is local
    assert store.settings()["model_mode"] == "local"


def test_external_workspace_consent_is_bound_and_resets_on_route_or_workspace_change(
    capsys,
    tmp_path,
) -> None:
    store = AssistantStore(tmp_path / "assistant.sqlite3")
    backend = UIBackend(Config.defaults(), EventEmitter(), store)
    first_workspace = backend.current_workspace_id
    second_workspace = store.create_workspace("Второе пространство")["id"]

    backend.handle(
        {
            "command": "configure_llm",
            "mode": "external",
            "base_url": "https://provider-a.example/v1",
            "model": "model-a",
            "api_key": "secret-a",
        }
    )
    backend.handle(
        {
            "command": "setting",
            "key": "external_context_scope",
            "value": "workspace",
        }
    )
    scoped = store.settings()
    assert scoped["external_context_scope"] == "workspace"
    assert scoped["external_context_scope_endpoint"] == "https://provider-a.example/v1"
    assert scoped["external_context_scope_workspace"] == first_workspace

    backend.handle({"command": "select_workspace", "id": second_workspace})
    workspace_reset = store.settings()
    assert workspace_reset["external_context_scope"] == "task"
    assert workspace_reset["external_context_scope_endpoint"] == ""
    assert workspace_reset["external_context_scope_workspace"] == ""

    backend.handle(
        {
            "command": "setting",
            "key": "external_context_scope",
            "value": "workspace",
        }
    )
    backend.handle(
        {
            "command": "configure_llm",
            "mode": "external",
            "base_url": "https://provider-b.example/v1",
            "model": "model-b",
            "api_key": "secret-b",
        }
    )
    route_reset = store.settings()
    assert route_reset["external_context_scope"] == "task"
    assert route_reset["external_context_scope_endpoint"] == ""
    assert route_reset["external_context_scope_workspace"] == ""
    assert "secret-a" not in capsys.readouterr().out


def test_configure_llm_is_rejected_while_task_is_active(capsys, tmp_path) -> None:
    backend = UIBackend(
        Config.defaults(),
        EventEmitter(),
        AssistantStore(tmp_path / "assistant.sqlite3"),
    )
    backend.task_lock.acquire()
    try:
        backend.handle({"command": "configure_llm", "mode": "local"})
    finally:
        backend.task_lock.release()

    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert events == [
        {
            "type": "llm_configuration_error",
            "message": "Дождитесь завершения текущей задачи перед сменой модели",
            "retryable": True,
        }
    ]


def test_generic_settings_command_refuses_api_keys(tmp_path) -> None:
    store = AssistantStore(tmp_path / "assistant.sqlite3")
    backend = UIBackend(Config.defaults(), EventEmitter(), store)

    with pytest.raises(ValueError, match="Секреты"):
        backend.handle(
            {"command": "setting", "key": "llm_api_key", "value": "secret"}
        )

    assert "secret" not in json.dumps(store.settings())


def test_background_automation_stays_on_local_llm_in_external_chat_mode(
    capsys,
    tmp_path,
) -> None:
    store = AssistantStore(tmp_path / "assistant.sqlite3")
    workspace = store.default_workspace_id()
    core = FakeCorePolicy()
    backend = UIBackend(
        Config.defaults(),
        EventEmitter(),
        store,
        core_policy=core,
    )
    store.set_settings(
        {
            "model_mode": "external",
            "external_context_scope": "task",
            "memory_enabled": "true",
        }
    )
    store.remember(
        "Контекст автоматизации",
        "МАРКЕР_ЛОКАЛЬНОЙ_ПАМЯТИ",
        workspace_id=workspace,
    )
    store.add_source(
        workspace,
        "document",
        "Кобальтовый отчёт",
        "МАРКЕР_ЛОКАЛЬНОГО_ИСТОЧНИКА: отчёт готов.",
    )
    automation = store.create_automation(
        workspace,
        "Локальная проверка",
        "Проверь кобальтовый отчёт",
        "при изменении контекста",
    )
    class RecordingChat:
        def __init__(self, reply: str) -> None:
            self.reply = reply
            self.prompts: list[str] = []
            self.model = object()
            self.tokenizer = object()

        def stream_reply(self, user_text, *, history=None):  # noqa: ANN001
            del history
            self.prompts.append(user_text)
            yield self.reply

        @staticmethod
        def remember(user_text, assistant_text):  # noqa: ANN001
            del user_text, assistant_text

    local = RecordingChat("Локальная автоматизация завершена")
    external = RecordingChat("Внешняя модель не должна вызываться")
    backend._local_chat = local  # type: ignore[assignment]
    backend.assistant.local_chat = local  # type: ignore[assignment]
    backend.assistant.chat = external  # type: ignore[assignment]

    backend._run_automation(automation)
    capsys.readouterr()

    assert external.prompts == []
    assert len(local.prompts) == 1
    prompt = local.prompts[0]
    assert "Политика данных: используй только локальный внутренний контекст" in prompt
    assert "Политика данных внешней модели" not in prompt
    assert "МАРКЕР_ЛОКАЛЬНОЙ_ПАМЯТИ" in prompt
    assert "МАРКЕР_ЛОКАЛЬНОГО_ИСТОЧНИКА" in prompt
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
    assert "кобальтовый" not in json.dumps(core.calls, ensure_ascii=False).casefold()
    completed = store._rows(
        "SELECT * FROM tasks WHERE result='Локальная автоматизация завершена'"
    )
    assert len(completed) == 1


def test_busy_voice_start_restores_stopped_state(capsys, tmp_path) -> None:
    backend = UIBackend(
        Config.defaults(),
        EventEmitter(),
        AssistantStore(tmp_path / "assistant.sqlite3"),
    )
    backend.task_lock.acquire()
    try:
        backend.start_session()
    finally:
        backend.task_lock.release()

    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert events == [
        {"type": "error", "message": "Дождитесь завершения текущего ответа"},
        {"type": "session_stopped"},
    ]
    assert backend.session_thread is None


def test_meeting_audio_command_transcribes_imports_and_selects_meeting(
    capsys,
    tmp_path,
) -> None:
    store = AssistantStore(tmp_path / "assistant.sqlite3")
    backend = UIBackend(Config.defaults(), EventEmitter(), store)
    audio = tmp_path / "status.m4a"
    audio.write_bytes(b"audio")
    transcript = (
        "Анна: Тема: Пилот. Иван: Решили продолжать. "
        "Анна: Иван подготовит отчёт до 12 сентября."
    )

    class FakeSTT:
        @staticmethod
        def transcribe_file(path):  # noqa: ANN001
            assert path == audio
            return transcript

    backend.assistant.stt = FakeSTT()
    backend.handle(
        {
            "command": "import_meeting_audio",
            "path": str(audio),
            "workspace_id": store.default_workspace_id(),
        }
    )
    assert backend.audio_import_thread is not None
    backend.audio_import_thread.join(timeout=3)
    assert not backend.audio_import_thread.is_alive()

    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    event_types = [event["type"] for event in events]
    assert event_types[:3] == [
        "audio_import_started",
        "transcription_started",
        "transcription_completed",
    ]
    completed = next(event for event in events if event["type"] == "audio_import_completed")
    assert completed["transcript_chars"] == len(transcript)
    assert completed["meeting_id"] == backend.current_meeting_id
    assert any(event["type"] == "source_imported" for event in events)
    assert events[-1]["type"] == "snapshot"
    assert store.get_meeting(backend.current_meeting_id)["status"] == "analyzed"


def test_delete_commands_emit_contract_and_replace_current_selection(
    capsys,
    tmp_path,
) -> None:
    store = AssistantStore(tmp_path / "assistant.sqlite3")
    store.trash_dir = tmp_path / "Trash"
    workspace = store.default_workspace_id()
    fallback_task = store.create_task(workspace, "Оставить")
    deleted_task = store.create_task(workspace, "Удалить")
    backend = UIBackend(Config.defaults(), EventEmitter(), store)
    backend.current_task_id = deleted_task["id"]

    backend.handle({"command": "delete_task", "task_id": deleted_task["id"]})
    task_events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    deleted_event = next(event for event in task_events if event["type"] == "entity_deleted")
    assert deleted_event == {
        "type": "entity_deleted",
        "entity_type": "task",
        "entity_id": deleted_task["id"],
        "title": "Удалить",
        "recovery": "database_only",
    }
    assert backend.current_task_id == fallback_task["id"]

    transcript = store.files_dir / "meeting.md"
    transcript.write_text("Решили продолжать пилот.", encoding="utf-8")
    source = store.add_source(
        workspace,
        "meeting",
        "Удаляемая встреча",
        "Решили продолжать пилот.",
        path=str(transcript),
    )
    meeting = store.analyze_meeting(source["id"])
    backend.current_meeting_id = meeting["id"]
    capsys.readouterr()
    backend.handle({"command": "delete_source", "source_id": source["id"]})
    source_events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    source_deleted = next(
        event for event in source_events if event["type"] == "entity_deleted"
    )
    assert source_deleted["entity_type"] == "source"
    assert source_deleted["recovery"] == "trash"
    assert backend.current_meeting_id is None

    artifact = store.create_artifact(workspace, None, "Удаляемый материал", "Текст")
    capsys.readouterr()
    backend.handle({"command": "delete_artifact", "artifact_id": artifact["id"]})
    artifact_events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    artifact_deleted = next(
        event for event in artifact_events if event["type"] == "entity_deleted"
    )
    assert artifact_deleted["entity_type"] == "artifact"
    assert artifact_deleted["recovery"] == "trash"


def test_remote_failure_before_first_token_falls_back_locally_without_mode_change(
    capsys,
    tmp_path,
) -> None:
    store = AssistantStore(tmp_path / "assistant.sqlite3")
    store.set_settings(
        {
            "model_mode": "external",
            "llm_base_url": "https://corporate.example/v1",
            "llm_model": "corp-model",
            "external_provider_type": "corporate",
        }
    )
    backend = UIBackend(Config.defaults(), EventEmitter(), store)

    class LocalRuntime:
        model = object()
        tokenizer = object()

    class RemoteRuntime:
        ready = True
        base_url = "https://corporate.example/v1"
        model_name = "corp-model"

    local = LocalRuntime()
    remote = RemoteRuntime()
    backend._local_chat = local  # type: ignore[assignment]
    backend._remote_chat = remote  # type: ignore[assignment]
    calls: list[object] = []

    class FallbackAssistant:
        @staticmethod
        def answer(prompt, *, chat_backend, on_token, on_phase, on_speech_text, **kwargs):  # noqa: ANN001
            del kwargs
            calls.append(chat_backend)
            on_phase("thinking")
            if chat_backend is remote:
                raise RuntimeError("SECRET_REMOTE_RESPONSE_BODY")
            assert "формируется локальной моделью" in prompt
            on_token("Локальный ответ")
            on_speech_text("")
            return "Локальный ответ"

    backend.assistant = FallbackAssistant()
    turn = backend._prepare_turn("Подготовь сводку", spoken=False)
    capsys.readouterr()

    backend._answer(turn, cancel_event=threading.Event(), speak=False)

    output = capsys.readouterr().out
    events = [json.loads(line) for line in output.splitlines()]
    fallback = next(event for event in events if event["type"] == "routing_fallback")
    finished = next(event for event in events if event["type"] == "assistant_end")
    assert calls == [remote, local]
    assert fallback["from_route"] == "corporate_api"
    assert fallback["to_route"] == "local_mlx"
    assert finished["llm_route"]["fallback_used"] is True
    assert finished["llm_route"]["actual_route"] == "local_mlx"
    assert finished["performance"]["first_token_seconds"] >= 0
    assert store.settings()["model_mode"] == "external"
    message = store.messages(turn.task_id)[-1]
    metadata = json.loads(message["metadata"])
    assert metadata["llm_route"]["fallback_used"] is True
    assert metadata["performance"]["total_seconds"] >= 0
    audit = store._rows(
        "SELECT * FROM audit_log WHERE action='llm.fallback' ORDER BY created_at DESC"
    )[0]
    assert audit["status"] == "success"
    assert "RuntimeError" in audit["detail"]
    assert "SECRET_REMOTE_RESPONSE_BODY" not in output
    assert "SECRET_REMOTE_RESPONSE_BODY" not in json.dumps(audit)


def test_remote_failure_after_partial_response_never_retries_locally(
    capsys,
    tmp_path,
) -> None:
    store = AssistantStore(tmp_path / "assistant.sqlite3")
    store.set_settings(
        {
            "model_mode": "external",
            "llm_base_url": "https://api.example/v1",
            "llm_model": "remote-model",
        }
    )
    backend = UIBackend(Config.defaults(), EventEmitter(), store)

    class LocalRuntime:
        model = object()
        tokenizer = object()

    class RemoteRuntime:
        ready = True
        base_url = "https://api.example/v1"
        model_name = "remote-model"

    local = LocalRuntime()
    remote = RemoteRuntime()
    backend._local_chat = local  # type: ignore[assignment]
    backend._remote_chat = remote  # type: ignore[assignment]
    calls: list[object] = []

    class PartialAssistant:
        @staticmethod
        def answer(prompt, *, chat_backend, on_token, on_phase, **kwargs):  # noqa: ANN001
            del prompt, kwargs
            calls.append(chat_backend)
            on_phase("thinking")
            on_token("Частичный ответ")
            raise RuntimeError(
                "HTTP 502: SECRET_REMOTE_RESPONSE_BODY; "
                "Authorization: Bearer sk-do-not-persist-this"
            )

    backend.assistant = PartialAssistant()
    turn = backend._prepare_turn("Проверь статус", spoken=False)
    capsys.readouterr()

    backend._answer(turn, cancel_event=threading.Event(), speak=False)

    output = capsys.readouterr().out
    events = [json.loads(line) for line in output.splitlines()]
    assert calls == [remote]
    assert not any(event["type"] == "routing_fallback" for event in events)
    assert not store._rows("SELECT * FROM audit_log WHERE action='llm.fallback'")
    finished = next(event for event in events if event["type"] == "assistant_end")
    notice = next(event for event in events if event["type"] == "notice")
    assert finished["text"] == "Частичный ответ"
    assert finished["incomplete"] is True
    assert finished["quick_actions"] == []
    assert finished["llm_route"]["completion_state"] == "incomplete"
    assert finished["llm_route"]["fallback_used"] is False
    assert "полностью" in notice["message"]

    task = store.get_task(turn.task_id)
    assert task["status"] == "needs_user"
    assert task["result"] == "Частичный ответ"
    message = store.messages(turn.task_id)[-1]
    metadata = json.loads(message["metadata"])
    assert message["role"] == "assistant"
    assert message["content"] == "Частичный ответ"
    assert metadata["incomplete"] is True
    assert metadata["llm_route"]["response_incomplete"] is True
    partial_audit = store._rows(
        "SELECT * FROM audit_log WHERE action='llm.partial' ORDER BY created_at DESC"
    )[0]
    assert partial_audit["status"] == "incomplete"

    persisted = json.dumps(
        {
            "task": task,
            "messages": store.messages(turn.task_id),
            "events": store._rows(
                "SELECT * FROM task_events WHERE task_id=?",
                (turn.task_id,),
            ),
            "audit": store._rows("SELECT * FROM audit_log"),
        },
        ensure_ascii=False,
        default=str,
    )
    for secret in (
        "SECRET_REMOTE_RESPONSE_BODY",
        "sk-do-not-persist-this",
        "Bearer sk-do-not-persist-this",
    ):
        assert secret not in output
        assert secret not in persisted


def test_voice_performance_is_measured_from_estimated_end_of_speech(
    capsys,
    tmp_path,
) -> None:
    store = AssistantStore(tmp_path / "assistant.sqlite3")
    backend = UIBackend(Config.defaults(), EventEmitter(), store)

    class LocalRuntime:
        model = object()
        tokenizer = object()

    local = LocalRuntime()
    backend._local_chat = local  # type: ignore[assignment]

    class FastAssistant:
        @staticmethod
        def answer(prompt, *, on_token, on_phase, on_speech_text, **kwargs):  # noqa: ANN001
            del prompt, kwargs
            on_phase("thinking")
            on_token("Готово")
            on_speech_text("")
            return "Готово"

    backend.assistant = FastAssistant()
    turn = backend._prepare_turn("Проверка метрик", spoken=True)
    capsys.readouterr()

    backend._answer(
        turn,
        cancel_event=threading.Event(),
        speak=False,
        response_started_at=time.perf_counter() - 1.0,
        stt_seconds=0.42,
    )

    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    finished = next(event for event in events if event["type"] == "assistant_end")
    performance = finished["performance"]
    assert performance["timing_origin"] == "estimated_speech_end"
    assert performance["stt_seconds"] == 0.42
    assert performance["first_token_seconds"] >= 0.9
    metadata = json.loads(store.messages(turn.task_id)[-1]["metadata"])
    assert metadata["performance"] == performance


def test_deterministic_voice_response_uses_same_concise_speech_policy(
    capsys,
    tmp_path,
) -> None:
    config = Config.defaults()
    config.assistant.max_tts_chars = 80
    config.assistant.max_tts_segments = 1
    store = AssistantStore(tmp_path / "assistant.sqlite3")
    backend = UIBackend(config, EventEmitter(), store)
    task = store.create_task(store.default_workspace_id(), "Дайджест")
    synthesized: list[str] = []

    class FakeTTS:
        @staticmethod
        def synthesize(text, cancel_event=None):  # noqa: ANN001
            del cancel_event
            synthesized.append(text)
            yield np.zeros(120, dtype=np.float32), 24_000

    class FakePlayer:
        @staticmethod
        def play(chunks, *, on_start=None, **kwargs):  # noqa: ANN001
            del kwargs
            if on_start:
                on_start()
            list(chunks)

    backend.assistant.tts = FakeTTS()
    backend.assistant.player = FakePlayer()
    full = "Главный результат готов. Подробное объяснение остаётся только в чате."

    backend._emit_deterministic_response(
        task_id=task["id"],
        user_text="Сделай дайджест",
        reply=full,
        skill="Дайджест",
        sources=[],
        artifact=None,
        speak=True,
        cancel_event=threading.Event(),
        event="digest",
    )

    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    finished = next(event for event in events if event["type"] == "assistant_end")
    assert synthesized == ["Главный результат готов."]
    assert finished["text"] == full
    assert finished["spoken_text"] == "Главный результат готов."
