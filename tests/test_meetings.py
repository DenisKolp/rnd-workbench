from pathlib import Path
import sqlite3

import pytest

from voice_assistant.meetings import analyze_transcript, parse_due_date
from voice_assistant.orchestrator import LocalOrchestrator
from voice_assistant.store import SCHEMA_VERSION, AssistantStore


TRANSCRIPT = """[00:01] Анна: Тема: Запуск пилота.
[00:14] Иван: Решили запустить пилот в сентябре.
[00:30] Анна: Иван подготовит смету до 12 сентября.
[00:44] Олег: Беру на себя проверку безопасности к пятнице.
[01:02] Анна: Риск: подрядчик может не успеть.
[01:14] Иван: Кто согласует бюджет?"""


def make_store(tmp_path: Path) -> AssistantStore:
    return AssistantStore(tmp_path / "assistant.sqlite3")


def add_meeting(
    store: AssistantStore,
    transcript: str = TRANSCRIPT,
    *,
    title: str = "Статус пилота",
    occurred_at: str = "2026-09-08T10:00:00+03:00",
):
    source = store.add_source(
        store.default_workspace_id(),
        "meeting",
        title,
        transcript,
        path=f"/meetings/{title}.md",
    )
    return source, store.analyze_meeting(source["id"], occurred_at=occurred_at)


