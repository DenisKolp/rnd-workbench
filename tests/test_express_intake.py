from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
from typing import Any
import zipfile

import pytest

from voice_assistant.express_intake import (
    ExpressIntakeConfig,
    ExpressIntakeError,
    ExpressMeetingIntake,
    TransportResponse,
    normalize_intake_base_url,
)
from voice_assistant.store import AssistantStore
from voice_assistant.synapse import SynapseMeetingPackageImporter


def _package(root: Path, target: Path) -> Path:
    root.mkdir(parents=True)
    transcript = "Решили начать пилот. Иван подготовит план до пятницы."
    description = "Еженедельная встреча команды RnD."
    (root / "transcript.txt").write_text(transcript, encoding="utf-8")
    (root / "description.md").write_text(description, encoding="utf-8")
    manifest = {
        "schema_version": "1.0",
        "source_system": "synapse",
        "import_mode": "LOCAL_PACKAGE_IMPORT",
        "package_id": "express-meeting-42",
        "meeting": {
            "title": "Пилот RnD",
            "participants": ["Иван"],
            "classification": "confidential",
        },
        "transcript": {
            "path": "transcript.txt",
            "media_type": "text/plain",
            "sha256": hashlib.sha256(transcript.encode()).hexdigest(),
        },
        "description": {
            "path": "description.md",
            "media_type": "text/markdown",
            "sha256": hashlib.sha256(description.encode()).hexdigest(),
        },
        "attachments": [],
        "metadata": {"source": "recordings_bot"},
    }
    (root / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False),
        encoding="utf-8",
    )
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(root.iterdir()):
            archive.write(path, path.name)
    return target


class FakeTransport:
    def __init__(self, index: dict[str, Any], package: Path) -> None:
        self.index = index
        self.package = package
        self.requests: list[str] = []
        self.downloads: list[str] = []
        self.index_statuses: list[int] = []

    def get_bytes(self, path: str, *, max_bytes: int) -> TransportResponse:
        self.requests.append(path)
        status = self.index_statuses.pop(0) if self.index_statuses else 200
        body = json.dumps(self.index).encode()
        return TransportResponse(
            status=status,
            headers={"content-type": "application/json"},
            body=body[:max_bytes],
            size_bytes=min(len(body), max_bytes),
        )

    def download(
        self,
        path: str,
        destination: Path,
        *,
        max_bytes: int,
    ) -> TransportResponse:
        self.downloads.append(path)
        data = self.package.read_bytes()
        assert len(data) <= max_bytes
        shutil.copyfile(self.package, destination)
        return TransportResponse(
            status=200,
            headers={"content-type": "application/zip"},
            size_bytes=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
        )


def _connector(tmp_path: Path) -> tuple[ExpressMeetingIntake, AssistantStore, FakeTransport]:
    archive = _package(tmp_path / "package", tmp_path / "meeting.zip")
    store = AssistantStore(tmp_path / "data" / "assistant.sqlite3")
    importer = SynapseMeetingPackageImporter(store, text_extractor=lambda path: "")
    manifest = importer.inspect(archive)
    data = archive.read_bytes()
    index = {
        "schema_version": 1,
        "items": [
            {
                "package_id": manifest.package_id,
                "package_fingerprint": manifest.fingerprint,
                "archive_sha256": hashlib.sha256(data).hexdigest(),
                "size_bytes": len(data),
                "download_path": "/bridge/v1/meeting-packages/42/content",
                "cursor": "event-42",
                "watermark": "2026-08-31T10:00:00Z",
            }
        ],
        "next_cursor": "event-42",
        "has_more": False,
    }
    transport = FakeTransport(index, archive)
    connector = ExpressMeetingIntake(
        importer,
        ExpressIntakeConfig(
            base_url="https://express-intake.corp.example/bridge",
            bearer_token="memory-only-secret",
        ),
        transport=transport,
        sleeper=lambda _: None,
    )
    return connector, store, transport


@pytest.mark.parametrize(
    "value",
    [
        "http://express.corp.example",
        "https://user:secret@express.corp.example",
        "https://127.0.0.1",
        "https://localhost",
        "https://express.corp.example/path?token=secret",
    ],
)
def test_intake_url_rejects_unsafe_shapes(value: str) -> None:
    with pytest.raises(ValueError):
        normalize_intake_base_url(value)


def test_environment_configuration_is_atomic_and_secret_is_not_in_diagnostics(
    tmp_path: Path,
) -> None:
    store = AssistantStore(tmp_path / "assistant.sqlite3")
    importer = SynapseMeetingPackageImporter(store, text_extractor=lambda path: "")
    with pytest.raises(ValueError, match="URL и токен"):
        ExpressMeetingIntake.from_environment(
            importer,
            {"RND_WORKBENCH_EXPRESS_INTAKE_URL": "https://intake.corp.example"},
        )
    connector = ExpressMeetingIntake.from_environment(importer, {})
    diagnostics = connector.diagnostics()
    assert diagnostics["configured"] is False
    assert "token" not in json.dumps(diagnostics).casefold()


