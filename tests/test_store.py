import json
from pathlib import Path
import sqlite3

import pytest

from voice_assistant.orchestrator import LocalOrchestrator
from voice_assistant.store import SCHEMA_VERSION, AssistantStore


def make_store(tmp_path: Path) -> AssistantStore:
    return AssistantStore(tmp_path / "assistant.sqlite3")


def test_store_seeds_local_workspace_skills_and_capabilities(tmp_path) -> None:
    store = make_store(tmp_path)
    snapshot = store.snapshot()

    assert snapshot["workspaces"][0]["name"] == "Личное пространство"
    assert {skill["command"] for skill in snapshot["skills"]} >= {
        "/research",
        "/meeting",
        "/briefing",
        "/digest",
        "/document",
    }
    connected = {item["id"] for item in snapshot["capabilities"] if item["status"] == "connected"}
    assert {"dictation", "local-search", "documents", "voice"} <= connected


def test_tasks_keep_separate_context_and_persist_messages(tmp_path) -> None:
    store = make_store(tmp_path)
    workspace = store.default_workspace_id()
    first = store.create_task(workspace, "Первая")
    second = store.create_task(workspace, "Вторая")
    store.add_message(first["id"], "user", "Контекст первой")
    store.add_message(second["id"], "user", "Контекст второй")

    assert [item["content"] for item in store.messages(first["id"])] == ["Контекст первой"]
    assert [item["content"] for item in store.messages(second["id"])] == ["Контекст второй"]


def test_source_search_and_citations_context(tmp_path) -> None:
    store = make_store(tmp_path)
    workspace = store.default_workspace_id()
    source = store.add_source(
        workspace,
        "meeting",
        "Встреча Project X",
        "Команда решила перенести релиз на пятницу. Иван отвечает за проверку бюджета.",
    )

    results = store.search_sources("релиз бюджет", workspace_id=workspace)
    context = store.source_context([source["id"]])

    assert results[0]["id"] == source["id"]
    assert "пятницу" in context[0]["excerpt"]


def test_chunk_search_returns_exact_late_offsets_and_prompt_uses_hit(tmp_path) -> None:
    store = make_store(tmp_path)
    workspace = store.default_workspace_id()
    prefix = "СЕКРЕТНЫЙ_СТАРТ. " + ("Вводный материал без целевого факта. " * 90)
    marker = "Кобальтовый бюджет утверждён на четвёртый квартал."
    source = store.add_source(
        workspace,
        "document",
        "Дальний фрагмент",
        prefix + marker,
    )

    results = store.search_sources("кобальтового бюджета", workspace_id=workspace)
    hit = results[0]
    context = store.source_context([hit])

    assert hit["id"] == source["id"]
    assert hit["char_start"] > 0
    assert source["content"][hit["char_start"] : hit["char_end"]] == hit["excerpt"]
    assert context[0]["excerpt"] == hit["excerpt"]
    assert context[0]["char_start"] == hit["char_start"]

    turn = LocalOrchestrator(store).prepare_turn(
        "Проверь кобальтовый бюджет",
        workspace_id=workspace,
        task_id=None,
    )
    assert marker in turn.prompt
    assert "СЕКРЕТНЫЙ_СТАРТ" not in turn.prompt
    assert f"символы {hit['char_start']}:" in turn.prompt


def test_auto_retrieval_is_per_turn_but_explicit_reference_is_durable(tmp_path) -> None:
    store = make_store(tmp_path)
    workspace = store.default_workspace_id()
    source = store.add_source(
        workspace,
        "document",
        "Кобальтовый бюджет",
        "Кобальтовый бюджет утверждён на четвёртый квартал.",
    )
    orchestrator = LocalOrchestrator(store)

    automatic = orchestrator.prepare_turn(
        "Проверь кобальтовый бюджет",
        workspace_id=workspace,
        task_id=None,
    )
    assert source["id"] in {item["id"] for item in automatic.sources}
    assert store.task_sources(automatic.task_id) == []

    unrelated = orchestrator.prepare_turn(
        "Расскажи о погоде Юпитера",
        workspace_id=workspace,
        task_id=automatic.task_id,
    )
    assert source["id"] not in {item["id"] for item in unrelated.sources}
    assert store.task_sources(automatic.task_id) == []

    explicit = orchestrator.prepare_turn(
        "Используй @[Кобальтовый бюджет]",
        workspace_id=workspace,
        task_id=automatic.task_id,
    )
    assert source["id"] in {item["id"] for item in explicit.sources}
    assert [item["id"] for item in store.task_sources(automatic.task_id)] == [source["id"]]

    later = orchestrator.prepare_turn(
        "Продолжай без нового поиска",
        workspace_id=workspace,
        task_id=automatic.task_id,
    )
    assert source["id"] in {item["id"] for item in later.sources}