def test_audio_import_persists_managed_copy_transcript_and_analysis(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    original = tmp_path / "weekly-sync.m4a"
    original.write_bytes(b"meeting-audio")

    source = LocalOrchestrator(store).import_meeting_audio(
        original,
        TRANSCRIPT,
        workspace_id=store.default_workspace_id(),
    )
    stored = store.get_source(source["id"])
    meeting = store.get_meeting(source["meeting_id"], include_items=True)

    assert stored["kind"] == "meeting"
    assert Path(stored["path"]).read_text(encoding="utf-8") == TRANSCRIPT
    managed_audio = Path(stored["metadata"]["managed_audio_path"])
    assert managed_audio.read_bytes() == b"meeting-audio"
    assert stored["metadata"]["original_audio_path"] == str(original)
    assert stored["metadata"]["transcribed_locally"] is True
    assert source["transcript_chars"] == len(TRANSCRIPT)
    assert meeting["status"] == "analyzed"
    assert {item["kind"] for item in meeting["items"]} >= {
        "decision",
        "action",
        "risk",
    }


def test_analyzer_extracts_speakers_cues_dates_and_exact_spans() -> None:
    analysis = analyze_transcript(
        TRANSCRIPT,
        title="Статус пилота",
        occurred_at="2026-09-08T10:00:00+03:00",
    )

    assert analysis["participants"] == ["Анна", "Иван", "Олег"]
    assert {item["kind"] for item in analysis["items"]} == {
        "topic",
        "decision",
        "action",
        "commitment",
        "risk",
        "question",
    }
    action = next(item for item in analysis["items"] if item["kind"] == "action")
    commitment = next(item for item in analysis["items"] if item["kind"] == "commitment")
    assert action["owner"] == "Иван"
    assert action["due_at"] == "2026-09-12"
    assert commitment["owner"] == "Олег"
    assert commitment["due_at"] == "2026-09-11"
    assert all(
        TRANSCRIPT[item["source_start"] : item["source_end"]] == item["source_quote"]
        for item in analysis["items"]
    )


def test_due_date_parser_supports_iso_numeric_month_and_relative_dates() -> None:
    base = "2026-09-08T10:00:00+03:00"
    assert parse_due_date("до 2026-10-03", base) == "2026-10-03"
    assert parse_due_date("не позднее 04.10.2026", base) == "2026-10-04"
    assert parse_due_date("до 12 сентября", base) == "2026-09-12"
    assert parse_due_date("завтра", base) == "2026-09-09"
    assert parse_due_date("через 3 дня", base) == "2026-09-11"
    assert parse_due_date("к пятнице", base) == "2026-09-11"
    assert parse_due_date("к пятнице", None) is None


def test_plain_paragraphs_are_analyzed_without_speaker_markup() -> None:
    transcript = (
        "Обсудим: качество поставки. Решено провести повторную проверку. "
        "Нужно отправить образцы до 14.09.2026. Риск: лаборатория перегружена. "
        "Когда будет результат?"
    )
    analysis = analyze_transcript(transcript, occurred_at="2026-09-10")

    assert analysis["participants"] == []
    assert [item["kind"] for item in analysis["items"]] == [
        "topic",
        "decision",
        "action",
        "risk",
        "question",
    ]
    assert all(item["source_quote"] in transcript for item in analysis["items"])


def test_schema_migrates_without_losing_existing_rows(tmp_path: Path) -> None:
    database = tmp_path / "assistant.sqlite3"
    store = AssistantStore(database)
    workspace = store.default_workspace_id()
    task = store.create_task(workspace, "Сохранить меня")
    source = store.add_source(workspace, "meeting", "Старый протокол", "Решили продолжать.")
    store.close()

    connection = sqlite3.connect(database)
    connection.execute("PRAGMA user_version = 1")
    connection.commit()
    connection.close()

    migrated = AssistantStore(database)
    assert migrated.get_task(task["id"])["title"] == "Сохранить меня"
    assert migrated.get_source(source["id"])["content"] == "Решили продолжать."
    assert migrated._connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert {
        row[0]
        for row in migrated._connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    } >= {"meetings", "meeting_items"}


def test_atomic_reanalysis_replaces_items_and_rejects_invalid_spans(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    source, meeting = add_meeting(store)
    original_id = meeting["id"]
    original_created_at = meeting["created_at"]
    assert len(meeting["items"]) == 6

    replacement = analyze_transcript(
        source["content"], title="Новый заголовок", occurred_at="2026-09-08"
    )
    replacement["items"] = replacement["items"][:2]
    updated = store.upsert_meeting_analysis(source["id"], replacement)
    assert updated["id"] == original_id
    assert updated["created_at"] == original_created_at
    assert updated["title"] == "Новый заголовок"
    assert len(updated["items"]) == 2

    invalid = {**replacement, "items": [{**replacement["items"][0], "source_quote": "подмена"}]}
    with pytest.raises(ValueError, match="не соответствует"):
        store.upsert_meeting_analysis(source["id"], invalid)
    assert len(store.get_meeting(original_id, include_items=True)["items"]) == 2

    duplicate = {**replacement, "items": [dict(item) for item in replacement["items"]]}
    duplicate["items"][0]["id"] = "same-id"
    duplicate["items"][1]["id"] = "same-id"
    with pytest.raises(sqlite3.IntegrityError):
        store.upsert_meeting_analysis(source["id"], duplicate)
    assert len(store.get_meeting(original_id, include_items=True)["items"]) == 2


def test_reanalysis_preserves_user_item_statuses_for_semantic_matches(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    source, meeting = add_meeting(store)
    action = next(item for item in meeting["items"] if item["kind"] == "action")
    risk = next(item for item in meeting["items"] if item["kind"] == "risk")
    store.update_meeting_item_status(action["id"], "done")
    store.update_meeting_item_status(risk["id"], "superseded")

    replacement = analyze_transcript(
        source["content"],
        title=meeting["title"],
        occurred_at=meeting["occurred_at"],
    )
    replacement_action = next(
        item for item in replacement["items"] if item["kind"] == "action"
    )
    replacement_action["text"] = replacement_action["text"].replace(
        "подготовит", "должен подготовить"
    )
    reanalyzed = store.upsert_meeting_analysis(source["id"], replacement)
    updated_action = next(item for item in reanalyzed["items"] if item["kind"] == "action")
    updated_risk = next(item for item in reanalyzed["items"] if item["kind"] == "risk")
    untouched_decision = next(
        item for item in reanalyzed["items"] if item["kind"] == "decision"
    )

    assert (updated_action["id"], updated_action["status"]) == (action["id"], "done")
    assert (updated_risk["id"], updated_risk["status"]) == (
        risk["id"],
        "superseded",
    )
    assert untouched_decision["status"] == "open"


def test_filters_status_source_enrichment_timeline_and_snapshot(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    source, meeting = add_meeting(store)
    workspace = store.default_workspace_id()
    action = next(item for item in meeting["items"] if item["kind"] == "action")

    filtered = store.list_meeting_items(
        workspace,
        kind="action",
        status="open",
        person="иван",
        date_from="2026-09-01",
        date_to="2026-09-30T23:59:59",
    )
    assert len(filtered) == 1
    assert filtered[0]["source_id"] == source["id"]
    assert filtered[0]["source_path"] == source["path"]
    assert filtered[0]["meeting_title"] == meeting["title"]

    store.update_meeting_item_status(action["id"], "done")
    assert not store.list_meeting_items(workspace, kind="action", status="open")
    assert store.list_meeting_items(workspace, kind="action", status="done")[0]["id"] == action["id"]
    timeline = store.topic_timeline(workspace, "пилот")
    assert timeline and all(item["meeting_id"] == meeting["id"] for item in timeline)

    snapshot = store.snapshot(workspace_id=workspace, meeting_id=meeting["id"])
    assert snapshot["today"]["meetings"] == 1
    assert snapshot["meeting_counts"]["total"] == 1
    assert snapshot["current_meeting_id"] == meeting["id"]
    assert snapshot["meeting_items"]
    assert snapshot["meetings"][0]["participants"] == ["Анна", "Иван", "Олег"]
    assert snapshot["meetings"][0]["source_path"] == source["path"]


def test_comparison_and_briefing_organize_changes_and_open_items(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    _, before = add_meeting(store, title="Пилот 1", occurred_at="2026-09-08")
    later = """Анна: Тема: Запуск пилота.
Иван: Решили запустить пилот в октябре.
Анна: Иван подготовит финальную смету до 20 сентября.
Олег: Риск: поставка оборудования задерживается.
Иван: Кто подтвердит площадку?"""
    _, after = add_meeting(store, later, title="Пилот 2", occurred_at="2026-09-15")

    comparison = store.compare_meetings(before["id"], after["id"])
    assert comparison["changed"]
    assert comparison["added"] or comparison["removed"]
    briefing = store.briefing_data(store.default_workspace_id())
    assert len(briefing["meetings"]) == 2
    assert briefing["recent_decisions"]
    assert briefing["open_actions"]
    assert briefing["risks"]
    assert briefing["questions"]


def test_participants_json_decoding_and_foreign_key_cascade(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    source, meeting = add_meeting(store)
    assert isinstance(store.get_meeting(meeting["id"])["participants"], list)

    with store.transaction() as connection:
        connection.execute("DELETE FROM sources WHERE id=?", (source["id"],))
    assert not store._rows("SELECT * FROM meetings WHERE id=?", (meeting["id"],))
    assert not store._rows("SELECT * FROM meeting_items WHERE meeting_id=?", (meeting["id"],))


def test_stable_inbox_event_is_updated_instead_of_duplicated(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    workspace = store.default_workspace_id()
    first = store.upsert_inbox_event(
        "deadline:item-1", "Срок завтра", "Первое", 2, "meeting_due", workspace
    )
    second = store.upsert_inbox_event(
        "deadline:item-1", "Срок сегодня", "Обновлено", 3, "meeting_due", workspace
    )
    assert first == second
    rows = store._rows("SELECT * FROM inbox WHERE kind='meeting_due'")
    assert len(rows) == 1
    assert rows[0]["title"] == "Срок сегодня"
    assert rows[0]["priority"] == 3
