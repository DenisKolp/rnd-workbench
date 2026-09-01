from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import zipfile

import pytest

from voice_assistant.orchestrator import (
    MAX_DOCX_DOCUMENT_XML_BYTES,
    LocalOrchestrator,
)
from voice_assistant.config import Config
from voice_assistant.store import SCHEMA_VERSION, AssistantStore
from voice_assistant.synapse import (
    FOLLOW_UP_CAPABILITIES,
    MAX_ZIP_ENTRIES,
    PackagePart,
    SynapseImportInProgressError,
    SynapseRepairRequiredError,
    SynapseMeetingPackageImporter,
    meeting_package_fingerprint,
)
from voice_assistant.ui_backend import EventEmitter, UIBackend


TRANSCRIPT = """Участники: Анна, Иван, Олег
[00:01] Анна: Тема: Запуск пилота.
[00:14] Иван: Решили запустить пилот в сентябре.
[00:30] Анна: Иван подготовит смету до 12 сентября.
[00:44] Олег: Беру на себя проверку безопасности к пятнице.
[01:02] Анна: Риск: подрядчик может не успеть.
[01:14] Иван: Кто согласует бюджет?"""

DESCRIPTION = """Цель встречи — согласовать запуск пилота.
Контекст: команда готовит первую установочную волну."""


def make_store(tmp_path: Path) -> AssistantStore:
    return AssistantStore(tmp_path / "data" / "assistant.sqlite3")


def create_package(
    root: Path,
    *,
    package_id: str = "synapse-demo-42",
    transcript: str = TRANSCRIPT,
    metadata: dict[str, object] | None = None,
    connector_checkpoint: dict[str, str] | None = None,
) -> Path:
    root.mkdir(parents=True)
    (root / "attachments").mkdir()
    files = {
        "transcript.txt": transcript.encode(),
        "description.md": DESCRIPTION.encode(),
        "attachments/plan.md": "План запуска: проверить 30 устройств.".encode(),
        "attachments/diagram.bin": b"\x00\x01binary-diagram",
    }
    for relative_path, data in files.items():
        target = root / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    def spec(path: str, **extra: str) -> dict[str, str]:
        return {
            "path": path,
            "sha256": hashlib.sha256(files[path]).hexdigest(),
            **extra,
        }

    manifest = {
        "schema_version": "1.0",
        "source_system": "synapse",
        "import_mode": "LOCAL_PACKAGE_IMPORT",
        "package_id": package_id,
        "meeting": {
            "title": "Статус пилота",
            "occurred_at": "2026-08-31T10:00:00+03:00",
            "participants": ["Анна", "Иван", "Олег", "Наталья"],
            "organizer": "Анна",
            "classification": "confidential",
        },
        "transcript": spec("transcript.txt", media_type="text/plain"),
        "description": spec("description.md", media_type="text/markdown"),
        "attachments": [
            spec("attachments/plan.md", title="План запуска", media_type="text/markdown"),
            spec(
                "attachments/diagram.bin",
                title="Диаграмма",
                media_type="application/octet-stream",
            ),
        ],
        "metadata": metadata
        if metadata is not None
        else {"project": "pilot", "duration_seconds": 1800, "recording": False},
    }
    if connector_checkpoint is not None:
        manifest["connector_checkpoint"] = connector_checkpoint
    (root / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False),
        encoding="utf-8",
    )
    return root


def zip_package(directory: Path, target: Path) -> Path:
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(directory.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(directory).as_posix())
    return target


def set_meeting_time(directory: Path, occurred_at: str) -> None:
    manifest_path = directory / "manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["meeting"]["occurred_at"] = occurred_at
    manifest_path.write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )


def create_quick_bundle(root: Path) -> Path:
    root.mkdir(parents=True)
    (root / "attachments").mkdir()
    (root / "Расшифровка.txt").write_text(TRANSCRIPT, encoding="utf-8")
    (root / "Сводка.md").write_text(DESCRIPTION, encoding="utf-8")
    (root / "attachments" / "plan.md").write_text(
        "План запуска: проверить 30 устройств.",
        encoding="utf-8",
    )
    (root / ".DS_Store").write_bytes(b"ignored-local-metadata")
    return root


def make_docx(text: str) -> bytes:
    paragraphs = []
    for line in text.splitlines():
        escaped = (
            line.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )
        paragraphs.append(f"<w:p><w:r><w:t>{escaped}</w:t></w:r></w:p>")
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/'
        'wordprocessingml/2006/main"><w:body>'
        + "".join(paragraphs)
        + "</w:body></w:document>"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", document.encode("utf-8"))
    return buffer.getvalue()


def replace_transcript_with_docx(package: Path, data: bytes) -> None:
    (package / "transcript.txt").unlink()
    (package / "transcript.docx").write_bytes(data)
    manifest_path = package / "manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["transcript"] = {
        "path": "transcript.docx",
        "media_type": (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        ),
        "sha256": hashlib.sha256(data).hexdigest(),
    }
    manifest_path.write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )


def replace_binary_attachment(
    package: Path,
    *,
    relative_path: str,
    title: str,
    data: bytes,
) -> None:
    target = package / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    manifest_path = package / "manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["attachments"][1] = {
        "path": relative_path,
        "title": title,
        "media_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "sha256": hashlib.sha256(data).hexdigest(),
    }
    manifest_path.write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )


def test_import_builds_enriched_traceable_context_and_truthful_gate(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    package = create_package(tmp_path / "synapse-export")

    result = LocalOrchestrator(store).import_synapse_meeting_package(
        package,
        workspace_id=store.default_workspace_id(),
    )

    assert result["status"] == "imported"
    assert result["capability"] == {
        "source_system": "synapse",
        "import_mode": "LOCAL_PACKAGE_IMPORT",
        "package_import_available": True,
        "corporate_api_connected": False,
        "real_integration": False,
        "write_back_available": False,
        "live_connector_available": False,
        "checkpoint_accepted": False,
        "supported_delivery_modes": ["POLLING", "WEBHOOK"],
        "reason_code": "CORPORATE_API_NOT_CONNECTED",
        "label": "Локальный импорт пакета; API eXpress (Синапс) не подключён",
    }
    assert result["follow_up_capabilities"] == [
        dict(item) for item in FOLLOW_UP_CAPABILITIES
    ]
    assert {key: len(value) for key, value in result["analysis"].items()} == {
        "decisions": 1,
        "actions": 2,
        "risks": 1,
        "questions": 1,
    }
    assert result["next_meeting"]["agenda"]
    assert result["next_meeting"]["context_scope"] == {
        "mode": "single_meeting",
        "scope_id": f"meeting:{result['meeting_id']}",
        "metadata_key": None,
        "meeting_count": 1,
        "selected_meeting_id": result["meeting_id"],
    }
    assert len(result["proposals"]) == 4
    assert all(item["execution_mode"] == "draft_only" for item in result["proposals"])
    assert all(item["external_system"] is None for item in result["proposals"])
    assert all(
        item["provenance"]["source_id"] == result["source_id"]
        for group in result["analysis"].values()
        for item in group
    )
    supporting = result["supporting_context"]
    assert supporting["boundary"] == "supporting_sources_not_transcript_facts"
    assert "согласовать запуск пилота" in supporting["description"]["snippet"]
    plan_context = next(
        item for item in supporting["attachments"] if item["title"] == "План запуска"
    )
    assert "30 устройств" in plan_context["snippet"]
    assert plan_context["provenance"]["relation_type"] == "synapse.attachment"

    primary = store.get_source(result["source_id"])
    meeting = store.get_meeting(result["meeting_id"], include_items=True)
    relations = store.source_relations(result["source_id"])
    assert primary["classification"] == "confidential"
    assert primary["metadata"]["provenance"]["real_integration"] is False
    assert meeting["participants"] == ["Анна", "Иван", "Наталья", "Олег"]
    assert len(relations) == 3
    assert {relation["relation_type"] for relation in relations} == {
        "synapse.description",
        "synapse.attachment",
    }
    assert all(_managed(store, Path(relation["related_path"])) for relation in relations)
    assert store.search_sources(
        "30 устройств",
        workspace_id=store.default_workspace_id(),
    )[0]["kind"] == "meeting_attachment"
    assert store._connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    audit = store._rows("SELECT * FROM audit_log WHERE action='synapse.package.import'")
    assert len(audit) == 1
    assert audit[0]["status"] == "local_mock"


def test_explicit_express_series_scopes_next_meeting_context_without_title_guessing(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path)
    orchestrator = LocalOrchestrator(store)
    first_package = create_package(
        tmp_path / "series-first",
        package_id="series-first",
        metadata={"group_chat_id": "team-room-42", "project": "pilot"},
    )
    set_meeting_time(first_package, "2026-08-24T10:00:00+03:00")
    first = orchestrator.import_synapse_meeting_package(
        first_package,
        workspace_id=store.default_workspace_id(),
    )
    unrelated_package = create_package(
        tmp_path / "same-title-unrelated",
        package_id="same-title-unrelated",
        metadata={"group_chat_id": "another-room", "project": "pilot"},
    )
    set_meeting_time(unrelated_package, "2026-08-30T10:00:00+03:00")
    unrelated = orchestrator.import_synapse_meeting_package(
        unrelated_package,
        workspace_id=store.default_workspace_id(),
    )
    second_package = create_package(
        tmp_path / "series-second",
        package_id="series-second",
        transcript=TRANSCRIPT.replace(
            "Иван подготовит смету до 12 сентября.",
            "Иван подготовит финальную смету до 15 сентября.",
        ),
        metadata={"group_chat_id": "TEAM-ROOM-42", "project": "pilot"},
    )
    set_meeting_time(second_package, "2026-08-31T10:00:00+03:00")
    second = orchestrator.import_synapse_meeting_package(
        second_package,
        workspace_id=store.default_workspace_id(),
    )

    scope = second["next_meeting"]["context_scope"]
    assert scope["mode"] == "express_series"
    assert scope["metadata_key"] == "group_chat_id"
    assert scope["meeting_count"] == 2
    assert scope["scope_id"].startswith("express-series:")
    briefing = store.briefing_data(
        store.default_workspace_id(),
        focus_meeting_id=second["meeting_id"],
        limit=12,
    )
    assert [item["id"] for item in briefing["meetings"]] == [
        second["meeting_id"],
        first["meeting_id"],
    ]
    assert unrelated["meeting_id"] not in {
        item["id"] for item in briefing["meetings"]
    }
    assert briefing["previous_meeting"]["id"] == first["meeting_id"]
    assert briefing["comparison"] is not None
    assert briefing["comparison"]["before"]["id"] == first["meeting_id"]
    assert briefing["comparison"]["after"]["id"] == second["meeting_id"]


def test_briefing_without_explicit_series_uses_only_selected_meeting(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path)
    orchestrator = LocalOrchestrator(store)
    first = orchestrator.import_synapse_meeting_package(
        create_package(tmp_path / "standalone-first", package_id="standalone-first"),
        workspace_id=store.default_workspace_id(),
    )
    second = orchestrator.import_synapse_meeting_package(
        create_package(tmp_path / "standalone-second", package_id="standalone-second"),
        workspace_id=store.default_workspace_id(),
    )

    briefing = store.briefing_data(
        store.default_workspace_id(),
        focus_meeting_id=second["meeting_id"],
    )

    assert [item["id"] for item in briefing["meetings"]] == [second["meeting_id"]]
    assert first["meeting_id"] not in {
        item["id"] for item in briefing["meetings"]
    }
    assert briefing["scope"]["mode"] == "single_meeting"
    assert briefing["previous_meeting"] is None
    assert briefing["comparison"] is None


def test_quick_bundle_without_manifest_imports_description_and_attachments(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path)
    bundle = create_quick_bundle(tmp_path / "Статус-пилота")

    result = LocalOrchestrator(store).import_synapse_meeting_package(
        bundle,
        workspace_id=store.default_workspace_id(),
    )

    assert result["status"] == "imported"
    assert result["capability"]["real_integration"] is False
    assert result["supporting_context"]["description"] is not None
    assert "согласовать запуск пилота" in result["supporting_context"]["description"]["snippet"]
    assert result["supporting_context"]["attachments"][0]["title"] == "plan.md"
    primary = store.get_source(result["source_id"])
    assert primary["title"] == "Статус пилота"
    assert primary["metadata"]["package_metadata"] == {
        "generated_manifest": "true",
        "quick_layout": "transcript+description+attachments",
    }


def test_manifest_docx_transcript_is_extracted_and_analyzed(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    package = create_package(tmp_path / "docx-manifest")
    replace_transcript_with_docx(package, make_docx(TRANSCRIPT))

    result = LocalOrchestrator(store).import_synapse_meeting_package(
        package,
        workspace_id=store.default_workspace_id(),
    )

    source = store.get_source(result["source_id"])
    assert result["status"] == "imported"
    assert "Решили запустить пилот" in source["content"]
    assert len(result["analysis"]["decisions"]) == 1
    assert source["path"].endswith(".docx")


def test_quick_docx_transcript_is_inferred_without_manifest(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    bundle = create_quick_bundle(tmp_path / "Запуск-пилота")
    (bundle / "Расшифровка.txt").unlink()
    (bundle / "Расшифровка.docx").write_bytes(make_docx(TRANSCRIPT))

    result = LocalOrchestrator(store).import_synapse_meeting_package(
        bundle,
        workspace_id=store.default_workspace_id(),
    )

    primary = store.get_source(result["source_id"])
    assert primary["title"] == "Запуск пилота"
    assert "Иван подготовит смету" in primary["content"]
    assert primary["metadata"]["provenance"]["media_type"].endswith(
        "wordprocessingml.document"
    )


def test_corrupt_docx_transcript_rolls_back_before_context_creation(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path)
    package = create_package(tmp_path / "corrupt-docx")
    replace_transcript_with_docx(package, b"not-a-docx")

    with pytest.raises(ValueError, match="безопасно извлечь текст"):
        LocalOrchestrator(store).import_synapse_meeting_package(
            package,
            workspace_id=store.default_workspace_id(),
        )

    assert not store._rows("SELECT * FROM sources")
    assert not list(store.files_dir.glob("synapse-*"))


def test_quick_directory_and_zip_share_deterministic_import_identity(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path)
    bundle = create_quick_bundle(tmp_path / "Команда-пилота")
    archive = zip_package(bundle, tmp_path / "Команда-пилота.zip")
    orchestrator = LocalOrchestrator(store)

    first = orchestrator.import_synapse_meeting_package(
        bundle,
        workspace_id=store.default_workspace_id(),
    )
    repeated = orchestrator.import_synapse_meeting_package(
        archive,
        workspace_id=store.default_workspace_id(),
    )

    assert first["status"] == "imported"
    assert repeated["status"] == "already_imported"
    assert repeated["source_id"] == first["source_id"]


def test_quick_bundle_rejects_ambiguous_transcript_before_store_mutation(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path)
    bundle = create_quick_bundle(tmp_path / "ambiguous")
    (bundle / "transcript-copy.md").write_text(TRANSCRIPT, encoding="utf-8")

    with pytest.raises(ValueError, match="ровно один UTF-8/DOCX файл"):
        LocalOrchestrator(store).import_synapse_meeting_package(
            bundle,
            workspace_id=store.default_workspace_id(),
        )

    assert not store._rows("SELECT * FROM sources")


def test_supporting_context_reserves_description_before_large_attachments(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path)
    package = create_package(tmp_path / "description-first")
    manifest_path = package / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["attachments"] = []
    for index in range(6):
        relative_path = f"attachments/large-{index}.md"
        data = ((f"Вложение {index} " + "контекст ") * 400).encode()
        (package / relative_path).write_bytes(data)
        manifest["attachments"].append(
            {
                "path": relative_path,
                "title": f"Большое вложение {index}",
                "media_type": "text/markdown",
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        )
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False),
        encoding="utf-8",
    )

    result = LocalOrchestrator(store).import_synapse_meeting_package(
        package,
        workspace_id=store.default_workspace_id(),
    )

    supporting = result["supporting_context"]
    assert supporting["description"] is not None
    assert "согласовать запуск пилота" in supporting["description"]["snippet"]
    assert supporting["truncated"] is True


def test_package_fingerprint_matches_java_golden_contract() -> None:
    parts = [
        PackagePart("TRANSCRIPT", "transcript.txt", "Транскрипт", "text/plain", "a" * 64, 1200, b""),
        PackagePart("DESCRIPTION", "description.md", "Описание", "text/markdown", "b" * 64, 320, b""),
        PackagePart(
            "ATTACHMENT",
            "attachments/plan.md",
            "План запуска",
            "text/markdown",
            "c" * 64,
            512,
            b"",
        ),
    ]
    assert meeting_package_fingerprint(
        package_id="synapse-demo-42",
        title="Статус пилота",
        occurred_at="2026-08-31T10:00:00+03:00",
        organizer="Анна",
        classification="confidential",
        participants=["Олег", "Анна", "Иван", "Анна"],
        metadata={"project": "pilot", "duration_seconds": "1800"},
        parts=parts,
    ) == "3ce465e6d824b03dc88f192131365e59db5cb3851e0bc4fe3bb57a1648aba550"


def test_participant_order_and_duplicates_do_not_change_fingerprint(
    tmp_path: Path,
) -> None:
    first = create_package(tmp_path / "first-order")
    reordered = create_package(tmp_path / "second-order")
    manifest_path = reordered / "manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["meeting"]["participants"] = [
        "Наталья",
        "Олег",
        "Анна",
        "Иван",
        "Анна",
    ]
    manifest_path.write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )
    importer = SynapseMeetingPackageImporter(
        make_store(tmp_path),
        text_extractor=LocalOrchestrator._extract_text,
    )

    first_manifest = importer.inspect(first)
    reordered_manifest = importer.inspect(reordered)

    assert reordered_manifest.participants == ("Анна", "Иван", "Наталья", "Олег")
    assert reordered_manifest.fingerprint == first_manifest.fingerprint
    java_contract = reordered_manifest.java_contract()
    assert java_contract["participants"] == [
        "Анна",
        "Иван",
        "Наталья",
        "Олег",
    ]
    assert java_contract["organizer"] == "Анна"
    assert java_contract["classification"] == "confidential"
    assert all(part["title"] for part in java_contract["parts"])