def test_external_task_scope_excludes_memory_and_automatic_sources(tmp_path) -> None:
    store = make_store(tmp_path)
    workspace = store.default_workspace_id()
    store.set_setting("model_mode", "external")
    store.set_setting("external_context_scope", "task")
    store.set_setting("memory_enabled", "true")
    store.remember(
        "Секретная рабочая память",
        "Маркер ПАМЯТЬ_НЕ_ОТПРАВЛЯТЬ",
        workspace_id=workspace,
    )
    source = store.add_source(
        workspace,
        "document",
        "Кобальтовый бюджет",
        "Маркер АВТОИСТОЧНИК_НЕ_ОТПРАВЛЯТЬ: бюджет утверждён.",
    )
    orchestrator = LocalOrchestrator(store)

    automatic = orchestrator.prepare_turn(
        "Проверь кобальтовый бюджет",
        workspace_id=workspace,
        task_id=None,
    )

    assert automatic.sources == []
    assert "ПАМЯТЬ_НЕ_ОТПРАВЛЯТЬ" not in automatic.prompt
    assert "АВТОИСТОЧНИК_НЕ_ОТПРАВЛЯТЬ" not in automatic.prompt

    explicit = orchestrator.prepare_turn(
        "Используй @[Кобальтовый бюджет]",
        workspace_id=workspace,
        task_id=automatic.task_id,
    )

    assert [item["id"] for item in explicit.sources] == [source["id"]]
    assert "АВТОИСТОЧНИК_НЕ_ОТПРАВЛЯТЬ" in explicit.prompt
    assert "ПАМЯТЬ_НЕ_ОТПРАВЛЯТЬ" not in explicit.prompt


def test_external_workspace_scope_explicitly_includes_memory_and_auto_sources(
    tmp_path,
) -> None:
    store = make_store(tmp_path)
    workspace = store.default_workspace_id()
    store.set_setting("model_mode", "external")
    store.set_setting("external_context_scope", "workspace")
    store.set_setting("llm_base_url", "https://api.example.com/v1")
    store.set_setting(
        "external_context_scope_endpoint", "https://api.example.com/v1"
    )
    store.set_setting("external_context_scope_workspace", workspace)
    store.set_setting("memory_enabled", "true")
    store.remember(
        "Рабочая память",
        "Маркер РАСШИРЕННАЯ_ПАМЯТЬ",
        workspace_id=workspace,
    )
    source = store.add_source(
        workspace,
        "document",
        "Никелевый прогноз",
        "Маркер РАСШИРЕННЫЙ_ИСТОЧНИК: прогноз готов.",
    )

    turn = LocalOrchestrator(store).prepare_turn(
        "Проверь никелевый прогноз",
        workspace_id=workspace,
        task_id=None,
    )

    assert source["id"] in {item["id"] for item in turn.sources}
    assert "РАСШИРЕННАЯ_ПАМЯТЬ" in turn.prompt
    assert "РАСШИРЕННЫЙ_ИСТОЧНИК" in turn.prompt
    assert "явно разрешил расширенный контекст" in turn.prompt


