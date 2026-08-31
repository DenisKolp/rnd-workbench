"""Safe local import contract for eXpress meeting packages (`synapse` alias).

This module is deliberately not a live corporate integration.  It validates a
directory or ZIP export, copies its parts into the application's managed store,
builds traceable meeting analysis, and exposes local/draft-only follow-ups.
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import date, datetime
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import struct
import threading
from typing import Any, Callable, Iterable
from uuid import uuid4
import zipfile

from .meetings import analyze_transcript
from .store import AssistantStore, highest_classification, normalize_classification


SCHEMA_VERSION = "1.0"
SOURCE_SYSTEM = "synapse"
IMPORT_MODE = "LOCAL_PACKAGE_IMPORT"
MANIFEST_NAME = "manifest.json"
MAX_MANIFEST_BYTES = 1 * 1024 * 1024
MAX_TRANSCRIPT_BYTES = 16 * 1024 * 1024
MAX_DESCRIPTION_BYTES = 2 * 1024 * 1024
MAX_ATTACHMENT_BYTES = 50 * 1024 * 1024
MAX_TOTAL_BYTES = 250 * 1024 * 1024
MAX_ATTACHMENTS = 32
MAX_ZIP_ENTRIES = 128
MAX_ZIP_NAME_BYTES = 512
FINGERPRINT_PROFILE = "synapse-meeting-package-fingerprint-v2"
MAX_SUPPORTING_CONTEXT_CHARS = 6_000
MAX_SUPPORTING_SNIPPET_CHARS = 1_200
MAX_SUPPORTING_ATTACHMENTS = 8

_PACKAGE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{2,127}\Z")
_SHA256 = re.compile(r"[a-f0-9]{64}\Z")
_MEDIA_TYPE = re.compile(
    r"[a-z0-9][a-z0-9.+-]{0,63}/[a-z0-9][a-z0-9.+-]{0,63}\Z"
)
_METADATA_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}\Z")
_SECRET_KEY = re.compile(
    r"(?:^|[._:-])(?:secret|token|password|api[_-]?key|authorization|cookie)(?:$|[._:-])",
    re.IGNORECASE,
)
_IMPORT_LOCK = threading.RLock()

FOLLOW_UP_CAPABILITIES: tuple[dict[str, str], ...] = (
    {
        "id": "prepare_next_meeting",
        "availability": "AVAILABLE_LOCAL",
        "effect": "READ_ONLY",
    },
    {
        "id": "analyze_decisions",
        "availability": "AVAILABLE_LOCAL",
        "effect": "READ_ONLY",
    },
    {
        "id": "analyze_actions",
        "availability": "AVAILABLE_LOCAL",
        "effect": "READ_ONLY",
    },
    {
        "id": "analyze_risks",
        "availability": "AVAILABLE_LOCAL",
        "effect": "READ_ONLY",
    },
    {
        "id": "analyze_questions",
        "availability": "AVAILABLE_LOCAL",
        "effect": "READ_ONLY",
    },
    {
        "id": "propose_follow_ups",
        "availability": "DRAFT_ONLY",
        "effect": "LOCAL_DRAFT",
    },
)


class SynapseRepairRequiredError(ValueError):
    """A completed import is inconsistent and must not be auto-deleted."""

    code = "repair_required"


class SynapseImportInProgressError(RuntimeError):
    """Another process owns the durable import lease for this package."""

    code = "import_in_progress"


def synapse_capability(*, checkpoint_accepted: bool = False) -> dict[str, Any]:
    """Truthful capability gate for the current implementation."""

    return {
        "source_system": SOURCE_SYSTEM,
        "import_mode": IMPORT_MODE,
        "package_import_available": True,
        "corporate_api_connected": False,
        "real_integration": False,
        "write_back_available": False,
        "live_connector_available": False,
        "checkpoint_accepted": checkpoint_accepted,
        "supported_delivery_modes": ["POLLING", "WEBHOOK"],
        "reason_code": "CORPORATE_API_NOT_CONNECTED",
        "label": "Локальный импорт пакета; API eXpress (Синапс) не подключён",
    }


@dataclass(frozen=True, slots=True)
class PackagePart:
    role: str
    relative_path: str
    title: str
    media_type: str
    sha256: str
    size_bytes: int
    data: bytes

    def contract(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "relativePath": self.relative_path,
            "title": self.title,
            "mediaType": self.media_type,
            "sha256": self.sha256,
            "sizeBytes": self.size_bytes,
        }


@dataclass(frozen=True, slots=True)
class PackageManifest:
    package_id: str
    title: str
    occurred_at: str | None
    participants: tuple[str, ...]
    organizer: str | None
    classification: str
    metadata: dict[str, str]
    connector_checkpoint: dict[str, str] | None
    parts: tuple[PackagePart, ...]
    fingerprint: str

    def transcript(self) -> PackagePart:
        return next(part for part in self.parts if part.role == "TRANSCRIPT")

    def description(self) -> PackagePart:
        return next(part for part in self.parts if part.role == "DESCRIPTION")

    def attachments(self) -> tuple[PackagePart, ...]:
        return tuple(part for part in self.parts if part.role == "ATTACHMENT")

    def java_contract(self) -> dict[str, Any]:
        contract: dict[str, Any] = {
            "schemaVersion": SCHEMA_VERSION,
            "sourceSystem": SOURCE_SYSTEM,
            "importMode": IMPORT_MODE,
            "packageId": self.package_id,
            "title": self.title,
            "occurredAt": self.occurred_at,
            "organizer": self.organizer,
            "classification": self.classification,
            "participants": list(self.participants),
            "parts": [part.contract() for part in self.parts],
            "metadata": dict(sorted(self.metadata.items())),
        }
        if self.connector_checkpoint is not None:
            contract["connectorCheckpoint"] = {
                "deliveryMode": self.connector_checkpoint["delivery_mode"],
                "cursor": self.connector_checkpoint.get("cursor"),
                "watermark": self.connector_checkpoint.get("watermark"),
            }
        return contract


class _PackageReader(AbstractContextManager["_PackageReader"]):
    def read(self, relative_path: str, *, max_bytes: int) -> bytes:
        raise NotImplementedError

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        return None


class _DirectoryReader(_PackageReader):
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def read(self, relative_path: str, *, max_bytes: int) -> bytes:
        safe = _safe_relative_path(relative_path)
        candidate = self.root / safe
        path = candidate.resolve()
        if (
            not path.is_relative_to(self.root)
            or candidate.is_symlink()
            or not path.is_file()
        ):
            raise ValueError(f"Файл пакета не найден или небезопасен: {relative_path}")
        size = path.stat().st_size
        if size <= 0 or size > max_bytes:
            raise ValueError(f"Файл пакета превышает допустимый размер: {relative_path}")
        with path.open("rb") as stream:
            data = stream.read(max_bytes + 1)
        if len(data) != size or len(data) > max_bytes:
            raise ValueError(f"Некорректный размер файла пакета: {relative_path}")
        return data


class _ZipReader(_PackageReader):
    def __init__(self, path: Path) -> None:
        self.archive = zipfile.ZipFile(path)
        infos: dict[str, zipfile.ZipInfo] = {}
        try:
            archive_entries = self.archive.infolist()
            if len(archive_entries) > MAX_ZIP_ENTRIES:
                raise ValueError(
                    f"В ZIP допустимо не более {MAX_ZIP_ENTRIES} записей"
                )
            for info in archive_entries:
                if len(info.filename.encode("utf-8", errors="surrogatepass")) > (
                    MAX_ZIP_NAME_BYTES
                ):
                    raise ValueError("Имя файла в ZIP превышает допустимую длину")
                if info.is_dir():
                    continue
                safe = _safe_relative_path(info.filename)
                if safe in infos:
                    raise ValueError(f"В ZIP повторяется путь: {safe}")
                if info.flag_bits & 0x1:
                    raise ValueError("Зашифрованные ZIP-пакеты не поддерживаются")
                infos[safe] = info
        except Exception:
            self.archive.close()
            raise
        self.infos = infos

    def read(self, relative_path: str, *, max_bytes: int) -> bytes:
        safe = _safe_relative_path(relative_path)
        info = self.infos.get(safe)
        if info is None:
            raise ValueError(f"Файл отсутствует в ZIP-пакете: {relative_path}")
        if info.file_size <= 0 or info.file_size > max_bytes:
            raise ValueError(f"Файл пакета превышает допустимый размер: {relative_path}")
        with self.archive.open(info) as stream:
            data = stream.read(max_bytes + 1)
        if len(data) != info.file_size or len(data) > max_bytes:
            raise ValueError(f"Некорректный размер файла пакета: {relative_path}")
        return data

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.archive.close()


class SynapseMeetingPackageImporter:
    """Import an eXpress (Синапс) export without any network access."""

    def __init__(
        self,
        store: AssistantStore,
        *,
        text_extractor: Callable[[Path], str],
    ) -> None:
        self.store = store
        self.text_extractor = text_extractor

    def inspect(self, package_path: Path) -> PackageManifest:
        package_path = package_path.expanduser()
        if package_path.is_dir():
            reader: _PackageReader = _DirectoryReader(package_path)
        elif package_path.is_file() and package_path.suffix.casefold() == ".zip":
            reader = _ZipReader(package_path)
        else:
            raise ValueError(
                "Пакет eXpress (Синапс) должен быть каталогом или ZIP-файлом"
            )
        with reader:
            manifest_bytes = reader.read(MANIFEST_NAME, max_bytes=MAX_MANIFEST_BYTES)
            manifest_json = _strict_json(manifest_bytes)
            return _parse_manifest(manifest_json, reader)

    def import_package(
        self,
        package_path: Path,
        *,
        workspace_id: str | None,
    ) -> dict[str, Any]:
        # Avoid duplicate work inside one backend. The durable SQLite receipt
        # below owns the package identity across processes; individual graph
        # writes remain recoverable multi-transaction steps guarded by a lease.
        with _IMPORT_LOCK:
            return self._import_package_locked(
                package_path,
                workspace_id=workspace_id,
            )

    def _import_package_locked(
        self,
        package_path: Path,
        *,
        workspace_id: str | None,
    ) -> dict[str, Any]:
        manifest = self.inspect(package_path)
        workspace_id = workspace_id or self.store.default_workspace_id()
        workspace = self.store.get_workspace(workspace_id)
        classification = highest_classification(
            [str(workspace["classification"]), manifest.classification]
        )
        claim = self.store.claim_source_import(
            SOURCE_SYSTEM,
            manifest.package_id,
            manifest.fingerprint,
            workspace_id,
        )
        if claim["status"] == "busy":
            raise SynapseImportInProgressError(
                "Этот пакет eXpress (Синапс) уже импортируется другим процессом"
            )
        claim_token = str(claim.get("claim_token") or "")
        try:
            return self._import_package_claimed(
                manifest,
                workspace_id=workspace_id,
                classification=classification,
                claim=claim,
            )
        finally:
            if claim_token:
                try:
                    self.store.release_source_import_claim(
                        SOURCE_SYSTEM,
                        manifest.package_id,
                        claim_token,
                    )
                except Exception:
                    # Do not hide a completed import or its original failure.
                    # An orphan claim remains recoverable after lease expiry.
                    pass

    def _import_package_claimed(
        self,
        manifest: PackageManifest,
        *,
        workspace_id: str,
        classification: str,
        claim: dict[str, Any],
    ) -> dict[str, Any]:

        package_sources = self.store.sources_by_provenance(
            SOURCE_SYSTEM,
            manifest.package_id,
        )
        primaries = [
            source
            for source in package_sources
            if source["kind"] == "meeting"
            and source["metadata"]["provenance"].get("part_role") == "transcript"
        ]
        if any(source.get("workspace_id") != workspace_id for source in package_sources):
            raise ValueError(
                "Пакет с таким идентификатором уже связан с другим рабочим пространством"
            )
        if any(
            source["metadata"]["provenance"].get("package_fingerprint")
            != manifest.fingerprint
            for source in package_sources
        ):
            raise ValueError(
                "Пакет с таким идентификатором уже импортирован с другим содержимым"
            )
        if len(primaries) == 1 and self._is_complete_graph(
            manifest,
            primaries[0],
            package_sources,
            workspace_id=workspace_id,
            classification=classification,
        ):
            self._complete_claim(claim, manifest, workspace_id, primaries[0]["id"])
            return self.context(primaries[0]["id"], status="already_imported")

        if claim["status"] == "complete" or (
            primaries and self._has_completion_marker(primaries)
        ):
            raise SynapseRepairRequiredError(
                "Завершённый импорт eXpress (Синапс) повреждён; "
                "требуется явное восстановление"
            )

        recovered_sources = 0
        if package_sources:
            for source in sorted(
                package_sources,
                key=lambda item: item["kind"] == "meeting",
            ):
                try:
                    self.store.rollback_source_import(str(source["id"]))
                    recovered_sources += 1
                except KeyError:
                    continue
                except Exception as error:
                    raise RuntimeError(
                        "Не удалось безопасно очистить незавершённый импорт eXpress"
                    ) from error
            if self.store.sources_by_provenance(SOURCE_SYSTEM, manifest.package_id):
                raise ValueError(
                    "Незавершённый импорт eXpress не удалось очистить полностью"
                )
        self._cleanup_orphan_managed_files(manifest.fingerprint)
        self._renew_claim(claim, manifest)

        created_sources: list[dict[str, Any]] = []
        managed_paths: list[Path] = []
        try:
            transcript_part = manifest.transcript()
            transcript_text = _decode_utf8(transcript_part, "транскрипт")
            managed_transcript = self._write_managed(
                transcript_part,
                package_fingerprint=manifest.fingerprint,
            )
            managed_paths.append(managed_transcript)
            primary = self.store.add_source(
                workspace_id,
                "meeting",
                manifest.title,
                transcript_text[:2_000_000],
                path=str(managed_transcript),
                metadata={
                    "provenance": _part_provenance(manifest, transcript_part),
                    "package_metadata": manifest.metadata,
                    "organizer": manifest.organizer,
                    "declared_participants": list(manifest.participants),
                    "capability": synapse_capability(
                        checkpoint_accepted=manifest.connector_checkpoint is not None
                    ),
                    "follow_up_capabilities": _capability_list(),
                },
                visibility="workspace",
                classification=classification,
            )
            created_sources.append(primary)

            analysis = analyze_transcript(
                transcript_text[:2_000_000],
                title=manifest.title,
                occurred_at=manifest.occurred_at,
            )
            analysis["participants"] = _deduplicate(
                [*manifest.participants, *analysis["participants"]]
            )
            meeting = self.store.upsert_meeting_analysis(primary["id"], analysis)

            description_part = manifest.description()
            description_text = _decode_utf8(description_part, "описание")
            managed_description = self._write_managed(
                description_part,
                package_fingerprint=manifest.fingerprint,
            )
            managed_paths.append(managed_description)
            description = self.store.add_source(
                workspace_id,
                "meeting_description",
                f"{manifest.title} — описание",
                description_text[:500_000],
                path=str(managed_description),
                metadata={
                    "provenance": _part_provenance(
                        manifest,
                        description_part,
                        primary_source_id=primary["id"],
                    )
                },
                visibility="workspace",
                classification=classification,
            )
            created_sources.append(description)
            self.store.add_source_relation(
                primary["id"],
                description["id"],
                "synapse.description",
                metadata=_relation_metadata(description_part),
            )

            for part in manifest.attachments():
                self._renew_claim(claim, manifest)
                managed_attachment = self._write_managed(
                    part,
                    package_fingerprint=manifest.fingerprint,
                )
                managed_paths.append(managed_attachment)
                extraction_status = "indexed"
                try:
                    attachment_text = self.text_extractor(managed_attachment).strip()
                except (OSError, ValueError, UnicodeError):
                    attachment_text = ""
                    extraction_status = "metadata_only"
                if not attachment_text:
                    attachment_text = (
                        f"Вложение пакета встречи: {part.title}. "
                        f"Тип: {part.media_type}; размер: {part.size_bytes} байт."
                    )
                    extraction_status = "metadata_only"
                attachment = self.store.add_source(
                    workspace_id,
                    "meeting_attachment",
                    part.title,
                    attachment_text[:2_000_000],
                    path=str(managed_attachment),
                    metadata={
                        "provenance": _part_provenance(
                            manifest,
                            part,
                            primary_source_id=primary["id"],
                        ),
                        "extraction_status": extraction_status,
                    },
                    visibility="workspace",
                    classification=classification,
                )
                created_sources.append(attachment)
                self.store.add_source_relation(
                    primary["id"],
                    attachment["id"],
                    "synapse.attachment",
                    metadata={
                        **_relation_metadata(part),
                        "extraction_status": extraction_status,
                    },
                )

            self._renew_claim(claim, manifest)
            self.store.audit(
                None,
                "synapse.package.import",
                primary["id"],
                "local_mock",
                json.dumps(
                    {
                        "package_id": manifest.package_id,
                        "package_fingerprint": manifest.fingerprint,
                        "parts": len(manifest.parts),
                        "real_integration": False,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                actor="local-user",
                origin="local_package_import",
            )
            result = self.context(primary["id"], status="imported")
            self._complete_claim(claim, manifest, workspace_id, primary["id"])
            result["meeting_id"] = meeting["id"]
            if recovered_sources or bool(claim.get("recovered")):
                result["recovery"] = {
                    "performed": True,
                    "removed_sources": recovered_sources,
                    "mode": "rollback_then_reimport",
                }
            return result
        except Exception:
            for source in reversed(created_sources):
                try:
                    self.store.rollback_source_import(str(source["id"]))
                except Exception:
                    # Preserve the import failure: rollback is best-effort and
                    # every managed path is attempted again below.
                    pass
            for managed_path in managed_paths:
                try:
                    managed_path.unlink(missing_ok=True)
                except Exception:
                    # A failed cleanup must not hide the original import error.
                    pass
            raise

    def _renew_claim(
        self,
        claim: dict[str, Any],
        manifest: PackageManifest,
    ) -> None:
        claim_token = str(claim.get("claim_token") or "")
        if claim["status"] != "claimed" or not claim_token:
            return
        self.store.renew_source_import_claim(
            SOURCE_SYSTEM,
            manifest.package_id,
            claim_token,
        )

    def _complete_claim(
        self,
        claim: dict[str, Any],
        manifest: PackageManifest,
        workspace_id: str,
        primary_source_id: str,
    ) -> None:
        if claim["status"] == "complete":
            if claim.get("primary_source_id") != primary_source_id:
                raise SynapseRepairRequiredError(
                    "Complete receipt eXpress указывает на другой primary source"
                )
            return
        claim_token = str(claim.get("claim_token") or "")
        if not claim_token:
            raise RuntimeError("Импорт eXpress выполняется без действующего claim")
        self.store.complete_source_import(
            SOURCE_SYSTEM,
            manifest.package_id,
            manifest.fingerprint,
            workspace_id,
            claim_token,
            primary_source_id,
        )

    def _is_complete_graph(
        self,
        manifest: PackageManifest,
        primary: dict[str, Any],
        package_sources: list[dict[str, Any]],
        *,
        workspace_id: str,
        classification: str,
    ) -> bool:
        if len(package_sources) != len(manifest.parts):
            return False
        expected = {
            (part.role.casefold(), part.relative_path): part for part in manifest.parts
        }
        actual: dict[tuple[str, str], tuple[dict[str, Any], PackagePart]] = {}
        for source in package_sources:
            provenance = source["metadata"].get("provenance")
            if not isinstance(provenance, dict):
                return False
            key = (
                str(provenance.get("part_role") or ""),
                str(provenance.get("relative_path") or ""),
            )
            part = expected.get(key)
            if part is None or key in actual:
                return False
            if not self._source_matches_part(
                source,
                primary["id"],
                manifest,
                part,
                workspace_id=workspace_id,
                classification=classification,
            ):
                return False
            actual[key] = (source, part)
        if set(actual) != set(expected):
            return False
        transcript_key = ("transcript", manifest.transcript().relative_path)
        if actual[transcript_key][0]["id"] != primary["id"]:
            return False
        try:
            meeting = self.store.get_meeting_by_source(
                primary["id"],
                include_items=True,
            )
        except KeyError:
            return False
        if (
            meeting["workspace_id"] != workspace_id
            or meeting["title"] != manifest.title
            or meeting.get("occurred_at") != manifest.occurred_at
        ):
            return False

        expected_relations = {
            (
                "synapse.description"
                if role == "description"
                else "synapse.attachment",
                source["id"],
            ): part
            for (role, _), (source, part) in actual.items()
            if role != "transcript"
        }
        actual_relations = {
            (relation["relation_type"], relation["related_source_id"]): relation
            for relation in self.store.source_relations(primary["id"])
            if str(relation["relation_type"]).startswith("synapse.")
        }
        if set(actual_relations) != set(expected_relations):
            return False
        for key, part in expected_relations.items():
            relation_metadata = actual_relations[key]["metadata"]
            if any(
                relation_metadata.get(field) != value
                for field, value in _relation_metadata(part).items()
            ):
                return False
        return bool(
            self.store._rows(
                """
                SELECT id FROM audit_log
                WHERE action='synapse.package.import' AND target=? AND status='local_mock'
                LIMIT 1
                """,
                (primary["id"],),
            )
        )

    def _source_matches_part(
        self,
        source: dict[str, Any],
        primary_source_id: str,
        manifest: PackageManifest,
        part: PackagePart,
        *,
        workspace_id: str,
        classification: str,
    ) -> bool:
        provenance = source["metadata"]["provenance"]
        if (
            source.get("workspace_id") != workspace_id
            or source.get("visibility") != "workspace"
            or highest_classification(
                [str(source.get("classification") or "internal"), classification]
            )
            != source.get("classification")
            or provenance.get("source_system") != SOURCE_SYSTEM
            or provenance.get("import_mode") != IMPORT_MODE
            or provenance.get("external_id") != manifest.package_id
            or provenance.get("package_fingerprint") != manifest.fingerprint
            or provenance.get("fingerprint_profile") != FINGERPRINT_PROFILE
            or provenance.get("part_title") != part.title
            or provenance.get("media_type") != part.media_type
            or provenance.get("sha256") != part.sha256
            or provenance.get("size_bytes") != part.size_bytes
            or provenance.get("real_integration") is not False
        ):
            return False
        expected_kind = {
            "TRANSCRIPT": "meeting",
            "DESCRIPTION": "meeting_description",
            "ATTACHMENT": "meeting_attachment",
        }[part.role]
        if source.get("kind") != expected_kind:
            return False
        if part.role == "TRANSCRIPT":
            if source["id"] != primary_source_id:
                return False
            if (
                source.get("title") != manifest.title
                or source["metadata"].get("organizer") != manifest.organizer
                or source["metadata"].get("declared_participants")
                != list(manifest.participants)
            ):
                return False
        elif provenance.get("primary_source_id") != primary_source_id:
            return False
        raw_path = source.get("path")
        if not raw_path:
            return False
        try:
            path = Path(str(raw_path)).resolve()
            if not path.is_relative_to(self.store.files_dir.resolve()):
                return False
            if not path.is_file() or path.stat().st_size != part.size_bytes:
                return False
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    digest.update(chunk)
            return digest.hexdigest() == part.sha256
        except OSError:
            return False

    def _has_completion_marker(self, primaries: list[dict[str, Any]]) -> bool:
        return any(
            self.store._rows(
                """
                SELECT id FROM audit_log
                WHERE action='synapse.package.import' AND target=? AND status='local_mock'
                LIMIT 1
                """,
                (primary["id"],),
            )
            for primary in primaries
        )

    def _cleanup_orphan_managed_files(self, package_fingerprint: str) -> None:
        prefix = f"synapse-{package_fingerprint}-"
        referenced = {
            str(Path(str(row["path"])).resolve())
            for row in self.store._rows(
                "SELECT path FROM sources WHERE path IS NOT NULL"
            )
        }
        for path in self.store.files_dir.iterdir():
            if not path.name.startswith(prefix) or not path.is_file():
                continue
            if str(path.resolve()) in referenced:
                continue
            try:
                path.unlink(missing_ok=True)
            except OSError:
                # Orphans are metadata-free and never used by context lookup;
                # a later retry can attempt the sweep again.
                pass

    def context(self, primary_source_id: str, *, status: str = "loaded") -> dict[str, Any]:
        primary = self.store.get_source(primary_source_id)
        provenance = primary.get("metadata", {}).get("provenance")
        if not isinstance(provenance, dict) or (
            provenance.get("source_system") != SOURCE_SYSTEM
            or provenance.get("part_role") != "transcript"
        ):
            raise ValueError(
                "Источник не является транскриптом пакета eXpress (Синапс)"
            )
        meeting = self.store.get_meeting_by_source(primary_source_id, include_items=True)
        relations = self.store.source_relations(primary_source_id)
        analysis = {
            "decisions": [],
            "actions": [],
            "risks": [],
            "questions": [],
        }
        kind_targets = {
            "decision": "decisions",
            "action": "actions",
            "commitment": "actions",
            "risk": "risks",
            "question": "questions",
        }
        for item in meeting["items"]:
            target = kind_targets.get(item["kind"])
            if target:
                analysis[target].append(_traceable_item(primary_source_id, item))
        agenda = _next_meeting_agenda(primary_source_id, meeting["items"])
        proposals = _follow_up_proposals(
            str(provenance["external_id"]),
            primary_source_id,
            meeting["items"],
        )
        supporting_context = self._supporting_context(relations)
        return {
            "status": status,
            "package_id": provenance["external_id"],
            "package_fingerprint": provenance["package_fingerprint"],
            "fingerprint_profile": provenance["fingerprint_profile"],
            "source_id": primary_source_id,
            "meeting_id": meeting["id"],
            "title": meeting["title"],
            "capability": synapse_capability(
                checkpoint_accepted=bool(provenance.get("connector_checkpoint"))
            ),
            "follow_up_capabilities": _capability_list(),
            "analysis": analysis,
            "supporting_context": supporting_context,
            "next_meeting": {
                "mode": "local_draft",
                "agenda": agenda,
            },
            "proposals": proposals,
            "provenance": {
                "primary_source": {
                    "id": primary_source_id,
                    "title": primary["title"],
                    "path": primary.get("path"),
                    "part": provenance,
                },
                "related_sources": relations,
            },
        }

    def _supporting_context(
        self,
        relations: list[dict[str, Any]],
    ) -> dict[str, Any]:
        budget = MAX_SUPPORTING_CONTEXT_CHARS
        description: dict[str, Any] | None = None
        attachments: list[dict[str, Any]] = []
        truncated = False
        # The meeting description is first-party framing from the organizer and
        # must not be crowded out by alphabetically earlier attachment rows.
        ordered_relations = sorted(
            enumerate(relations),
            key=lambda item: (
                item[1]["relation_type"] != "synapse.description",
                item[0],
            ),
        )
        for _, relation in ordered_relations:
            is_description = relation["relation_type"] == "synapse.description"
            if not is_description and len(attachments) >= MAX_SUPPORTING_ATTACHMENTS:
                truncated = True
                continue
            if budget <= 0:
                truncated = True
                continue
            source = self.store.get_source(str(relation["related_source_id"]))
            limit = min(MAX_SUPPORTING_SNIPPET_CHARS, budget)
            content = str(source.get("content") or "")
            snippet = _bounded_snippet(content, limit)
            if not snippet:
                continue
            normalized_content = re.sub(r"\s+", " ", content).strip()
            if len(normalized_content) > len(snippet.rstrip("…")):
                truncated = True
            item = {
                "kind": "description" if is_description else "attachment",
                "title": source["title"],
                "snippet": snippet,
                "provenance": {
                    "source_id": source["id"],
                    "relation_type": relation["relation_type"],
                    "classification": source["classification"],
                    "part": relation["metadata"],
                },
            }
            budget -= len(snippet)
            if is_description:
                if description is not None:
                    truncated = True
                    continue
                description = item
            else:
                attachments.append(item)
        attachment_total = sum(
            relation["relation_type"] == "synapse.attachment"
            for relation in relations
        )
        description_expected = any(
            relation["relation_type"] == "synapse.description"
            for relation in relations
        )
        return {
            "boundary": "supporting_sources_not_transcript_facts",
            "description": description,
            "attachments": attachments,
            "truncated": truncated
            or len(attachments) < attachment_total
            or (description_expected and description is None),
        }

    def _write_managed(
        self,
        part: PackagePart,
        *,
        package_fingerprint: str,
    ) -> Path:
        suffix = PurePosixPath(part.relative_path).suffix.casefold()
        if not suffix:
            suffix = ".bin"
        target = self.store.files_dir / (
            f"synapse-{package_fingerprint}-{uuid4().hex}{suffix}"
        )
        try:
            target.write_bytes(part.data)
        except Exception:
            try:
                target.unlink(missing_ok=True)
            except Exception:
                pass
            raise
        return target


def _parse_manifest(payload: Any, reader: _PackageReader) -> PackageManifest:
    root = _mapping(payload, "manifest")
    _exact_keys(
        root,
        required={
            "schema_version",
            "source_system",
            "import_mode",
            "package_id",
            "meeting",
            "transcript",
            "description",
            "attachments",
            "metadata",
        },
        allowed={
            "schema_version",
            "source_system",
            "import_mode",
            "package_id",
            "meeting",
            "transcript",
            "description",
            "attachments",
            "metadata",
            "connector_checkpoint",
        },
        label="manifest",
    )
    if root["schema_version"] != SCHEMA_VERSION:
        raise ValueError("Поддерживается только schema_version 1.0")
    if str(root["source_system"]).casefold() != SOURCE_SYSTEM:
        raise ValueError("Пакет не является экспортом eXpress (Синапс)")
    if str(root["import_mode"]).upper() != IMPORT_MODE:
        raise ValueError("Поддерживается только локальный импорт пакета")
    package_id = _bounded_text(root["package_id"], "package_id", 128)
    if not _PACKAGE_ID.fullmatch(package_id):
        raise ValueError("Некорректный package_id")

    meeting = _mapping(root["meeting"], "meeting")
    _exact_keys(
        meeting,
        required={"title", "participants"},
        allowed={"title", "occurred_at", "participants", "organizer", "classification"},
        label="meeting",
    )
    title = _bounded_text(meeting["title"], "meeting.title", 240)
    occurred_at = _optional_iso(meeting.get("occurred_at"))
    raw_participants = meeting["participants"]
    if not isinstance(raw_participants, list) or len(raw_participants) > 200:
        raise ValueError("meeting.participants должен быть массивом")
    participants = tuple(
        _canonical_participants(
            [_bounded_text(item, "participant", 160) for item in raw_participants]
        )
    )
    organizer = (
        _bounded_text(meeting["organizer"], "meeting.organizer", 160)
        if meeting.get("organizer") is not None
        else None
    )
    classification = normalize_classification(meeting.get("classification") or "internal")
    metadata = _metadata(root["metadata"])
    connector_checkpoint = _connector_checkpoint(root.get("connector_checkpoint"))

    raw_attachments = root["attachments"]
    if not isinstance(raw_attachments, list) or len(raw_attachments) > MAX_ATTACHMENTS:
        raise ValueError(f"Допустимо не более {MAX_ATTACHMENTS} вложений")
    specs = [
        ("TRANSCRIPT", root["transcript"], MAX_TRANSCRIPT_BYTES),
        ("DESCRIPTION", root["description"], MAX_DESCRIPTION_BYTES),
        *[("ATTACHMENT", item, MAX_ATTACHMENT_BYTES) for item in raw_attachments],
    ]
    parts: list[PackagePart] = []
    seen_paths: set[str] = set()
    total_bytes = 0
    for role, raw_spec, limit in specs:
        spec = _part_spec(raw_spec, role)
        if spec["path"] in seen_paths:
            raise ValueError(f"Путь части пакета повторяется: {spec['path']}")
        seen_paths.add(spec["path"])
        data = reader.read(spec["path"], max_bytes=limit)
        actual_sha256 = hashlib.sha256(data).hexdigest()
        declared_sha256 = spec.get("sha256")
        if declared_sha256 and declared_sha256 != actual_sha256:
            raise ValueError(f"SHA-256 не совпадает для {spec['path']}")
        total_bytes += len(data)
        if total_bytes > MAX_TOTAL_BYTES:
            raise ValueError("Суммарный размер пакета превышает 250 МБ")
        parts.append(
            PackagePart(
                role=role,
                relative_path=spec["path"],
                title=spec["title"],
                media_type=spec["media_type"],
                sha256=actual_sha256,
                size_bytes=len(data),
                data=data,
            )
        )
    fingerprint = meeting_package_fingerprint(
        package_id=package_id,
        title=title,
        occurred_at=occurred_at,
        organizer=organizer,
        classification=classification,
        participants=participants,
        metadata=metadata,
        connector_checkpoint=connector_checkpoint,
        parts=parts,
    )
    return PackageManifest(
        package_id=package_id,
        title=title,
        occurred_at=occurred_at,
        participants=participants,
        organizer=organizer,
        classification=classification,
        metadata=metadata,
        connector_checkpoint=connector_checkpoint,
        parts=tuple(parts),
        fingerprint=fingerprint,
    )


def _part_spec(value: Any, role: str) -> dict[str, str]:
    spec = _mapping(value, role.casefold())
    _exact_keys(
        spec,
        required={"path"},
        allowed={"path", "title", "media_type", "sha256"},
        label=role.casefold(),
    )
    path = _safe_relative_path(_bounded_text(spec["path"], "part.path", 240))
    title = _bounded_text(
        spec.get("title") or PurePosixPath(path).name,
        "part.title",
        240,
    )
    media_type = str(spec.get("media_type") or _infer_media_type(path)).casefold()
    if not _MEDIA_TYPE.fullmatch(media_type):
        raise ValueError(f"Некорректный media_type части: {path}")
    sha256 = str(spec.get("sha256") or "").casefold()
    if sha256 and not _SHA256.fullmatch(sha256):
        raise ValueError(f"Некорректный SHA-256 части: {path}")
    return {"path": path, "title": title, "media_type": media_type, "sha256": sha256}


def _strict_json(data: bytes) -> Any:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("manifest.json должен быть UTF-8") from error

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"В manifest.json повторяется поле: {key}")
            result[key] = value
        return result

    try:
        return json.loads(text, object_pairs_hook=reject_duplicates)
    except json.JSONDecodeError as error:
        raise ValueError("manifest.json содержит некорректный JSON") from error


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValueError(f"{label} должен быть JSON-объектом")
    return value


def _exact_keys(
    value: dict[str, Any],
    *,
    required: set[str],
    allowed: set[str],
    label: str,
) -> None:
    missing = required - value.keys()
    unknown = value.keys() - allowed
    if missing or unknown:
        raise ValueError(
            f"Некорректные поля {label}: missing={sorted(missing)}, unknown={sorted(unknown)}"
        )


def _safe_relative_path(value: str) -> str:
    if (
        not isinstance(value, str)
        or "\\" in value
        or "//" in value
        or value.endswith("/")
        or value.startswith(("/", "\\"))
    ):
        raise ValueError("Путь части должен быть относительным POSIX-путём")
    path = PurePosixPath(value)
    if not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("Путь части выходит за границы пакета")
    return path.as_posix()


def _bounded_text(value: Any, label: str, max_length: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} должен быть строкой")
    normalized = value.strip()
    if not normalized or len(normalized) > max_length:
        raise ValueError(f"Некорректная длина {label}")
    return normalized


def _optional_iso(value: Any) -> str | None:
    if value in (None, ""):
        return None
    normalized = _bounded_text(value, "meeting.occurred_at", 64)
    try:
        if len(normalized) == 10:
            date.fromisoformat(normalized)
        else:
            parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
            if parsed.utcoffset() is None:
                raise ValueError("timezone is required")
    except ValueError as error:
        raise ValueError("meeting.occurred_at должен быть ISO-8601") from error
    return normalized


def _metadata(value: Any) -> dict[str, str]:
    raw = _mapping(value, "metadata")
    if len(raw) > 64:
        raise ValueError("В metadata допустимо не более 64 полей")
    normalized: dict[str, str] = {}
    for key, item in raw.items():
        if not _METADATA_KEY.fullmatch(key) or _SECRET_KEY.search(key):
            raise ValueError(f"Поле metadata запрещено: {key}")
        if isinstance(item, bool):
            text = "true" if item else "false"
        elif isinstance(item, (int, float)) and not isinstance(item, bool):
            text = json.dumps(item, ensure_ascii=False, allow_nan=False)
        elif isinstance(item, str):
            text = item.strip()
        else:
            raise ValueError("metadata поддерживает только строки, числа и boolean")
        if not text or len(text) > 512:
            raise ValueError(f"Некорректное значение metadata: {key}")
        normalized[key] = text
    return dict(sorted(normalized.items()))


def _connector_checkpoint(value: Any) -> dict[str, str] | None:
    if value is None:
        return None
    checkpoint = _mapping(value, "connector_checkpoint")
    _exact_keys(
        checkpoint,
        required={"delivery_mode"},
        allowed={"delivery_mode", "cursor", "watermark"},
        label="connector_checkpoint",
    )
    delivery_mode = _bounded_text(
        checkpoint["delivery_mode"],
        "connector_checkpoint.delivery_mode",
        16,
    ).upper()
    if delivery_mode not in {"POLLING", "WEBHOOK"}:
        raise ValueError("delivery_mode должен быть POLLING или WEBHOOK")

    def opaque(field: str) -> str | None:
        raw = checkpoint.get(field)
        if raw in (None, ""):
            return None
        result = _bounded_text(raw, f"connector_checkpoint.{field}", 512)
        if any(ord(character) < 32 or ord(character) == 127 for character in result):
            raise ValueError(f"connector_checkpoint.{field} содержит control characters")
        return result

    cursor = opaque("cursor")
    watermark = opaque("watermark")
    if cursor is None and watermark is None:
        raise ValueError("connector_checkpoint требует cursor или watermark")
    result = {"delivery_mode": delivery_mode}
    if cursor is not None:
        result["cursor"] = cursor
    if watermark is not None:
        result["watermark"] = watermark
    return result


def _infer_media_type(relative_path: str) -> str:
    suffix = PurePosixPath(relative_path).suffix.casefold()
    fixed = {
        ".csv": "text/csv",
        ".json": "application/json",
        ".log": "text/plain",
        ".markdown": "text/markdown",
        ".md": "text/markdown",
        ".pdf": "application/pdf",
        ".txt": "text/plain",
        ".xml": "application/xml",
    }
    return fixed.get(suffix, "application/octet-stream")


def _decode_utf8(part: PackagePart, label: str) -> str:
    try:
        value = part.data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError(f"{label} должен быть UTF-8") from error
    if not value.strip():
        raise ValueError(f"{label} не содержит текста")
    return value


def meeting_package_fingerprint(
    *,
    package_id: str,
    title: str,
    occurred_at: str | None,
    organizer: str | None,
    classification: str,
    participants: Iterable[str],
    metadata: dict[str, str],
    connector_checkpoint: dict[str, str] | None = None,
    parts: Iterable[PackagePart],
) -> str:
    digest = hashlib.sha256()

    def update(value: str) -> None:
        encoded = value.encode("utf-8")
        digest.update(struct.pack(">I", len(encoded)))
        digest.update(encoded)

    canonical_participants = _canonical_participants(participants)
    ordered_metadata = sorted(metadata.items())
    ordered_parts = sorted(parts, key=lambda item: (item.role, item.relative_path))

    for value in (
        FINGERPRINT_PROFILE,
        SCHEMA_VERSION,
        SOURCE_SYSTEM,
        IMPORT_MODE,
        package_id,
        title,
        occurred_at or "",
        organizer or "",
        normalize_classification(classification),
    ):
        update(value)
    update(str(len(canonical_participants)))
    for participant in canonical_participants:
        update(participant)
    update(str(len(ordered_metadata)))
    for key, value in ordered_metadata:
        update(key)
        update(value)
    # Delivery checkpoints are receipts, not meeting content identity. A live
    # connector may redeliver identical content at another cursor/watermark.
    del connector_checkpoint
    update(str(len(ordered_parts)))
    for part in ordered_parts:
        update(part.role)
        update(part.relative_path)
        update(part.title)
        update(part.media_type)
        update(part.sha256)
        update(str(part.size_bytes))
    return digest.hexdigest()


def _part_provenance(
    manifest: PackageManifest,
    part: PackagePart,
    *,
    primary_source_id: str | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "source_system": SOURCE_SYSTEM,
        "import_mode": IMPORT_MODE,
        "external_id": manifest.package_id,
        "package_fingerprint": manifest.fingerprint,
        "fingerprint_profile": FINGERPRINT_PROFILE,
        "part_role": part.role.casefold(),
        "relative_path": part.relative_path,
        "part_title": part.title,
        "media_type": part.media_type,
        "sha256": part.sha256,
        "size_bytes": part.size_bytes,
        "real_integration": False,
    }
    if primary_source_id:
        result["primary_source_id"] = primary_source_id
    if manifest.connector_checkpoint is not None:
        result["connector_checkpoint"] = manifest.connector_checkpoint
    return result


def _relation_metadata(part: PackagePart) -> dict[str, Any]:
    return {
        "part_role": part.role.casefold(),
        "relative_path": part.relative_path,
        "part_title": part.title,
        "media_type": part.media_type,
        "sha256": part.sha256,
        "size_bytes": part.size_bytes,
    }


def _capability_list() -> list[dict[str, str]]:
    return [dict(item) for item in FOLLOW_UP_CAPABILITIES]


def _deduplicate(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        key = value.casefold()
        if key not in seen:
            seen.add(key)
            result.append(value)
    return result


def _canonical_participants(values: Iterable[str]) -> list[str]:
    """Treat participants as an order-independent, case-deduplicated set."""

    by_key: dict[str, str] = {}
    for raw_value in values:
        value = raw_value.strip()
        key = value.lower()
        previous = by_key.get(key)
        if previous is None or value < previous:
            by_key[key] = value
    return [by_key[key] for key in sorted(by_key)]


def _bounded_snippet(value: str, limit: int) -> str:
    normalized = re.sub(r"\s+", " ", value).strip()
    if len(normalized) <= limit:
        return normalized
    boundary = normalized.rfind(" ", 0, max(1, limit - 1))
    if boundary < max(1, limit // 2):
        boundary = max(1, limit - 1)
    return normalized[:boundary].rstrip() + "…"


def _traceable_item(source_id: str, item: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": item["id"],
        "kind": item["kind"],
        "text": item["text"],
        "owner": item.get("owner"),
        "due_at": item.get("due_at"),
        "topic": item.get("topic"),
        "status": item["status"],
        "confidence": item["confidence"],
        "provenance": {
            "source_id": source_id,
            "char_start": item["source_start"],
            "char_end": item["source_end"],
            "quote": item["source_quote"],
        },
    }


def _next_meeting_agenda(
    source_id: str,
    items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    prefixes = {
        "question": "Закрыть вопрос",
        "risk": "Проверить риск",
        "action": "Проверить поручение",
        "commitment": "Проверить обязательство",
        "decision": "Подтвердить решение",
    }
    order = {"question": 0, "risk": 1, "action": 2, "commitment": 3, "decision": 4}
    candidates = [
        item
        for item in items
        if item["kind"] in prefixes
        and (item["kind"] == "decision" or item["status"] == "open")
    ]
    candidates.sort(key=lambda item: (order[item["kind"]], item["source_start"], item["id"]))
    return [
        {
            "title": f"{prefixes[item['kind']]}: {item['text']}",
            "kind": item["kind"],
            "owner": item.get("owner"),
            "due_at": item.get("due_at"),
            "provenance": {
                "source_id": source_id,
                "meeting_item_id": item["id"],
                "char_start": item["source_start"],
                "char_end": item["source_end"],
            },
        }
        for item in candidates[:20]
    ]


def _follow_up_proposals(
    package_id: str,
    source_id: str,
    items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    proposal_kind = {
        "action": "work_item_draft",
        "commitment": "work_item_draft",
        "risk": "risk_mitigation_draft",
        "question": "clarification_draft",
    }
    proposals: list[dict[str, Any]] = []
    for item in items:
        if item["status"] != "open" or item["kind"] not in proposal_kind:
            continue
        stable = hashlib.sha256(
            (
                f"{package_id}|{item['kind']}|{item['source_start']}|"
                f"{item['source_end']}|{item['text']}"
            ).encode("utf-8")
        ).hexdigest()[:20]
        proposals.append(
            {
                "id": f"synapse-proposal-{stable}",
                "kind": proposal_kind[item["kind"]],
                "title": item["text"],
                "owner": item.get("owner"),
                "due_at": item.get("due_at"),
                "execution_mode": "draft_only",
                "external_system": None,
                "provenance": {
                    "source_id": source_id,
                    "meeting_item_id": item["id"],
                    "char_start": item["source_start"],
                    "char_end": item["source_end"],
                    "quote": item["source_quote"],
                },
            }
        )
    return proposals


__all__ = [
    "FINGERPRINT_PROFILE",
    "FOLLOW_UP_CAPABILITIES",
    "IMPORT_MODE",
    "PackageManifest",
    "PackagePart",
    "SCHEMA_VERSION",
    "SOURCE_SYSTEM",
    "SynapseImportInProgressError",
    "SynapseRepairRequiredError",
    "SynapseMeetingPackageImporter",
    "meeting_package_fingerprint",
    "synapse_capability",
]