def test_polling_checkpoint_is_preserved_without_enabling_live_connector(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path)
    package = create_package(
        tmp_path / "polling-package",
        connector_checkpoint={
            "delivery_mode": "POLLING",
            "cursor": "cursor-00042",
            "watermark": "2026-08-31T07:00:00Z",
        },
    )
    importer = SynapseMeetingPackageImporter(
        store,
        text_extractor=LocalOrchestrator._extract_text,
    )

    manifest = importer.inspect(package)
    result = importer.import_package(package, workspace_id=store.default_workspace_id())

    assert manifest.java_contract()["connectorCheckpoint"] == {
        "deliveryMode": "POLLING",
        "cursor": "cursor-00042",
        "watermark": "2026-08-31T07:00:00Z",
    }
    assert manifest.fingerprint == (
        "381841647b532aee966a046d8f076864ffd28a5fca8ffb253a915fd1242e3666"
    )
    assert result["capability"]["checkpoint_accepted"] is True
    assert result["capability"]["live_connector_available"] is False
    assert result["capability"]["corporate_api_connected"] is False
    provenance = store.get_source(result["source_id"])["metadata"]["provenance"]
    assert provenance["connector_checkpoint"] == {
        "delivery_mode": "POLLING",
        "cursor": "cursor-00042",
        "watermark": "2026-08-31T07:00:00Z",
    }

    manifest_path = package / "manifest.json"
    redelivery = json.loads(manifest_path.read_text(encoding="utf-8"))
    redelivery["connector_checkpoint"]["cursor"] = "cursor-00043"
    manifest_path.write_text(
        json.dumps(redelivery, ensure_ascii=False),
        encoding="utf-8",
    )
    redelivered_manifest = importer.inspect(package)
    redelivered = importer.import_package(
        package,
        workspace_id=store.default_workspace_id(),
    )
    assert redelivered_manifest.fingerprint == manifest.fingerprint
    assert redelivered["status"] == "already_imported"