def test_external_workspace_scope_is_ignored_for_a_different_endpoint_or_workspace(
    tmp_path,
) -> None:
    store = make_store(tmp_path)
    first_workspace = store.default_workspace_id()
    second_workspace = store.create_workspace("Другой проект")["id"]
    store.set_settings(
        {
            "model_mode": "external",
            "llm_base_url": "https://provider-b.example/v1",
            "external_context_scope": "workspace",
            "external_context_scope_endpoint": "https://provider-a.example/v1",
            "external_context_scope_workspace": first_workspace,
            "memory_enabled": "true",
        }
    )
    store.remember(
        "Не отправлять",
        "МАРКЕР_НЕСВЯЗАННОГО_СОГЛАСИЯ",
        workspace_id=first_workspace,
    )

    wrong_endpoint = LocalOrchestrator(store).prepare_turn(
        "Проверь контекст",
        workspace_id=first_workspace,
        task_id=None,
    )
    wrong_workspace = LocalOrchestrator(store).prepare_turn(
        "Проверь контекст",
        workspace_id=second_workspace,
        task_id=None,
    )

    assert "МАРКЕР_НЕСВЯЗАННОГО_СОГЛАСИЯ" not in wrong_endpoint.prompt
    assert "расширенный контекст" not in wrong_endpoint.prompt
    assert "расширенный контекст" not in wrong_workspace.prompt


def test_artifact_versions_are_kept(tmp_path) -> None:
    store = make_store(tmp_path)
    workspace = store.default_workspace_id()
    artifact = store.create_artifact(workspace, None, "Отчёт", "Версия один")
    first_path = Path(artifact["path"])
    updated = store.update_artifact(artifact["id"], "Версия два")

    assert first_path.read_text() == "Версия один"
    assert Path(updated["path"]).read_text() == "Версия два"
    assert updated["current_version"] == 2


def test_custom_skill_update_creates_new_version(tmp_path) -> None:
    store = make_store(tmp_path)
    skill = store.create_or_update_skill(
        "Моя методика", "/mine", "Описание", "Инструкция 1"
    )
    updated = store.create_or_update_skill(
        "Моя методика", "/mine", "Описание", "Инструкция 2", skill_id=skill["id"]
    )

    assert updated["version"] == 2
    versions = store._rows("SELECT * FROM skill_versions WHERE skill_id = ?", (skill["id"],))
    assert versions[0]["instruction"] == "Инструкция 1"


def test_orchestrator_selects_skill_sources_and_stages_external_action(tmp_path) -> None:
    store = make_store(tmp_path)
    workspace = store.default_workspace_id()
    store.add_source(
        workspace,
        "meeting",
        "Бюджет проекта",
        "На встрече утвердили бюджет проекта X в размере десяти миллионов.",
    )
    orchestrator = LocalOrchestrator(store)
    turn = orchestrator.prepare_turn(
        "/research проанализируй бюджет проекта и отправь письмо",
        workspace_id=workspace,
        task_id=None,
    )

    assert turn.skill["id"] == "research"
    assert turn.sources
    assert "[S1]" in turn.prompt
    assert store.get_task(turn.task_id)["status"] == "running"
    assert store._rows("SELECT * FROM approvals WHERE task_id = ?", (turn.task_id,))

    orchestrator.finish_turn(turn, "Черновик письма готов.")

    assert store.get_task(turn.task_id)["status"] == "needs_user"


def test_interrupted_turn_waits_for_user_instead_of_staying_running(tmp_path) -> None:
    store = make_store(tmp_path)
    orchestrator = LocalOrchestrator(store)
    turn = orchestrator.prepare_turn(
        "Первый голосовой вопрос",
        workspace_id=store.default_workspace_id(),
        task_id=None,
    )

    orchestrator.finish_turn(turn, "Незавершённый ответ", interrupted=True)

    assert store.get_task(turn.task_id)["status"] == "needs_user"
    assert store._rows(
        "SELECT 1 FROM task_events WHERE task_id = ? AND kind = 'interrupted'",
        (turn.task_id,),
    )


def test_import_markdown_and_normalize_spoken_text(tmp_path) -> None:
    store = make_store(tmp_path)
    orchestrator = LocalOrchestrator(store)
    document = tmp_path / "meeting.md"
    document.write_text("Решили запустить пилот в сентябре.", encoding="utf-8")

    source = orchestrator.import_file(document, workspace_id=store.default_workspace_id())
    normalized = orchestrator.normalize_spoken_text("ну вот эээ подготовь отчет")

    assert source["kind"] == "meeting"
    assert source["meeting_id"]
    assert store.get_meeting(source["meeting_id"], include_items=True)["items"]
    assert "пилот" in source["content"]
    assert normalized == "Подготовь отчет"