def test_sync_imports_verified_corporate_package_and_is_idempotent(tmp_path: Path) -> None:
    connector, store, transport = _connector(tmp_path)
    workspace_id = store.default_workspace_id()

    first = connector.sync(workspace_id=workspace_id)
    second = connector.sync(workspace_id=workspace_id, cursor=first["next_cursor"])

    assert first["status"] == "succeeded"
    assert first["processed"] == 1
    assert first["connector"]["connected"] is True
    assert second["imported"][0]["status"] == "already_imported"
    assert transport.requests == [
        "/bridge/v1/meeting-packages?limit=20",
        "/bridge/v1/meeting-packages?limit=20&cursor=event-42",
    ]
    sources = store.sources_by_provenance("synapse", "express-meeting-42")
    assert len(sources) == 2
    for source in sources:
        provenance = source["metadata"]["provenance"]
        assert provenance["import_mode"] == "CORPORATE_PACKAGE_IMPORT"
        assert provenance["real_integration"] is True
        assert provenance["connector_id"] == "express-corporate-intake"
        assert "memory-only-secret" not in json.dumps(source)
    primary = next(source for source in sources if source["kind"] == "meeting")
    capability = primary["metadata"]["capability"]
    assert capability["corporate_api_connected"] is True
    assert capability["write_back_available"] is False
    audit = store._rows(
        "SELECT status, detail, origin FROM audit_log WHERE action='synapse.package.import'"
    )
    assert audit[0]["status"] == "succeeded"
    assert audit[0]["origin"] == "express_corporate_intake"
    assert "memory-only-secret" not in audit[0]["detail"]


def test_cursor_does_not_advance_when_package_hash_is_wrong(tmp_path: Path) -> None:
    connector, store, transport = _connector(tmp_path)
    transport.index["items"][0]["archive_sha256"] = "0" * 64

    with pytest.raises(ExpressIntakeError) as raised:
        connector.sync(workspace_id=store.default_workspace_id(), cursor="before")

    assert raised.value.code == "PACKAGE_HASH_MISMATCH"
    assert connector.diagnostics()["connected"] is False
    assert connector.diagnostics()["last_error_code"] == "PACKAGE_HASH_MISMATCH"
    assert not store.sources_by_provenance("synapse", "express-meeting-42")


def test_index_retries_only_safe_transient_gets(tmp_path: Path) -> None:
    connector, store, transport = _connector(tmp_path)
    transport.index_statuses = [503, 429, 200]

    result = connector.sync(workspace_id=store.default_workspace_id())

    assert result["processed"] == 1
    assert len(transport.requests) == 3


@pytest.mark.parametrize(
    "download_path",
    [
        "/other/content",
        "/bridge/v1/meeting-packages/%2e%2e/private/content",
        "/bridge/v1/meeting-packages/42%2f%2e%2e%2fprivate/content",
    ],
)
def test_download_path_cannot_leave_configured_intake_prefix(
    tmp_path: Path,
    download_path: str,
) -> None:
    connector, store, transport = _connector(tmp_path)
    transport.index["items"][0]["download_path"] = download_path

    with pytest.raises(ExpressIntakeError) as raised:
        connector.sync(workspace_id=store.default_workspace_id())

    assert raised.value.code == "DOWNLOAD_PATH_BLOCKED"


def test_bounded_drain_commits_each_complete_page(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connector, store, _ = _connector(tmp_path)
    pages = [
        {
            "status": "succeeded",
            "connector": {},
            "imported": [{"package_id": "one"}],
            "processed": 1,
            "next_cursor": "cursor-1",
            "watermark": "w1",
            "has_more": True,
        },
        {
            "status": "succeeded",
            "connector": {},
            "imported": [{"package_id": "two"}],
            "processed": 1,
            "next_cursor": "cursor-2",
            "watermark": "w2",
            "has_more": False,
        },
    ]
    observed_cursors: list[str | None] = []

    def fake_sync(*, workspace_id: str, cursor: str | None = None):  # noqa: ANN202
        assert workspace_id == store.default_workspace_id()
        observed_cursors.append(cursor)
        return pages.pop(0)

    monkeypatch.setattr(connector, "sync", fake_sync)
    committed: list[tuple[str, str | None]] = []

    result = connector.sync_until_idle(
        workspace_id=store.default_workspace_id(),
        cursor=None,
        commit_checkpoint=lambda cursor, watermark: committed.append(
            (cursor, watermark)
        ),
    )

    assert observed_cursors == [None, "cursor-1"]
    assert committed == [("cursor-1", "w1"), ("cursor-2", "w2")]
    assert result["processed"] == 2
    assert result["pages"] == 2
    assert result["has_more"] is False


def test_bounded_drain_rejects_non_advancing_cursor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connector, store, _ = _connector(tmp_path)
    monkeypatch.setattr(
        connector,
        "sync",
        lambda **_: {
            "status": "succeeded",
            "connector": {},
            "imported": [],
            "processed": 0,
            "next_cursor": "same",
            "watermark": None,
            "has_more": True,
        },
    )
    committed: list[str] = []

    with pytest.raises(ExpressIntakeError) as raised:
        connector.sync_until_idle(
            workspace_id=store.default_workspace_id(),
            cursor="same",
            commit_checkpoint=lambda cursor, _: committed.append(cursor),
        )

    assert raised.value.code == "CURSOR_NOT_ADVANCED"
    assert committed == []