def test_zip_import_is_idempotent_and_follow_ups_are_stable(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    package_dir = create_package(tmp_path / "package")
    package_zip = zip_package(package_dir, tmp_path / "package.zip")
    orchestrator = LocalOrchestrator(store)

    first = orchestrator.import_synapse_meeting_package(
        package_zip,
        workspace_id=store.default_workspace_id(),
    )
    second = orchestrator.import_synapse_meeting_package(
        package_zip,
        workspace_id=store.default_workspace_id(),
    )
    reconstructed = orchestrator.synapse_meeting_context(first["source_id"])

    assert second["status"] == "already_imported"
    assert second["source_id"] == first["source_id"]
    assert second["package_fingerprint"] == first["package_fingerprint"]
    assert reconstructed["proposals"] == first["proposals"]
    assert reconstructed["next_meeting"] == first["next_meeting"]
    imported = [
        source
        for source in store._rows("SELECT * FROM sources")
        if "synapse" in str(source["metadata"])
    ]
    assert len(imported) == 4


def test_zip_rejects_excessive_central_directory_entries(tmp_path: Path) -> None:
    package_zip = tmp_path / "oversized-directory.zip"
    with zipfile.ZipFile(package_zip, "w") as archive:
        for index in range(MAX_ZIP_ENTRIES + 1):
            archive.writestr(f"unused/{index}.txt", "x")

    importer = SynapseMeetingPackageImporter(
        make_store(tmp_path),
        text_extractor=LocalOrchestrator._extract_text,
    )
    with pytest.raises(ValueError, match="записей"):
        importer.inspect(package_zip)


def test_zip_rejects_excessive_entry_name(tmp_path: Path) -> None:
    package_zip = tmp_path / "long-name.zip"
    with zipfile.ZipFile(package_zip, "w") as archive:
        archive.writestr(f"{'x' * 513}.txt", "x")

    importer = SynapseMeetingPackageImporter(
        make_store(tmp_path),
        text_extractor=LocalOrchestrator._extract_text,
    )
    with pytest.raises(ValueError, match="имя файла|Имя файла"):
        importer.inspect(package_zip)


def test_ui_command_emits_follow_ups_and_selects_imported_meeting(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = make_store(tmp_path)
    package = create_package(tmp_path / "ui-package")
    backend = UIBackend(Config.defaults(), EventEmitter(), store)

    backend.handle(
        {
            "command": "import_synapse_package",
            "path": str(package),
            "workspace_id": store.default_workspace_id(),
        }
    )

    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    imported = next(event for event in events if event["type"] == "synapse_package_imported")
    result = imported["result"]
    assert result["status"] == "imported"
    assert result["follow_up_capabilities"][0]["id"] == "prepare_next_meeting"
    assert result["follow_up_capabilities"][-1]["availability"] == "DRAFT_ONLY"
    assert result["capability"]["corporate_api_connected"] is False
    assert backend.current_meeting_id == result["meeting_id"]
    snapshot = [event for event in events if event["type"] == "snapshot"][-1]
    assert snapshot["data"]["current_meeting_id"] == result["meeting_id"]


def test_briefing_keeps_description_and_attachment_snippets_traceable(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = make_store(tmp_path)
    package = create_package(tmp_path / "briefing-package")
    imported = LocalOrchestrator(store).import_synapse_meeting_package(
        package,
        workspace_id=store.default_workspace_id(),
    )
    backend = UIBackend(Config.defaults(), EventEmitter(), store)

    backend.handle(
        {
            "command": "prepare_briefing",
            "meeting_id": imported["meeting_id"],
        }
    )

    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    briefing = [event for event in events if event["type"] == "snapshot"][-1]["data"][
        "meeting_briefing"
    ]
    assert "не смешан с фактами транскрипта" in briefing
    assert "согласовать запуск пилота" in briefing
    assert "30 устройств" in briefing
    assert "[источник " in briefing


@pytest.mark.parametrize("variant", ["malformed", "oversized"])
def test_bad_nested_docx_falls_back_to_metadata_only(
    tmp_path: Path,
    variant: str,
) -> None:
    store = make_store(tmp_path)
    package = create_package(tmp_path / f"docx-{variant}")
    if variant == "malformed":
        docx = b"this is not a zip"
    else:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(
                "word/document.xml",
                b"x" * (MAX_DOCX_DOCUMENT_XML_BYTES + 1),
            )
        docx = buffer.getvalue()
    replace_binary_attachment(
        package,
        relative_path=f"attachments/{variant}.docx",
        title=f"DOCX {variant}",
        data=docx,
    )

    result = LocalOrchestrator(store).import_synapse_meeting_package(
        package,
        workspace_id=store.default_workspace_id(),
    )

    docx_source = next(
        source
        for source in store.sources_by_provenance("synapse", "synapse-demo-42")
        if source["metadata"]["provenance"]["relative_path"].endswith(".docx")
    )
    assert result["status"] == "imported"
    assert docx_source["metadata"]["extraction_status"] == "metadata_only"
    assert f"DOCX {variant}" in docx_source["content"]


def test_same_external_id_with_changed_content_is_a_conflict(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    first = create_package(tmp_path / "first")
    changed = create_package(
        tmp_path / "changed",
        transcript=TRANSCRIPT + "\nАнна: Решили расширить пилот.",
    )
    orchestrator = LocalOrchestrator(store)
    orchestrator.import_synapse_meeting_package(
        first,
        workspace_id=store.default_workspace_id(),
    )

    with pytest.raises(ValueError, match="другим содержимым"):
        orchestrator.import_synapse_meeting_package(
            changed,
            workspace_id=store.default_workspace_id(),
        )

    assert len(store._rows("SELECT * FROM meetings")) == 1


@pytest.mark.parametrize(
    "field",
    ["classification", "organizer", "participants", "part_title"],
)
def test_identity_field_change_is_a_package_conflict(
    tmp_path: Path,
    field: str,
) -> None:
    store = make_store(tmp_path)
    first = create_package(tmp_path / "identity-first")
    changed = create_package(tmp_path / "identity-changed")
    manifest_path = changed / "manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    if field == "classification":
        payload["meeting"]["classification"] = "restricted"
    elif field == "organizer":
        payload["meeting"]["organizer"] = "Наталья"
    elif field == "participants":
        payload["meeting"]["participants"].append("Мария")
    else:
        payload["attachments"][0]["title"] = "Изменённый план"
    manifest_path.write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )
    orchestrator = LocalOrchestrator(store)
    orchestrator.import_synapse_meeting_package(
        first,
        workspace_id=store.default_workspace_id(),
    )

    with pytest.raises(ValueError, match="другим содержимым"):
        orchestrator.import_synapse_meeting_package(
            changed,
            workspace_id=store.default_workspace_id(),
        )


def test_interrupted_partial_graph_is_rolled_back_and_reimported(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = make_store(tmp_path)
    package = create_package(tmp_path / "crash-recovery")
    orchestrator = LocalOrchestrator(store)
    original_add_relation = store.add_source_relation
    calls = 0

    def interrupt_second_relation(*args: object, **kwargs: object) -> dict[str, object]:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise KeyboardInterrupt("simulated process death")
        return original_add_relation(*args, **kwargs)

    monkeypatch.setattr(store, "add_source_relation", interrupt_second_relation)
    with pytest.raises(KeyboardInterrupt, match="process death"):
        orchestrator.import_synapse_meeting_package(
            package,
            workspace_id=store.default_workspace_id(),
        )
    assert store.sources_by_provenance("synapse", "synapse-demo-42")
    assert not store._rows(
        "SELECT id FROM audit_log WHERE action='synapse.package.import'"
    )

    monkeypatch.setattr(store, "add_source_relation", original_add_relation)
    recovered = orchestrator.import_synapse_meeting_package(
        package,
        workspace_id=store.default_workspace_id(),
    )
    repeated = orchestrator.import_synapse_meeting_package(
        package,
        workspace_id=store.default_workspace_id(),
    )

    assert recovered["status"] == "imported"
    assert recovered["recovery"]["mode"] == "rollback_then_reimport"
    assert repeated["status"] == "already_imported"
    assert len(store.sources_by_provenance("synapse", "synapse-demo-42")) == 4
    assert len(store.source_relations(recovered["source_id"])) == 3
    assert len(store._rows("SELECT * FROM meetings")) == 1


def test_import_claim_is_shared_across_store_connections_and_expired_claim_recovers(
    tmp_path: Path,
) -> None:
    database = tmp_path / "data" / "assistant.sqlite3"
    first = AssistantStore(database)
    second = AssistantStore(database)
    package = create_package(tmp_path / "cross-process-claim")
    importer = SynapseMeetingPackageImporter(
        second,
        text_extractor=LocalOrchestrator._extract_text,
    )
    manifest = importer.inspect(package)
    workspace = first.default_workspace_id()
    original = first.claim_source_import(
        "synapse",
        manifest.package_id,
        manifest.fingerprint,
        workspace,
    )

    with pytest.raises(SynapseImportInProgressError, match="другим процессом"):
        importer.import_package(package, workspace_id=workspace)
    assert not second.sources_by_provenance("synapse", manifest.package_id)

    with first.transaction() as connection:
        connection.execute(
            """
            UPDATE source_import_receipts
            SET lease_expires_at='1970-01-01T00:00:00+00:00'
            WHERE source_system='synapse' AND external_id=?
            """,
            (manifest.package_id,),
        )

    recovered = importer.import_package(package, workspace_id=workspace)
    receipt = first.source_import_receipt("synapse", manifest.package_id)

    assert recovered["status"] == "imported"
    assert recovered["recovery"] == {
        "performed": True,
        "removed_sources": 0,
        "mode": "rollback_then_reimport",
    }
    assert receipt is not None
    assert receipt["state"] == "complete"
    assert receipt["primary_source_id"] == recovered["source_id"]
    assert not first.release_source_import_claim(
        "synapse",
        manifest.package_id,
        str(original["claim_token"]),
    )


def test_backend_deletes_synapse_package_related_first_and_only_managed_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = make_store(tmp_path)
    store.trash_dir = tmp_path / "Trash"
    package = create_package(tmp_path / "delete-package")
    imported = LocalOrchestrator(store).import_synapse_meeting_package(
        package,
        workspace_id=store.default_workspace_id(),
    )
    package_sources = store.sources_by_provenance("synapse", imported["package_id"])
    primary_id = imported["source_id"]
    related_ids = {source["id"] for source in package_sources if source["id"] != primary_id}
    managed_paths = [Path(str(source["path"])) for source in package_sources]
    original_paths = [path for path in package.rglob("*") if path.is_file()]
    external_guard = tmp_path / "external-recording-keep.wav"
    external_guard.write_bytes(b"external")
    primary_metadata = store.get_source(primary_id)["metadata"]
    primary_metadata["managed_audio_path"] = str(external_guard)
    with store.transaction() as connection:
        connection.execute(
            "UPDATE sources SET metadata=? WHERE id=?",
            (json.dumps(primary_metadata, ensure_ascii=False), primary_id),
        )
    deletion_order: list[str] = []
    original_delete = store._delete_source_rows

    def record_delete(connection: object, source_id: str) -> None:
        deletion_order.append(source_id)
        original_delete(connection, source_id)

    monkeypatch.setattr(store, "_delete_source_rows", record_delete)
    backend = UIBackend(Config.defaults(), EventEmitter(), store)
    backend.current_meeting_id = imported["meeting_id"]
    capsys.readouterr()

    backend.handle({"command": "delete_source", "source_id": primary_id})

    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    deleted = next(event for event in events if event["type"] == "entity_deleted")
    assert deletion_order[-1] == primary_id
    assert set(deletion_order[:-1]) == related_ids
    assert deleted["package_aware"] is True
    assert deleted["deleted_sources"] == 4
    assert backend.current_meeting_id is None
    assert not store.sources_by_provenance("synapse", imported["package_id"])
    assert not store._rows("SELECT * FROM source_relations")
    assert not store._rows("SELECT * FROM meetings")
    assert store.source_import_receipt("synapse", imported["package_id"]) is None
    assert all(not path.exists() for path in managed_paths)
    assert all(path.exists() for path in original_paths)
    assert external_guard.exists()
    assert len(list(store.trash_dir.iterdir())) == len(managed_paths)
    assert not store._connection.execute("PRAGMA foreign_key_check").fetchall()


def test_completed_but_corrupt_graph_requires_explicit_repair(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = make_store(tmp_path)
    package = create_package(tmp_path / "completed-corrupt")
    orchestrator = LocalOrchestrator(store)
    imported = orchestrator.import_synapse_meeting_package(
        package,
        workspace_id=store.default_workspace_id(),
    )
    relation = store.source_relations(imported["source_id"])[0]
    with store.transaction() as connection:
        connection.execute("DELETE FROM source_relations WHERE id=?", (relation["id"],))

    with pytest.raises(SynapseRepairRequiredError, match="явное восстановление") as error:
        orchestrator.import_synapse_meeting_package(
            package,
            workspace_id=store.default_workspace_id(),
        )
    assert error.value.code == "repair_required"
    assert len(store.sources_by_provenance("synapse", "synapse-demo-42")) == 4

    backend = UIBackend(Config.defaults(), EventEmitter(), store)
    backend.handle(
        {
            "command": "import_synapse_package",
            "path": str(package),
            "workspace_id": store.default_workspace_id(),
        }
    )
    event = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert event["type"] == "synapse_package_repair_required"
    assert event["code"] == "repair_required"


def test_idempotent_retry_preserves_security_escalation_and_item_status(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path)
    package = create_package(tmp_path / "preserve-user-state")
    orchestrator = LocalOrchestrator(store)
    imported = orchestrator.import_synapse_meeting_package(
        package,
        workspace_id=store.default_workspace_id(),
    )
    item = store.get_meeting(imported["meeting_id"], include_items=True)["items"][0]
    store.update_meeting_item_status(item["id"], "done")
    with store.transaction() as connection:
        connection.execute(
            "UPDATE sources SET classification='restricted' WHERE id=?",
            (imported["source_id"],),
        )

    repeated = orchestrator.import_synapse_meeting_package(
        package,
        workspace_id=store.default_workspace_id(),
    )

    assert repeated["status"] == "already_imported"
    assert store.get_source(imported["source_id"])["classification"] == "restricted"
    assert store.get_meeting(imported["meeting_id"], include_items=True)["items"][0][
        "status"
    ] == "done"


def test_same_fingerprint_file_only_residue_is_swept_before_retry(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    package = create_package(tmp_path / "file-residue")
    importer = SynapseMeetingPackageImporter(
        store,
        text_extractor=LocalOrchestrator._extract_text,
    )
    fingerprint = importer.inspect(package).fingerprint
    orphan = store.files_dir / f"synapse-{fingerprint}-interrupted.txt"
    orphan.write_text("partial", encoding="utf-8")

    importer.import_package(package, workspace_id=store.default_workspace_id())

    assert not orphan.exists()


@pytest.mark.parametrize(
    ("mutation", "error"),
    [
        ("traversal", "границ|относительным"),
        ("checksum", "SHA-256"),
        ("secret", "запрещено"),
    ],
)
def test_invalid_package_is_rejected_before_store_mutation(
    tmp_path: Path,
    mutation: str,
    error: str,
) -> None:
    store = make_store(tmp_path)
    package = create_package(tmp_path / mutation)
    manifest_path = package / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if mutation == "traversal":
        manifest["attachments"][0]["path"] = "../outside.md"
    elif mutation == "checksum":
        manifest["transcript"]["sha256"] = "0" * 64
    else:
        manifest["metadata"]["api_token"] = "must-not-be-imported"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

    importer = SynapseMeetingPackageImporter(
        store,
        text_extractor=LocalOrchestrator._extract_text,
    )
    with pytest.raises(ValueError, match=error):
        importer.import_package(package, workspace_id=store.default_workspace_id())

    assert not store._rows("SELECT * FROM meetings")
    assert not store.find_source_by_provenance("synapse", "synapse-demo-42")


def test_failed_relation_rolls_back_sources_and_managed_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = make_store(tmp_path)
    package = create_package(tmp_path / "rollback")

    original_add_relation = store.add_source_relation
    calls = 0

    def fail_second_relation(*args: object, **kwargs: object) -> dict[str, object]:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("simulated relation failure")
        return original_add_relation(*args, **kwargs)

    monkeypatch.setattr(store, "add_source_relation", fail_second_relation)
    with pytest.raises(RuntimeError, match="simulated"):
        LocalOrchestrator(store).import_synapse_meeting_package(
            package,
            workspace_id=store.default_workspace_id(),
        )

    assert not store._rows("SELECT * FROM sources WHERE kind LIKE 'meeting%'")
    assert not store._rows("SELECT * FROM source_relations")
    assert not store._rows("SELECT * FROM meetings")
    assert not store._rows("SELECT * FROM meeting_items")
    assert not store.find_source_by_provenance("synapse", "synapse-demo-42")
    assert not list(store.files_dir.iterdir())


def test_cleanup_failure_does_not_mask_original_import_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = make_store(tmp_path)
    package = create_package(tmp_path / "cleanup-error")

    def fail_relation(*args: object, **kwargs: object) -> dict[str, object]:
        raise RuntimeError("original import failure")

    def fail_rollback(source_id: str) -> None:
        raise OSError(f"cleanup failed for {source_id}")

    original_unlink = Path.unlink

    def fail_managed_unlink(path: Path, *args: object, **kwargs: object) -> None:
        if path.resolve().is_relative_to(store.files_dir.resolve()):
            raise OSError("managed cleanup failed")
        original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(store, "add_source_relation", fail_relation)
    monkeypatch.setattr(store, "rollback_source_import", fail_rollback)
    monkeypatch.setattr(Path, "unlink", fail_managed_unlink)

    with pytest.raises(RuntimeError, match="original import failure"):
        LocalOrchestrator(store).import_synapse_meeting_package(
            package,
            workspace_id=store.default_workspace_id(),
        )


def _managed(store: AssistantStore, path: Path) -> bool:
    return path.exists() and path.resolve().is_relative_to(store.files_dir.resolve())