def test_memory_can_be_edited(tmp_path) -> None:
    store = make_store(tmp_path)
    item = store.remember("Старое", "Первый текст", workspace_id=store.default_workspace_id())

    store.update_memory(item["id"], "Новое", "Исправленный текст")

    updated = store._rows("SELECT * FROM memory WHERE id=?", (item["id"],))[0]
    assert updated["title"] == "Новое"
    assert updated["content"] == "Исправленный текст"


def test_memory_types_can_be_allowed_independently(tmp_path) -> None:
    store = make_store(tmp_path)
    workspace = store.default_workspace_id()
    store.remember(
        "Предпочтение",
        "Маркер ПРЕДПОЧТЕНИЕ",
        workspace_id=workspace,
        kind="preference",
    )
    store.remember(
        "Факт",
        "Маркер ФАКТ",
        workspace_id=workspace,
        kind="fact",
    )
    store.set_setting("memory_preferences_enabled", "false")

    turn = LocalOrchestrator(store).prepare_turn(
        "Что ты помнишь?",
        workspace_id=workspace,
        task_id=None,
    )

    assert "ПРЕДПОЧТЕНИЕ" not in turn.prompt
    assert "ФАКТ" in turn.prompt
    with pytest.raises(ValueError, match="тип рабочей памяти отключён"):
        store.remember(
            "Ещё предпочтение",
            "Не сохранять",
            workspace_id=workspace,
            kind="preference",
        )


def test_explicit_memory_detects_type_and_respects_type_setting(tmp_path) -> None:
    store = make_store(tmp_path)
    workspace = store.default_workspace_id()
    orchestrator = LocalOrchestrator(store)
    store.set_setting("memory_preferences_enabled", "false")

    orchestrator.prepare_turn(
        "Запомни, что я предпочитаю короткие заголовки",
        workspace_id=workspace,
        task_id=None,
    )
    orchestrator.prepare_turn(
        "Запомни, что я обещаю подготовить отчёт",
        workspace_id=workspace,
        task_id=None,
    )

    rows = store._rows("SELECT kind, content FROM memory ORDER BY created_at")
    assert rows == [
        {
            "kind": "commitment",
            "content": "я обещаю подготовить отчёт",
        }
    ]


def test_memory_edit_can_change_kind(tmp_path) -> None:
    store = make_store(tmp_path)
    item = store.remember("Заметка", "Текст")

    store.update_memory(
        item["id"],
        "Факт",
        "Проверенный текст",
        kind="fact",
    )

    updated = store._rows("SELECT kind, title, content FROM memory WHERE id=?", (item["id"],))[0]
    assert updated == {
        "kind": "fact",
        "title": "Факт",
        "content": "Проверенный текст",
    }


def test_event_automation_stays_enabled_and_can_be_edited(tmp_path) -> None:
    store = make_store(tmp_path)
    automation = store.create_automation(
        store.default_workspace_id(),
        "Следить за источниками",
        "/digest обнови сводку",
        "при новом источнике",
    )

    assert automation["next_run_at"] is None
    assert store.event_automations(store.default_workspace_id())[0]["id"] == automation["id"]

    store.update_automation(
        automation["id"],
        name="Следить за контекстом",
        prompt="/research что изменилось",
        schedule="при изменении контекста",
    )
    store.mark_automation_run(automation["id"], "при изменении контекста")

    updated = store._rows("SELECT * FROM automations WHERE id=?", (automation["id"],))[0]
    assert updated["enabled"] == 1
    assert updated["name"] == "Следить за контекстом"

    store.delete_automation(automation["id"])
    assert not store._rows("SELECT * FROM automations WHERE id=?", (automation["id"],))


def test_task_only_source_is_isolated_and_persists_with_task(tmp_path) -> None:
    database = tmp_path / "assistant.sqlite3"
    store = AssistantStore(database)
    workspace = store.default_workspace_id()
    first = store.create_task(workspace, "Первая задача")
    second = store.create_task(workspace, "Вторая задача")
    document = tmp_path / "private-note.md"
    document.write_text("Секретный маркер проекта — кобальт.", encoding="utf-8")

    orchestrator = LocalOrchestrator(store)
    source = orchestrator.import_file(
        document,
        workspace_id=workspace,
        task_id=first["id"],
    )

    assert source["visibility"] == "task"
    assert [item["id"] for item in store.task_sources(first["id"])] == [source["id"]]
    assert store.search_sources(
        "кобальт", workspace_id=workspace, task_id=first["id"]
    )
    assert not store.search_sources(
        "кобальт", workspace_id=workspace, task_id=second["id"]
    )
    assert not store.search_sources("кобальт", workspace_id=workspace)
    assert source["id"] not in {
        item["id"] for item in store.snapshot(workspace_id=workspace, task_id=first["id"])["sources"]
    }

    first_turn = orchestrator.prepare_turn(
        "Ответь без поиска новых документов",
        workspace_id=workspace,
        task_id=first["id"],
    )
    second_turn = orchestrator.prepare_turn(
        "Ответь без поиска новых документов",
        workspace_id=workspace,
        task_id=second["id"],
    )
    assert source["id"] in {item["id"] for item in first_turn.sources}
    assert source["id"] not in {item["id"] for item in second_turn.sources}
    assert store._connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION

    store.close()
    reopened = AssistantStore(database)
    restored = reopened.snapshot(workspace_id=workspace, task_id=first["id"])
    assert restored["task_sources"] == [
        {
            "id": source["id"],
            "title": source["title"],
            "kind": source["kind"],
            "path": source["path"],
        }
    ]
    assert reopened.snapshot(workspace_id=workspace, task_id=second["id"])[
        "task_sources"
    ] == []


def test_assistant_message_persists_source_references_and_skill(tmp_path) -> None:
    store = make_store(tmp_path)
    workspace = store.default_workspace_id()
    source = store.add_source(
        workspace,
        "meeting",
        "Бюджетный комитет",
        "Бюджет проекта утверждён на следующий квартал.",
        path="/tmp/budget.md",
    )
    orchestrator = LocalOrchestrator(store)
    turn = orchestrator.prepare_turn(
        "/research проверь бюджет проекта",
        workspace_id=workspace,
        task_id=None,
    )

    orchestrator.finish_turn(turn, "Бюджет утверждён [S1].")

    assistant_message = [
        item for item in store.messages(turn.task_id) if item["role"] == "assistant"
    ][-1]
    metadata = json.loads(assistant_message["metadata"])
    assert metadata["skill"] == "research"
    retrieved = turn.sources[0]
    assert metadata["sources"] == [
        {
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
    ]


def test_existing_database_migrates_source_visibility_and_schema_version(tmp_path) -> None:
    database = tmp_path / "assistant.sqlite3"
    store = AssistantStore(database)
    workspace = store.default_workspace_id()
    source = store.add_source(workspace, "document", "Старый источник", "Старый текст")
    store.close()

    connection = sqlite3.connect(database)
    connection.execute("DROP TABLE source_chunks_fts")
    connection.execute("DROP TABLE source_chunks")
    connection.execute("DROP TABLE task_sources")
    connection.execute("ALTER TABLE sources DROP COLUMN visibility")
    connection.execute("PRAGMA user_version = 0")
    connection.commit()
    connection.close()

    migrated = AssistantStore(database)
    source_columns = {
        row[1] for row in migrated._connection.execute("PRAGMA table_info(sources)")
    }
    assert "visibility" in source_columns
    assert migrated.get_source(source["id"])["visibility"] == "workspace"
    assert migrated._connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert migrated._connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='task_sources'"
    ).fetchone()
    chunks = migrated._rows(
        "SELECT * FROM source_chunks WHERE source_id = ? ORDER BY chunk_index",
        (source["id"],),
    )
    assert chunks
    assert all(
        migrated.get_source(source["id"])["content"][chunk["char_start"] : chunk["char_end"]]
        == chunk["content"]
        for chunk in chunks
    )
    assert migrated.search_sources("Старый текст", workspace_id=workspace)[0][
        "id"
    ] == source["id"]


def test_reopen_recovers_stale_running_task_as_waiting_for_user(tmp_path) -> None:
    database = tmp_path / "assistant.sqlite3"
    store = AssistantStore(database)
    task = store.create_task(store.default_workspace_id(), "Незавершённая задача")
    store.update_task(task["id"], status="running")
    store.close()

    reopened = AssistantStore(database)

    assert reopened.get_task(task["id"])["status"] == "needs_user"
    assert reopened._rows(
        "SELECT 1 FROM task_events WHERE task_id = ? AND kind = 'recovered'",
        (task["id"],),
    )
    assert reopened._rows(
        "SELECT 1 FROM audit_log WHERE task_id = ? AND action = 'task.recover'",
        (task["id"],),
    )


def test_delete_task_cascades_private_context_and_preserves_artifact(tmp_path) -> None:
    store = make_store(tmp_path)
    store.trash_dir = tmp_path / "Trash"
    workspace = store.default_workspace_id()
    task = store.create_task(workspace, "Удаляемая задача")
    managed_file = store.files_dir / "private.txt"
    managed_file.write_text("секрет задачи", encoding="utf-8")
    source = store.add_source(
        workspace,
        "document",
        "Файл задачи",
        "секрет задачи",
        path=str(managed_file),
        visibility="task",
        task_id=task["id"],
    )
    store.add_message(task["id"], "user", "удалить историю")
    artifact = store.create_artifact(
        workspace,
        task["id"],
        "Независимый результат",
        "сохранить результат",
    )

    result = store.delete_task(task["id"])

    assert result["trashed_files"] == 1
    with pytest.raises(KeyError):
        store.get_task(task["id"])
    with pytest.raises(KeyError):
        store.get_source(source["id"])
    assert not managed_file.exists()
    assert any(store.trash_dir.iterdir())
    assert store.get_artifact(artifact["id"])["task_id"] is None
    assert not store._rows("SELECT * FROM messages WHERE task_id=?", (task["id"],))
    assert not store._connection.execute("PRAGMA foreign_key_check").fetchall()


def test_delete_meeting_source_trashes_only_managed_files_and_clears_indexes(
    tmp_path,
) -> None:
    store = make_store(tmp_path)
    store.trash_dir = tmp_path / "Trash"
    workspace = store.default_workspace_id()
    original = tmp_path / "original-recording.wav"
    original.write_bytes(b"original")
    managed_audio = store.files_dir / "managed-recording.wav"
    managed_audio.write_bytes(b"managed")
    transcript = store.files_dir / "managed-transcript.md"
    transcript.write_text("Решили продолжать пилот.", encoding="utf-8")
    source = store.add_source(
        workspace,
        "meeting",
        "Протокол на удаление",
        "Решили продолжать пилот.",
        path=str(transcript),
        metadata={
            "original_audio_path": str(original),
            "managed_audio_path": str(managed_audio),
        },
    )
    meeting = store.analyze_meeting(source["id"])

    result = store.delete_source(source["id"])

    assert result["trashed_files"] == 2
    assert original.exists()
    assert not managed_audio.exists()
    assert not transcript.exists()
    assert len(list(store.trash_dir.iterdir())) == 2
    assert not store._rows("SELECT * FROM meetings WHERE id=?", (meeting["id"],))
    assert not store._rows("SELECT * FROM source_chunks WHERE source_id=?", (source["id"],))
    assert not store._rows("SELECT * FROM sources_fts WHERE source_id=?", (source["id"],))
    assert not store.search_sources("продолжать пилот", workspace_id=workspace)
    assert not store._connection.execute("PRAGMA foreign_key_check").fetchall()


def test_delete_artifact_moves_all_versions_to_trash(tmp_path) -> None:
    store = make_store(tmp_path)
    store.trash_dir = tmp_path / "Trash"
    artifact = store.create_artifact(
        store.default_workspace_id(),
        None,
        "Версионный отчёт",
        "Версия один",
    )
    store.update_artifact(artifact["id"], "Версия два")
    artifact_directory = Path(store.get_artifact(artifact["id"])["path"]).parent

    result = store.delete_artifact(artifact["id"])

    assert result["trashed_files"] == 1
    with pytest.raises(KeyError):
        store.get_artifact(artifact["id"])
    assert not artifact_directory.exists()
    assert len(list(store.trash_dir.iterdir())) == 1
    assert not store._rows(
        "SELECT * FROM artifact_versions WHERE artifact_id=?", (artifact["id"],)
    )
    assert not store._connection.execute("PRAGMA foreign_key_check").fetchall()
