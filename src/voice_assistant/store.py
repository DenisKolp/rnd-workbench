from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
from difflib import SequenceMatcher
import hashlib
import json
import math
from pathlib import Path
import re
import shutil
import sqlite3
import threading
from typing import Any, Iterable, Iterator
import unicodedata
from uuid import uuid4

from .automation import next_run


SCHEMA_VERSION = 7

DATA_CLASSIFICATIONS = ("public", "internal", "confidential", "restricted")
CLASSIFICATION_RANK = {
    value: index for index, value in enumerate(DATA_CLASSIFICATIONS)
}

SOURCE_CHUNK_TARGET_CHARS = 1_200
SOURCE_CHUNK_OVERLAP_CHARS = 180
SOURCE_SEARCH_SCAN_LIMIT = 1_200

MEMORY_KINDS = (
    "note",
    "preference",
    "fact",
    "commitment",
    "explicit",
    "task_result",
)
MEMORY_KIND_SETTING = {
    "note": "memory_work_enabled",
    "explicit": "memory_work_enabled",
    "task_result": "memory_work_enabled",
    "preference": "memory_preferences_enabled",
    "fact": "memory_facts_enabled",
    "commitment": "memory_commitments_enabled",
}

_SEARCH_STOPWORDS = {
    "без",
    "бы",
    "в",
    "во",
    "вот",
    "для",
    "до",
    "же",
    "за",
    "и",
    "из",
    "или",
    "как",
    "к",
    "ли",
    "на",
    "не",
    "но",
    "о",
    "об",
    "от",
    "по",
    "про",
    "с",
    "со",
    "то",
    "у",
    "что",
    "это",
    "the",
    "a",
    "an",
    "and",
    "for",
    "in",
    "of",
    "on",
    "or",
    "to",
}


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def new_id() -> str:
    return uuid4().hex


def normalize_classification(
    value: Any,
    *,
    default: str = "internal",
) -> str:
    normalized = str(value or default).strip().casefold()
    if normalized not in CLASSIFICATION_RANK:
        raise ValueError(
            "Классификация должна быть public, internal, confidential или restricted"
        )
    return normalized


def highest_classification(values: Iterable[str]) -> str:
    normalized = [normalize_classification(value) for value in values]
    return max(normalized or ["internal"], key=CLASSIFICATION_RANK.__getitem__)


def normalize_memory_kind(value: Any, *, default: str = "note") -> str:
    normalized = str(value or default).strip().casefold()
    if normalized not in MEMORY_KINDS:
        raise ValueError(
            "Тип памяти должен быть note, preference, fact или commitment"
        )
    return normalized


class AssistantStore:
    """Thread-safe, local-only persistence for the personal assistant workspace."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.artifacts_dir = self.path.parent / "artifacts"
        self.files_dir = self.path.parent / "files"
        self.trash_dir = Path.home() / ".Trash"
        self.artifacts_dir.mkdir(exist_ok=True)
        self.files_dir.mkdir(exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._migrate()
        self._seed()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            try:
                yield self._connection
                self._connection.commit()
            except BaseException:
                self._connection.rollback()
                raise

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def _migrate(self) -> None:
        schema = """
        CREATE TABLE IF NOT EXISTS workspaces (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            classification TEXT NOT NULL DEFAULT 'internal'
                CHECK(classification IN ('public', 'internal', 'confidential', 'restricted'))
        );
        CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY,
            workspace_id TEXT REFERENCES workspaces(id),
            title TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'new',
            plan TEXT NOT NULL DEFAULT '[]',
            result TEXT NOT NULL DEFAULT '',
            skill_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            classification TEXT NOT NULL DEFAULT 'internal'
                CHECK(classification IN ('public', 'internal', 'confidential', 'restricted'))
        );
        CREATE TABLE IF NOT EXISTS messages (
            id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            metadata TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            classification TEXT NOT NULL DEFAULT 'internal'
                CHECK(classification IN ('public', 'internal', 'confidential', 'restricted'))
        );
        CREATE TABLE IF NOT EXISTS task_events (
            id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            kind TEXT NOT NULL,
            title TEXT NOT NULL,
            detail TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sources (
            id TEXT PRIMARY KEY,
            workspace_id TEXT REFERENCES workspaces(id),
            kind TEXT NOT NULL,
            title TEXT NOT NULL,
            path TEXT,
            content TEXT NOT NULL,
            metadata TEXT NOT NULL DEFAULT '{}',
            visibility TEXT NOT NULL DEFAULT 'workspace',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            classification TEXT NOT NULL DEFAULT 'internal'
                CHECK(classification IN ('public', 'internal', 'confidential', 'restricted'))
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS sources_fts USING fts5(
            source_id UNINDEXED,
            title,
            content,
            tokenize='unicode61'
        );
        CREATE TABLE IF NOT EXISTS source_chunks (
            id TEXT PRIMARY KEY,
            source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
            chunk_index INTEGER NOT NULL CHECK(chunk_index >= 0),
            char_start INTEGER NOT NULL CHECK(char_start >= 0),
            char_end INTEGER NOT NULL CHECK(char_end >= char_start),
            content TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(source_id, chunk_index)
        );
        CREATE INDEX IF NOT EXISTS source_chunks_source_offset_idx
            ON source_chunks(source_id, char_start, char_end);
        CREATE VIRTUAL TABLE IF NOT EXISTS source_chunks_fts USING fts5(
            chunk_id UNINDEXED,
            source_id UNINDEXED,
            title,
            content,
            tokenize='unicode61'
        );
        CREATE TABLE IF NOT EXISTS task_sources (
            task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
            created_at TEXT NOT NULL,
            PRIMARY KEY(task_id, source_id)
        );
        CREATE INDEX IF NOT EXISTS task_sources_source_idx ON task_sources(source_id);
        CREATE TABLE IF NOT EXISTS meetings (
            id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
            source_id TEXT NOT NULL UNIQUE REFERENCES sources(id) ON DELETE CASCADE,
            title TEXT NOT NULL,
            occurred_at TEXT,
            participants TEXT NOT NULL DEFAULT '[]',
            summary TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'analyzed'
                CHECK(status IN ('analyzed', 'error')),
            analyzed_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS meetings_workspace_date_idx
            ON meetings(workspace_id, occurred_at DESC, created_at DESC);
        CREATE TABLE IF NOT EXISTS meeting_items (
            id TEXT PRIMARY KEY,
            meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
            kind TEXT NOT NULL
                CHECK(kind IN ('topic', 'decision', 'action', 'commitment', 'risk', 'question')),
            text TEXT NOT NULL,
            owner TEXT,
            due_at TEXT,
            topic TEXT,
            status TEXT NOT NULL DEFAULT 'open'
                CHECK(status IN ('open', 'done', 'superseded')),
            source_quote TEXT NOT NULL,
            source_start INTEGER NOT NULL CHECK(source_start >= 0),
            source_end INTEGER NOT NULL CHECK(source_end >= source_start),
            confidence REAL NOT NULL DEFAULT 1.0 CHECK(confidence >= 0 AND confidence <= 1),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS meeting_items_meeting_kind_idx
            ON meeting_items(meeting_id, kind, status);
        CREATE INDEX IF NOT EXISTS meeting_items_owner_due_idx
            ON meeting_items(owner, due_at, status);
        CREATE INDEX IF NOT EXISTS meeting_items_topic_idx
            ON meeting_items(topic, created_at);
        CREATE TABLE IF NOT EXISTS memory (
            id TEXT PRIMARY KEY,
            workspace_id TEXT REFERENCES workspaces(id),
            kind TEXT NOT NULL DEFAULT 'note',
            title TEXT NOT NULL,
            content TEXT NOT NULL,
            source_id TEXT REFERENCES sources(id),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            classification TEXT NOT NULL DEFAULT 'internal'
                CHECK(classification IN ('public', 'internal', 'confidential', 'restricted'))
        );
        CREATE TABLE IF NOT EXISTS skills (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            command TEXT NOT NULL UNIQUE,
            description TEXT NOT NULL,
            instruction TEXT NOT NULL,
            scope TEXT NOT NULL DEFAULT 'personal',
            workspace_id TEXT REFERENCES workspaces(id),
            version INTEGER NOT NULL DEFAULT 1,
            builtin INTEGER NOT NULL DEFAULT 0,
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            classification TEXT NOT NULL DEFAULT 'internal'
                CHECK(classification IN ('public', 'internal', 'confidential', 'restricted'))
        );
        CREATE TABLE IF NOT EXISTS skill_versions (
            id TEXT PRIMARY KEY,
            skill_id TEXT NOT NULL REFERENCES skills(id) ON DELETE CASCADE,
            version INTEGER NOT NULL,
            instruction TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS capabilities (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            category TEXT NOT NULL,
            description TEXT NOT NULL,
            status TEXT NOT NULL,
            risk TEXT NOT NULL DEFAULT 'safe'
        );
        CREATE TABLE IF NOT EXISTS artifacts (
            id TEXT PRIMARY KEY,
            workspace_id TEXT REFERENCES workspaces(id),
            task_id TEXT REFERENCES tasks(id),
            title TEXT NOT NULL,
            kind TEXT NOT NULL DEFAULT 'markdown',
            path TEXT NOT NULL,
            current_version INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            classification TEXT NOT NULL DEFAULT 'internal'
                CHECK(classification IN ('public', 'internal', 'confidential', 'restricted'))
        );
        CREATE TABLE IF NOT EXISTS artifact_versions (
            id TEXT PRIMARY KEY,
            artifact_id TEXT NOT NULL REFERENCES artifacts(id) ON DELETE CASCADE,
            version INTEGER NOT NULL,
            path TEXT NOT NULL,
            metadata TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            classification TEXT NOT NULL DEFAULT 'internal'
                CHECK(classification IN ('public', 'internal', 'confidential', 'restricted'))
        );
        CREATE UNIQUE INDEX IF NOT EXISTS artifact_versions_number_idx
            ON artifact_versions(artifact_id, version);
        CREATE TABLE IF NOT EXISTS artifact_relations (
            id TEXT PRIMARY KEY,
            artifact_id TEXT NOT NULL REFERENCES artifacts(id) ON DELETE CASCADE,
            artifact_version INTEGER NOT NULL,
            relation_type TEXT NOT NULL,
            task_id TEXT REFERENCES tasks(id) ON DELETE SET NULL,
            source_id TEXT REFERENCES sources(id) ON DELETE SET NULL,
            related_artifact_id TEXT REFERENCES artifacts(id) ON DELETE SET NULL,
            related_artifact_version INTEGER,
            metadata TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS artifact_relations_artifact_idx
            ON artifact_relations(artifact_id, artifact_version, created_at);
        CREATE TABLE IF NOT EXISTS inbox (
            id TEXT PRIMARY KEY,
            workspace_id TEXT REFERENCES workspaces(id),
            kind TEXT NOT NULL,
            title TEXT NOT NULL,
            detail TEXT NOT NULL DEFAULT '',
            priority INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'new',
            source_ref TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS approvals (
            id TEXT PRIMARY KEY,
            task_id TEXT REFERENCES tasks(id),
            action_type TEXT NOT NULL,
            title TEXT NOT NULL,
            payload TEXT NOT NULL DEFAULT '{}',
            risk TEXT NOT NULL DEFAULT 'confirm',
            status TEXT NOT NULL DEFAULT 'pending',
            actor TEXT NOT NULL DEFAULT 'local-user',
            origin TEXT NOT NULL DEFAULT 'assistant',
            workflow_id TEXT NOT NULL DEFAULT '',
            step_index INTEGER NOT NULL DEFAULT 0,
            idempotency_key TEXT NOT NULL DEFAULT '',
            confirmation_policy TEXT NOT NULL DEFAULT 'explicit',
            revision INTEGER NOT NULL DEFAULT 1,
            result TEXT NOT NULL DEFAULT '',
            resolved_by TEXT,
            resolved_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS automations (
            id TEXT PRIMARY KEY,
            workspace_id TEXT REFERENCES workspaces(id),
            name TEXT NOT NULL,
            prompt TEXT NOT NULL,
            schedule TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            last_run_at TEXT,
            next_run_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            classification TEXT NOT NULL DEFAULT 'internal'
                CHECK(classification IN ('public', 'internal', 'confidential', 'restricted'))
        );
        CREATE TABLE IF NOT EXISTS audit_log (
            id TEXT PRIMARY KEY,
            task_id TEXT REFERENCES tasks(id),
            action TEXT NOT NULL,
            target TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL,
            detail TEXT NOT NULL DEFAULT '',
            actor TEXT NOT NULL DEFAULT 'system',
            origin TEXT NOT NULL DEFAULT 'application',
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        """
        with self.transaction() as connection:
            connection.executescript(schema)
            message_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(messages)").fetchall()
            }
            if "metadata" not in message_columns:
                connection.execute(
                    "ALTER TABLE messages ADD COLUMN metadata TEXT NOT NULL DEFAULT '{}'"
                )
            source_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(sources)").fetchall()
            }
            if "visibility" not in source_columns:
                connection.execute(
                    "ALTER TABLE sources ADD COLUMN visibility TEXT NOT NULL DEFAULT 'workspace'"
                )
            for table in (
                "workspaces",
                "tasks",
                "messages",
                "sources",
                "memory",
                "skills",
                "artifacts",
                "artifact_versions",
                "automations",
            ):
                columns = {
                    row[1]
                    for row in connection.execute(
                        f"PRAGMA table_info({table})"
                    ).fetchall()
                }
                if "classification" not in columns:
                    connection.execute(
                        f"ALTER TABLE {table} ADD COLUMN classification TEXT "
                        "NOT NULL DEFAULT 'internal' "
                        "CHECK(classification IN "
                        "('public', 'internal', 'confidential', 'restricted'))"
                    )
            artifact_version_columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(artifact_versions)"
                ).fetchall()
            }
            if "metadata" not in artifact_version_columns:
                connection.execute(
                    "ALTER TABLE artifact_versions "
                    "ADD COLUMN metadata TEXT NOT NULL DEFAULT '{}'"
                )
            approval_columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(approvals)"
                ).fetchall()
            }
            approval_migrations = {
                "actor": "TEXT NOT NULL DEFAULT 'local-user'",
                "origin": "TEXT NOT NULL DEFAULT 'legacy'",
                "workflow_id": "TEXT NOT NULL DEFAULT ''",
                "step_index": "INTEGER NOT NULL DEFAULT 0",
                "idempotency_key": "TEXT NOT NULL DEFAULT ''",
                "confirmation_policy": "TEXT NOT NULL DEFAULT 'explicit'",
                "revision": "INTEGER NOT NULL DEFAULT 1",
                "result": "TEXT NOT NULL DEFAULT ''",
                "resolved_by": "TEXT",
                "resolved_at": "TEXT",
            }
            for column, definition in approval_migrations.items():
                if column not in approval_columns:
                    connection.execute(
                        f"ALTER TABLE approvals ADD COLUMN {column} {definition}"
                    )
            connection.execute(
                "UPDATE approvals SET workflow_id=id WHERE workflow_id=''"
            )
            connection.execute(
                "UPDATE approvals SET idempotency_key='legacy:' || id "
                "WHERE idempotency_key=''"
            )
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS approvals_idempotency_idx "
                "ON approvals(idempotency_key) WHERE idempotency_key != ''"
            )
            audit_columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(audit_log)"
                ).fetchall()
            }
            if "actor" not in audit_columns:
                connection.execute(
                    "ALTER TABLE audit_log ADD COLUMN actor TEXT NOT NULL DEFAULT 'system'"
                )
            if "origin" not in audit_columns:
                connection.execute(
                    "ALTER TABLE audit_log ADD COLUMN origin TEXT NOT NULL DEFAULT 'application'"
                )
            self._backfill_source_chunks(connection)
            # An interrupted process cannot prove that an in-flight external
            # side effect happened.  Recover it as an explicit error and let
            # the user edit/replan; never retry automatically and risk a
            # duplicate corporate action.
            executing_approvals = connection.execute(
                """
                SELECT id, task_id, action_type, workflow_id, step_index, revision
                FROM approvals WHERE status='executing'
                """
            ).fetchall()
            if executing_approvals:
                recovered_at = utc_now()
                connection.execute(
                    """
                    UPDATE approvals
                    SET status='error',
                        result='Выполнение прервано перезапуском; результат не подтверждён',
                        updated_at=?
                    WHERE status='executing'
                    """,
                    (recovered_at,),
                )
                connection.executemany(
                    """
                    INSERT INTO audit_log(
                        id, task_id, action, target, status, detail,
                        actor, origin, created_at
                    ) VALUES (?, ?, 'approval.recover', ?, 'error', ?,
                              'system', 'startup', ?)
                    """,
                    [
                        (
                            new_id(),
                            row["task_id"],
                            row["id"],
                            json.dumps(
                                {
                                    "action_type": row["action_type"],
                                    "result_code": "interrupted_execution",
                                    "revision": row["revision"],
                                    "step_index": row["step_index"],
                                    "workflow_id": row["workflow_id"],
                                },
                                ensure_ascii=False,
                                sort_keys=True,
                            ),
                            recovered_at,
                        )
                        for row in executing_approvals
                    ],
                )
                for task_id in {
                    row["task_id"] for row in executing_approvals if row["task_id"]
                }:
                    connection.execute(
                        "UPDATE tasks SET status='needs_user', updated_at=? WHERE id=?",
                        (recovered_at, task_id),
                    )
                    connection.execute(
                        """
                        INSERT INTO task_events VALUES (
                            ?, ?, 'approval_error',
                            'Внешнее действие прервано при перезапуске',
                            'Результат не подтверждён; измените план перед повтором.', ?
                        )
                        """,
                        (new_id(), task_id, recovered_at),
                    )
            # A fresh process cannot still be executing work left by the old
            # one. Recover persisted ``running`` rows into an explicit
            # user-waiting state so the UI never shows ghost executions after
            # an app restart or an interrupted voice response.
            running_task_ids = [
                row[0]
                for row in connection.execute(
                    "SELECT id FROM tasks WHERE status = 'running'"
                ).fetchall()
            ]
            if running_task_ids:
                recovered_at = utc_now()
                connection.execute(
                    "UPDATE tasks SET status = 'needs_user', updated_at = ? "
                    "WHERE status = 'running'",
                    (recovered_at,),
                )
                connection.executemany(
                    "INSERT INTO task_events VALUES (?, ?, 'recovered', ?, ?, ?)",
                    [
                        (
                            new_id(),
                            task_id,
                            "Выполнение остановлено при перезапуске",
                            "Задача ожидает продолжения пользователя.",
                            recovered_at,
                        )
                        for task_id in running_task_ids
                    ],
                )
                connection.executemany(
                    """
                    INSERT INTO audit_log(
                        id, task_id, action, target, status, detail,
                        actor, origin, created_at
                    ) VALUES (?, ?, 'task.recover', ?, 'success', ?, 'system', 'startup', ?)
                    """,
                    [
                        (
                            new_id(),
                            task_id,
                            task_id,
                            "Состояние running восстановлено как needs_user",
                            recovered_at,
                        )
                        for task_id in running_task_ids
                    ],
                )
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    @staticmethod
    def _chunk_content(content: str) -> list[tuple[int, int, str]]:
        """Split text into overlapping excerpts while preserving exact source offsets."""
        if not content:
            return []
        length = len(content)
        chunks: list[tuple[int, int, str]] = []
        start = 0
        while start < length:
            hard_end = min(length, start + SOURCE_CHUNK_TARGET_CHARS)
            end = hard_end
            if hard_end < length:
                search_start = start + SOURCE_CHUNK_TARGET_CHARS // 2
                candidates: list[int] = []
                for marker in ("\n\n", "\n", ". ", "? ", "! ", "; "):
                    position = content.rfind(marker, search_start, hard_end)
                    if position >= 0:
                        candidates.append(position + len(marker))
                if candidates:
                    end = max(candidates)
            if end <= start:
                end = hard_end
            chunks.append((start, end, content[start:end]))
            if end >= length:
                break
            next_start = max(start + 1, end - SOURCE_CHUNK_OVERLAP_CHARS)
            # Prefer beginning the overlap at a readable boundary, but never
            # change the excerpt itself: offsets always address source.content.
            boundary = content.find("\n", next_start, end)
            if boundary >= 0 and boundary + 1 < end:
                next_start = boundary + 1
            start = next_start
        return chunks

    @classmethod
    def _insert_source_chunks(
        cls,
        connection: sqlite3.Connection,
        source_id: str,
        title: str,
        content: str,
        created_at: str,
    ) -> None:
        for index, (char_start, char_end, chunk_content) in enumerate(
            cls._chunk_content(content)
        ):
            chunk_id = f"{source_id}:{index}"
            connection.execute(
                """
                INSERT OR IGNORE INTO source_chunks(
                    id, source_id, chunk_index, char_start, char_end, content, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    chunk_id,
                    source_id,
                    index,
                    char_start,
                    char_end,
                    chunk_content,
                    created_at,
                ),
            )
            indexed = connection.execute(
                "SELECT 1 FROM source_chunks_fts WHERE chunk_id = ? LIMIT 1",
                (chunk_id,),
            ).fetchone()
            if not indexed:
                connection.execute(
                    """
                    INSERT INTO source_chunks_fts(chunk_id, source_id, title, content)
                    VALUES (?, ?, ?, ?)
                    """,
                    # Index the title once per source so a long document does
                    # not occupy every FTS candidate slot with duplicate title
                    # hits. Content remains indexed for every chunk.
                    (chunk_id, source_id, title if index == 0 else "", chunk_content),
                )

    @classmethod
    def _backfill_source_chunks(cls, connection: sqlite3.Connection) -> None:
        rows = connection.execute(
            """
            SELECT s.id, s.title, s.content, s.created_at
            FROM sources s
            WHERE NOT EXISTS (
                SELECT 1 FROM source_chunks sc WHERE sc.source_id = s.id
            )
            """
        ).fetchall()
        for row in rows:
            cls._insert_source_chunks(
                connection,
                str(row["id"]),
                str(row["title"]),
                str(row["content"]),
                str(row["created_at"]),
            )

    def _seed(self) -> None:
        now = utc_now()
        with self.transaction() as connection:
            if connection.execute("SELECT COUNT(*) FROM workspaces").fetchone()[0] == 0:
                connection.execute(
                    """
                    INSERT INTO workspaces(
                        id, name, description, status, created_at, updated_at,
                        classification
                    ) VALUES (?, ?, ?, 'active', ?, ?, 'internal')
                    """,
                    (
                        "personal",
                        "Личное пространство",
                        "Локальный рабочий контекст",
                        now,
                        now,
                    ),
                )

            builtin_skills = [
                (
                    "research",
                    "Исследование",
                    "/research",
                    "Собирает факты из локальных источников и связывает выводы с ними.",
                    "Составь краткий план исследования, сопоставь доступные источники, отдели факты от выводов и укажи ссылки вида [S1].",
                ),
                (
                    "meeting-analysis",
                    "Анализ встречи",
                    "/meeting",
                    "Выделяет темы, решения, поручения, риски и открытые вопросы.",
                    "Проанализируй транскрипт встречи. Структура: темы, решения, поручения с исполнителями и сроками, обещания, риски, открытые вопросы. Каждый пункт свяжи с источником.",
                ),
                (
                    "briefing",
                    "Подготовка к встрече",
                    "/briefing",
                    "Готовит briefing по истории проекта и прошлым встречам.",
                    "Подготовь briefing: контекст, последние решения, незакрытые поручения, изменения, риски и вопросы для обсуждения.",
                ),
                (
                    "digest",
                    "Дайджест",
                    "/digest",
                    "Собирает рабочую сводку по задачам и контексту.",
                    "Сформируй компактный дайджест: главное, изменения, решения, сроки, риски и следующие действия.",
                ),
                (
                    "document",
                    "Рабочий документ",
                    "/document",
                    "Создаёт структурированный Markdown-артефакт.",
                    "Подготовь самостоятельный рабочий документ в Markdown с понятным заголовком, кратким резюме и логичной структурой.",
                ),
            ]
            for skill_id, name, command, description, instruction in builtin_skills:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO skills
                    (id, name, command, description, instruction, scope, workspace_id,
                     version, builtin, enabled, created_at, updated_at, classification)
                    VALUES (?, ?, ?, ?, ?, 'corporate', NULL, 1, 1, 1, ?, ?, 'public')
                    """,
                    (skill_id, name, command, description, instruction, now, now),
                )
            # Built-in instructions are application code, not corporate data.
            # Correct legacy rows as well as fresh inserts so they never raise
            # the classification of an otherwise public remote turn.
            connection.execute(
                "UPDATE skills SET classification='public' WHERE builtin=1"
            )

            capabilities = [
                ("dictation", "Диктовка", "Голос", "Whisper STT и нормализация голосового запроса", "connected", "safe"),
                ("local-search", "Локальный поиск", "Знания", "Поиск по импортированным документам и встречам", "connected", "safe"),
                ("documents", "Документы", "Материалы", "Создание и версионирование локальных Markdown-артефактов", "connected", "safe"),
                ("voice", "Озвучивание", "Голос", "Локальный OmniVoice Fast/Metal с перебиванием", "connected", "safe"),
                ("email", "Почта", "Внешние системы", "Корпоративная почта: требуется подключение", "not_connected", "confirm"),
                ("synapse", "Синапс", "Внешние системы", "Корпоративный мессенджер: требуется API", "not_connected", "confirm"),
                ("calendar", "Календарь", "Внешние системы", "Корпоративный календарь: требуется подключение", "not_connected", "confirm"),
                ("corporate", "Корпоративные системы", "Внешние системы", "Project 360, Service Desk и другие API", "not_connected", "confirm"),
            ]
            connection.executemany(
                "INSERT OR REPLACE INTO capabilities VALUES (?, ?, ?, ?, ?, ?)",
                capabilities,
            )
            defaults = {
                "response_style": "brief",
                "proactivity": "balanced",
                "memory_enabled": "true",
                "memory_preferences_enabled": "true",
                "memory_facts_enabled": "true",
                "memory_commitments_enabled": "true",
                "memory_work_enabled": "true",
                "voice_review_before_send": "false",
                "internal_only": "true",
                "model_mode": "local",
                "auto_remote_policy": "local_only",
                "llm_base_url": "",
                "llm_model": "",
                "external_context_scope": "task",
                "external_context_scope_endpoint": "",
                "external_context_scope_workspace": "",
                "external_provider_type": "external",
                "default_classification": "internal",
            }
            connection.executemany(
                "INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)",
                defaults.items(),
            )

    def _rows(self, query: str, parameters: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(query, parameters).fetchall()
        return [dict(row) for row in rows]

    def default_workspace_id(self) -> str:
        row = self._rows(
            "SELECT id FROM workspaces WHERE status = 'active' ORDER BY created_at LIMIT 1"
        )
        return row[0]["id"]

    def create_workspace(
        self,
        name: str,
        description: str = "",
        *,
        classification: str = "internal",
    ) -> dict[str, Any]:
        workspace_id = new_id()
        now = utc_now()
        classification = normalize_classification(classification)
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO workspaces(
                    id, name, description, status, created_at, updated_at,
                    classification
                ) VALUES (?, ?, ?, 'active', ?, ?, ?)
                """,
                (
                    workspace_id,
                    name.strip(),
                    description.strip(),
                    now,
                    now,
                    classification,
                ),
            )
        self.audit(None, "workspace.create", workspace_id, "success", name)
        return self.get_workspace(workspace_id)

    def get_workspace(self, workspace_id: str) -> dict[str, Any]:
        rows = self._rows("SELECT * FROM workspaces WHERE id = ?", (workspace_id,))
        if not rows:
            raise KeyError(f"Рабочее пространство не найдено: {workspace_id}")
        return rows[0]

    def update_workspace(
        self,
        workspace_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
        status: str | None = None,
    ) -> None:
        current = self.get_workspace(workspace_id)
        with self.transaction() as connection:
            connection.execute(
                "UPDATE workspaces SET name = ?, description = ?, status = ?, updated_at = ? WHERE id = ?",
                (
                    name.strip() if name is not None else current["name"],
                    description.strip() if description is not None else current["description"],
                    status or current["status"],
                    utc_now(),
                    workspace_id,
                ),
            )
        self.audit(None, "workspace.update", workspace_id, "success")

    def create_task(
        self,
        workspace_id: str,
        title: str,
        plan: list[str] | None = None,
        *,
        classification: str | None = None,
    ) -> dict[str, Any]:
        task_id = new_id()
        now = utc_now()
        plan = plan or ["Подобрать контекст", "Выполнить задачу", "Сохранить результат"]
        workspace = self.get_workspace(workspace_id)
        classification = normalize_classification(
            classification,
            default=str(workspace["classification"]),
        )
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO tasks(
                    id, workspace_id, title, status, plan, result, skill_id,
                    created_at, updated_at, classification
                ) VALUES (?, ?, ?, 'new', ?, '', NULL, ?, ?, ?)
                """,
                (
                    task_id,
                    workspace_id,
                    title.strip()[:160],
                    json.dumps(plan, ensure_ascii=False),
                    now,
                    now,
                    classification,
                ),
            )
        self.add_task_event(task_id, "created", "Задача создана")
        return self.get_task(task_id)

    def get_task(self, task_id: str) -> dict[str, Any]:
        rows = self._rows("SELECT * FROM tasks WHERE id = ?", (task_id,))
        if not rows:
            raise KeyError(f"Задача не найдена: {task_id}")
        task = rows[0]
        task["plan"] = json.loads(task["plan"])
        return task

    def update_task(
        self,
        task_id: str,
        *,
        status: str | None = None,
        title: str | None = None,
        result: str | None = None,
        skill_id: str | None = None,
        plan: list[str] | None = None,
    ) -> None:
        current = self.get_task(task_id)
        with self.transaction() as connection:
            connection.execute(
                """
                UPDATE tasks SET title = ?, status = ?, plan = ?, result = ?, skill_id = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    title or current["title"],
                    status or current["status"],
                    json.dumps(plan if plan is not None else current["plan"], ensure_ascii=False),
                    result if result is not None else current["result"],
                    skill_id if skill_id is not None else current["skill_id"],
                    utc_now(),
                    task_id,
                ),
            )

    def delete_task(self, task_id: str) -> dict[str, Any]:
        """Delete a task while preserving independent artifacts and audit history."""

        task = self.get_task(task_id)
        owned_sources = self._rows(
            """
            SELECT s.id FROM sources s
            JOIN task_sources own ON own.source_id=s.id AND own.task_id=?
            WHERE s.visibility='task'
              AND NOT EXISTS (
                  SELECT 1 FROM task_sources other
                  WHERE other.source_id=s.id AND other.task_id<>?
              )
            """,
            (task_id, task_id),
        )
        source_records = [self.get_source(row["id"]) for row in owned_sources]
        moved = self._trash_managed_paths(
            path
            for source in source_records
            for path in self._source_managed_paths(source)
        )
        try:
            with self.transaction() as connection:
                for source in source_records:
                    self._delete_source_rows(connection, source["id"])
                connection.execute("DELETE FROM approvals WHERE task_id=?", (task_id,))
                connection.execute("UPDATE artifacts SET task_id=NULL WHERE task_id=?", (task_id,))
                connection.execute("UPDATE audit_log SET task_id=NULL WHERE task_id=?", (task_id,))
                connection.execute("DELETE FROM inbox WHERE source_ref=?", (task_id,))
                cursor = connection.execute("DELETE FROM tasks WHERE id=?", (task_id,))
                if cursor.rowcount == 0:
                    raise KeyError(task_id)
        except BaseException:
            self._restore_trashed_paths(moved)
            raise
        self.audit(None, "task.delete", task_id, "success", task["title"])
        return {
            "id": task_id,
            "title": task["title"],
            "trashed_files": len(moved),
        }

    def add_message(
        self,
        task_id: str,
        role: str,
        content: str,
        metadata: dict[str, Any] | None = None,
        *,
        classification: str | None = None,
    ) -> dict[str, Any]:
        task = self.get_task(task_id)
        classification = normalize_classification(
            classification,
            default=str(task["classification"]),
        )
        message = {
            "id": new_id(),
            "task_id": task_id,
            "role": role,
            "content": content,
            "metadata": json.dumps(metadata or {}, ensure_ascii=False),
            "created_at": utc_now(),
            "classification": classification,
        }
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO messages(
                    id, task_id, role, content, metadata, created_at,
                    classification
                ) VALUES (
                    :id, :task_id, :role, :content, :metadata, :created_at,
                    :classification
                )
                """,
                message,
            )
        return message

    def messages(self, task_id: str, limit: int = 40) -> list[dict[str, Any]]:
        return self._rows(
            """
            SELECT id, task_id, role, content, metadata, created_at,
                   classification FROM (
                SELECT * FROM messages WHERE task_id = ? ORDER BY created_at DESC LIMIT ?
            ) ORDER BY created_at
            """,
            (task_id, limit),
        )

    def add_task_event(self, task_id: str, kind: str, title: str, detail: str = "") -> None:
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO task_events VALUES (?, ?, ?, ?, ?, ?)",
                (new_id(), task_id, kind, title, detail, utc_now()),
            )

    def add_source(
        self,
        workspace_id: str,
        kind: str,
        title: str,
        content: str,
        *,
        path: str | None = None,
        metadata: dict[str, Any] | None = None,
        visibility: str = "workspace",
        task_id: str | None = None,
        classification: str | None = None,
    ) -> dict[str, Any]:
        if visibility not in {"workspace", "task"}:
            raise ValueError(f"Неизвестная область видимости источника: {visibility}")
        if visibility == "task" and not task_id:
            raise ValueError("Для источника задачи нужен task_id")
        if task_id:
            task = self.get_task(task_id)
            if task["workspace_id"] != workspace_id:
                raise ValueError("Задача и источник должны принадлежать одному Workspace")
            inherited_classification = str(task["classification"])
        else:
            inherited_classification = str(
                self.get_workspace(workspace_id)["classification"]
            )
        classification = normalize_classification(
            classification,
            default=inherited_classification,
        )
        source_id = new_id()
        now = utc_now()
        payload = json.dumps(metadata or {}, ensure_ascii=False)
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO sources(
                    id, workspace_id, kind, title, path, content, metadata,
                    visibility, created_at, updated_at, classification
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source_id,
                    workspace_id,
                    kind,
                    title,
                    path,
                    content,
                    payload,
                    visibility,
                    now,
                    now,
                    classification,
                ),
            )
            connection.execute(
                "INSERT INTO sources_fts(source_id, title, content) VALUES (?, ?, ?)",
                (source_id, title, content),
            )
            self._insert_source_chunks(
                connection,
                source_id,
                title,
                content,
                now,
            )
            if task_id:
                connection.execute(
                    "INSERT OR IGNORE INTO task_sources VALUES (?, ?, ?)",
                    (task_id, source_id, now),
                )
        self.audit(task_id, "source.import", source_id, "success", title)
        if visibility == "workspace":
            self.add_inbox(
                workspace_id,
                "source",
                f"Добавлен источник: {title}",
                f"Тип: {kind}",
                priority=1,
                source_ref=source_id,
            )
        elif task_id:
            self.add_task_event(task_id, "source", f"Добавлен файл: {title}")
        return self.get_source(source_id)

    def get_source(self, source_id: str) -> dict[str, Any]:
        rows = self._rows("SELECT * FROM sources WHERE id = ?", (source_id,))
        if not rows:
            raise KeyError(f"Источник не найден: {source_id}")
        row = rows[0]
        row["metadata"] = json.loads(row["metadata"])
        return row

    def delete_source(self, source_id: str) -> dict[str, Any]:
        source = self.get_source(source_id)
        moved = self._trash_managed_paths(self._source_managed_paths(source))
        try:
            with self.transaction() as connection:
                self._delete_source_rows(connection, source_id)
        except BaseException:
            self._restore_trashed_paths(moved)
            raise
        self.audit(None, "source.delete", source_id, "success", source["title"])
        return {
            "id": source_id,
            "title": source["title"],
            "kind": source["kind"],
            "trashed_files": len(moved),
        }

    def rollback_source_import(self, source_id: str) -> None:
        """Discard an uncommitted managed copy after a batched import fails.

        This is intentionally separate from the user-facing delete operation:
        the original user file is never touched, while the application's new
        private copy is removed instead of polluting the user's Trash.
        """

        source = self.get_source(source_id)
        managed_paths = self._source_managed_paths(source)
        with self.transaction() as connection:
            self._delete_source_rows(connection, source_id)
        roots = (self.files_dir.resolve(), self.artifacts_dir.resolve())
        for raw_path in managed_paths:
            path = raw_path.expanduser().resolve()
            if not any(path.is_relative_to(root) for root in roots):
                continue
            try:
                path.unlink(missing_ok=True)
            except OSError:
                # The database rollback is authoritative. A failed cleanup can
                # leave only an unreferenced app-managed copy, never the user's
                # original file.
                pass
        self.audit(None, "source.import", source_id, "rolled_back", source["title"])

    def _delete_source_rows(
        self,
        connection: sqlite3.Connection,
        source_id: str,
    ) -> None:
        related_ids = [source_id]
        for row in connection.execute(
            "SELECT id FROM meetings WHERE source_id=?", (source_id,)
        ).fetchall():
            related_ids.append(str(row["id"]))
            related_ids.extend(
                str(item["id"])
                for item in connection.execute(
                    "SELECT id FROM meeting_items WHERE meeting_id=?", (row["id"],)
                ).fetchall()
            )
        placeholders = ",".join("?" for _ in related_ids)
        connection.execute(
            f"DELETE FROM inbox WHERE source_ref IN ({placeholders})",
            tuple(related_ids),
        )
        connection.execute("UPDATE memory SET source_id=NULL WHERE source_id=?", (source_id,))
        connection.execute("DELETE FROM source_chunks_fts WHERE source_id=?", (source_id,))
        connection.execute("DELETE FROM sources_fts WHERE source_id=?", (source_id,))
        cursor = connection.execute("DELETE FROM sources WHERE id=?", (source_id,))
        if cursor.rowcount == 0:
            raise KeyError(source_id)

    def link_task_source(self, task_id: str, source_id: str) -> None:
        task = self.get_task(task_id)
        source = self.get_source(source_id)
        if task["workspace_id"] != source["workspace_id"]:
            raise ValueError("Задача и источник должны принадлежать одному Workspace")
        if source["visibility"] == "task":
            owners = self._rows(
                "SELECT task_id FROM task_sources WHERE source_id = ?",
                (source_id,),
            )
            if owners and any(owner["task_id"] != task_id for owner in owners):
                raise PermissionError("Источник доступен только исходной задаче")
        with self.transaction() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO task_sources VALUES (?, ?, ?)",
                (task_id, source_id, utc_now()),
            )

    def task_sources(self, task_id: str) -> list[dict[str, Any]]:
        return self._rows(
            """
            SELECT s.id, s.title, s.kind, s.path
            FROM task_sources ts JOIN sources s ON s.id = ts.source_id
            WHERE ts.task_id = ?
            ORDER BY ts.created_at, s.created_at
            """,
            (task_id,),
        )

    @staticmethod
    def _search_tokens(value: str) -> list[str]:
        tokens = re.findall(r"[\w-]{2,}", value.casefold(), flags=re.UNICODE)
        meaningful = [token for token in tokens if token not in _SEARCH_STOPWORDS]
        return list(dict.fromkeys(meaningful or tokens))[:16]

    @staticmethod
    def _search_stem(token: str) -> str:
        """Small deterministic normalizer for Russian/English inflections.

        This deliberately is not presented as an embedding model.  It only
        improves local recall for common word endings without any network or
        additional model dependency.
        """
        normalized = token.casefold().replace("ё", "е").strip("-_")
        if len(normalized) <= 4:
            return normalized
        endings = (
            "иями",
            "ями",
            "ами",
            "ого",
            "ему",
            "ому",
            "ыми",
            "ими",
            "ий",
            "ый",
            "ой",
            "ая",
            "яя",
            "ое",
            "ее",
            "ые",
            "ие",
            "ам",
            "ям",
            "ах",
            "ях",
            "ов",
            "ев",
            "ом",
            "ем",
            "ы",
            "и",
            "а",
            "я",
            "у",
            "ю",
            "е",
            "о",
            "s",
            "es",
            "ed",
            "ing",
        )
        for ending in endings:
            if normalized.endswith(ending) and len(normalized) - len(ending) >= 4:
                return normalized[: -len(ending)]
        return normalized

    @classmethod
    def _hybrid_source_score(
        cls,
        query_tokens: list[str],
        row: dict[str, Any],
        fts_order: dict[str, int],
    ) -> float:
        content = str(row["chunk_content"])
        title = str(row["title"])
        # _search_tokens caps query-size inputs. Search scoring needs the full
        # chunk vocabulary, still bounded by the fixed chunk length.
        content_tokens = list(
            dict.fromkeys(
                re.findall(r"[\w-]{2,}", content.casefold(), flags=re.UNICODE)
            )
        )
        title_tokens = cls._search_tokens(title)
        query_stems = {cls._search_stem(token) for token in query_tokens}
        content_stems = {cls._search_stem(token) for token in content_tokens}
        title_stems = {cls._search_stem(token) for token in title_tokens}
        coverage = len(query_stems & content_stems) / max(1, len(query_stems))
        title_coverage = len(query_stems & title_stems) / max(1, len(query_stems))

        vocabulary = set(content_tokens) | set(title_tokens)
        fuzzy_sum = 0.0
        for query_token in query_tokens:
            if cls._search_stem(query_token) in content_stems | title_stems:
                fuzzy_sum += 1.0
                continue
            if len(query_token) < 4:
                continue
            likely = (
                token
                for token in vocabulary
                if token[:1] == query_token[:1] and abs(len(token) - len(query_token)) <= 3
            )
            best = max(
                (SequenceMatcher(None, query_token, token).ratio() for token in likely),
                default=0.0,
            )
            if best >= 0.78:
                fuzzy_sum += best
        fuzzy_coverage = fuzzy_sum / max(1, len(query_tokens))

        normalized_query = " ".join(query_tokens)
        normalized_haystack = " ".join(title_tokens + content_tokens)
        phrase = 1.0 if normalized_query and normalized_query in normalized_haystack else 0.0
        fts_position = fts_order.get(str(row["chunk_id"]))
        fts_signal = (
            max(0.25, 1.0 - fts_position / max(1, len(fts_order)))
            if fts_position is not None
            else 0.0
        )
        return (
            0.38 * fts_signal
            + 0.27 * coverage
            + 0.20 * fuzzy_coverage
            + 0.10 * title_coverage
            + 0.05 * phrase
        )

    @staticmethod
    def _source_snippet(content: str, tokens: list[str], max_chars: int = 360) -> str:
        lowered = content.casefold()
        positions = [lowered.find(token) for token in tokens]
        positions = [position for position in positions if position >= 0]
        anchor = min(positions) if positions else 0
        start = max(0, anchor - max_chars // 3)
        end = min(len(content), start + max_chars)
        if end - start < max_chars:
            start = max(0, end - max_chars)
        snippet = content[start:end]
        for token in sorted(tokens, key=len, reverse=True):
            snippet = re.sub(
                rf"(?i)(?<!\w)({re.escape(token)})(?!\w)",
                r"‹\1›",
                snippet,
            )
        if start:
            snippet = "… " + snippet
        if end < len(content):
            snippet += " …"
        return snippet

    def search_sources(
        self,
        query: str,
        *,
        workspace_id: str | None = None,
        task_id: str | None = None,
        limit: int = 8,
    ) -> list[dict[str, Any]]:
        tokens = self._search_tokens(query)
        if not tokens:
            return []
        fts_query = " OR ".join(f'"{token}"' for token in tokens[:12])
        scope, scope_parameters = self._source_scope("s", workspace_id, task_id)
        fts_limit = max(128, limit * 24)
        fts_rows: list[dict[str, Any]] = []
        try:
            fts_rows = self._rows(
                f"""
                SELECT sc.id AS chunk_id, sc.source_id, sc.chunk_index,
                       sc.char_start, sc.char_end, sc.content AS chunk_content,
                       s.id, s.workspace_id, s.kind, s.title, s.path,
                       s.classification,
                       bm25(source_chunks_fts) AS fts_rank, s.created_at
                FROM source_chunks_fts
                JOIN source_chunks sc ON sc.id = source_chunks_fts.chunk_id
                JOIN sources s ON s.id = sc.source_id
                WHERE source_chunks_fts MATCH ? AND {scope}
                ORDER BY fts_rank LIMIT ?
                """,
                (fts_query, *scope_parameters, fts_limit),
            )
        except sqlite3.OperationalError:
            # The deterministic scan below is also the compatibility fallback
            # for SQLite builds where the FTS query parser rejects a token.
            fts_rows = []

        scan_rows = self._rows(
            f"""
            SELECT sc.id AS chunk_id, sc.source_id, sc.chunk_index,
                   sc.char_start, sc.char_end, sc.content AS chunk_content,
                   s.id, s.workspace_id, s.kind, s.title, s.path,
                   s.classification,
                   NULL AS fts_rank, s.created_at
            FROM source_chunks sc JOIN sources s ON s.id = sc.source_id
            WHERE {scope}
            ORDER BY sc.chunk_index, s.updated_at DESC
            LIMIT ?
            """,
            (*scope_parameters, SOURCE_SEARCH_SCAN_LIMIT),
        )
        candidates: dict[str, dict[str, Any]] = {
            str(row["chunk_id"]): row for row in scan_rows
        }
        for row in fts_rows:
            candidates[str(row["chunk_id"])] = row
        fts_order = {
            str(row["chunk_id"]): index for index, row in enumerate(fts_rows)
        }

        scored: list[dict[str, Any]] = []
        for row in candidates.values():
            score = self._hybrid_source_score(tokens, row, fts_order)
            is_fts_match = str(row["chunk_id"]) in fts_order
            if not is_fts_match and score < 0.17:
                continue
            scored.append({**row, "score": score})
        scored.sort(
            key=lambda row: (
                -float(row["score"]),
                str(row["title"]).casefold(),
                int(row["chunk_index"]),
            )
        )

        results: list[dict[str, Any]] = []
        seen_sources: set[str] = set()
        for row in scored:
            source_id = str(row["source_id"])
            if source_id in seen_sources:
                continue
            seen_sources.add(source_id)
            chunk_content = str(row["chunk_content"])
            results.append(
                {
                    "id": source_id,
                    "source_id": source_id,
                    "workspace_id": row["workspace_id"],
                    "kind": row["kind"],
                    "title": row["title"],
                    "path": row["path"],
                    "classification": row["classification"],
                    "chunk_id": row["chunk_id"],
                    "chunk_index": row["chunk_index"],
                    "char_start": row["char_start"],
                    "char_end": row["char_end"],
                    "excerpt": chunk_content,
                    "snippet": self._source_snippet(chunk_content, tokens),
                    "score": round(float(row["score"]), 6),
                    "rank": -round(float(row["score"]), 6),
                    "created_at": row["created_at"],
                }
            )
            if len(results) >= limit:
                break
        return results

    @staticmethod
    def _source_scope(
        alias: str,
        workspace_id: str | None,
        task_id: str | None,
    ) -> tuple[str, tuple[Any, ...]]:
        clauses: list[str] = []
        parameters: list[Any] = []
        if workspace_id:
            clauses.append(f"{alias}.workspace_id = ?")
            parameters.append(workspace_id)
        if task_id:
            clauses.append(
                f"({alias}.visibility = 'workspace' OR EXISTS ("
                f"SELECT 1 FROM task_sources visible_ts "
                f"WHERE visible_ts.source_id = {alias}.id AND visible_ts.task_id = ?))"
            )
            parameters.append(task_id)
        else:
            clauses.append(f"{alias}.visibility = 'workspace'")
        return " AND ".join(clauses), tuple(parameters)

    def source_context(
        self,
        source_refs: list[str | dict[str, Any]],
        max_chars: int = 12_000,
    ) -> list[dict[str, Any]]:
        if not source_refs:
            return []
        context: list[dict[str, Any]] = []
        excerpt_chars = max(1, max_chars // len(source_refs))
        seen_sources: set[str] = set()
        for reference in source_refs:
            source_id = str(reference["id"] if isinstance(reference, dict) else reference)
            if source_id in seen_sources:
                continue
            seen_sources.add(source_id)
            source = self.get_source(source_id)
            start = 0
            end = min(len(source["content"]), excerpt_chars)
            chunk_id = None
            selection = "linked"
            if isinstance(reference, dict):
                requested_start = reference.get("char_start")
                requested_end = reference.get("char_end")
                if isinstance(requested_start, int) and isinstance(requested_end, int):
                    if 0 <= requested_start < requested_end <= len(source["content"]):
                        start = requested_start
                        end = min(requested_end, start + excerpt_chars)
                        chunk_id = reference.get("chunk_id")
                selection = str(reference.get("selection") or "retrieved")
            excerpt = source["content"][start:end]
            context.append(
                {
                    **source,
                    "excerpt": excerpt,
                    "char_start": start,
                    "char_end": end,
                    "chunk_id": chunk_id,
                    "selection": selection,
                }
            )
        return context

    @staticmethod
    def _decode_meeting(row: dict[str, Any]) -> dict[str, Any]:
        decoded = dict(row)
        raw_participants = decoded.get("participants", "[]")
        if isinstance(raw_participants, str):
            try:
                value = json.loads(raw_participants)
                decoded["participants"] = value if isinstance(value, list) else []
            except (TypeError, json.JSONDecodeError):
                decoded["participants"] = []
        return decoded

    @staticmethod
    def _validate_iso(value: str | None, label: str) -> None:
        if value is None:
            return
        try:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError(f"{label} должен быть датой или временем ISO 8601") from error

    @staticmethod
    def _reanalysis_item_similarity(
        previous: dict[str, Any], current: dict[str, Any]
    ) -> float:
        if previous.get("kind") != current.get("kind"):
            return 0.0

        def normalized(value: Any) -> str:
            return re.sub(
                r"[^\w]+",
                " ",
                str(value or "").casefold().replace("ё", "е"),
                flags=re.UNICODE,
            ).strip()

        previous_text = normalized(previous.get("text"))
        current_text = normalized(current.get("text"))
        if previous_text and previous_text == current_text:
            return 1.0
        text_score = SequenceMatcher(None, previous_text, current_text).ratio()
        quote_score = SequenceMatcher(
            None,
            normalized(previous.get("source_quote")),
            normalized(current.get("source_quote")),
        ).ratio()
        owner_score = (
            1.0
            if normalized(previous.get("owner")) == normalized(current.get("owner"))
            else 0.0
        )
        due_score = 1.0 if previous.get("due_at") == current.get("due_at") else 0.0
        return 0.68 * text_score + 0.20 * quote_score + 0.07 * owner_score + 0.05 * due_score

    def upsert_meeting(
        self,
        source_id: str,
        *,
        title: str,
        occurred_at: str | None,
        participants: list[str],
        summary: str,
        items: list[dict[str, Any]],
        status: str = "analyzed",
    ) -> dict[str, Any]:
        """Atomically create/re-analyze a meeting and replace its extracted items."""
        if status not in {"analyzed", "error"}:
            raise ValueError(f"Неизвестный статус встречи: {status}")
        self._validate_iso(occurred_at, "occurred_at")
        source = self.get_source(source_id)
        transcript = source["content"]
        allowed_kinds = {"topic", "decision", "action", "commitment", "risk", "question"}
        allowed_statuses = {"open", "done", "superseded"}
        normalized_items: list[dict[str, Any]] = []
        for item in items:
            kind = str(item.get("kind", ""))
            item_status = str(item.get("status", "open"))
            if kind not in allowed_kinds:
                raise ValueError(f"Неизвестный тип пункта встречи: {kind}")
            if item_status not in allowed_statuses:
                raise ValueError(f"Неизвестный статус пункта встречи: {item_status}")
            start = int(item.get("source_start", -1))
            end = int(item.get("source_end", -1))
            quote = str(item.get("source_quote", ""))
            due_at = str(item["due_at"]) if item.get("due_at") else None
            self._validate_iso(due_at, "due_at")
            confidence = float(item.get("confidence", 1.0))
            if not math.isfinite(confidence):
                raise ValueError("confidence должен быть конечным числом")
            if (
                start < 0
                or end <= start
                or end > len(transcript)
                or not quote
                or transcript[start:end] != quote
            ):
                raise ValueError("Цитата пункта встречи не соответствует исходному транскрипту")
            normalized_items.append(
                {
                    "id": str(item.get("id") or new_id()),
                    "kind": kind,
                    "text": str(item.get("text", "")).strip(),
                    "owner": str(item["owner"]).strip() if item.get("owner") else None,
                    "due_at": due_at,
                    "topic": str(item["topic"]).strip() if item.get("topic") else None,
                    "status": item_status,
                    "source_quote": quote,
                    "source_start": start,
                    "source_end": end,
                    "confidence": max(0.0, min(1.0, confidence)),
                }
            )

        incoming_ids = [item["id"] for item in normalized_items]
        if len(incoming_ids) != len(set(incoming_ids)):
            # Preserve the former atomic UNIQUE failure contract before any
            # stable-ID reconciliation happens during re-analysis.
            raise sqlite3.IntegrityError("UNIQUE constraint failed: meeting_items.id")

        now = utc_now()
        with self.transaction() as connection:
            current = connection.execute(
                "SELECT id, created_at FROM meetings WHERE source_id = ?", (source_id,)
            ).fetchone()
            meeting_id = current["id"] if current else new_id()
            created_at = current["created_at"] if current else now
            previous_items = (
                [
                    dict(row)
                    for row in connection.execute(
                        "SELECT * FROM meeting_items WHERE meeting_id = ?",
                        (meeting_id,),
                    ).fetchall()
                ]
                if current
                else []
            )
            unmatched_previous = set(range(len(previous_items)))
            for item in normalized_items:
                candidates = [
                    (
                        self._reanalysis_item_similarity(previous_items[index], item),
                        index,
                    )
                    for index in unmatched_previous
                ]
                similarity, matched_index = max(candidates, default=(0.0, -1))
                if similarity < 0.84:
                    item["created_at"] = now
                    continue
                previous = previous_items[matched_index]
                unmatched_previous.remove(matched_index)
                item["id"] = previous["id"]
                item["created_at"] = previous["created_at"]
                if item["status"] == "open" and previous["status"] in {
                    "done",
                    "superseded",
                }:
                    item["status"] = previous["status"]
            connection.execute(
                """
                INSERT INTO meetings(
                    id, workspace_id, source_id, title, occurred_at, participants,
                    summary, status, analyzed_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_id) DO UPDATE SET
                    workspace_id=excluded.workspace_id,
                    title=excluded.title,
                    occurred_at=excluded.occurred_at,
                    participants=excluded.participants,
                    summary=excluded.summary,
                    status=excluded.status,
                    analyzed_at=excluded.analyzed_at,
                    updated_at=excluded.updated_at
                """,
                (
                    meeting_id,
                    source["workspace_id"],
                    source_id,
                    title.strip() or source["title"],
                    occurred_at,
                    json.dumps(list(dict.fromkeys(participants)), ensure_ascii=False),
                    summary.strip(),
                    status,
                    now,
                    created_at,
                    now,
                ),
            )
            connection.execute("DELETE FROM meeting_items WHERE meeting_id = ?", (meeting_id,))
            connection.executemany(
                """
                INSERT INTO meeting_items(
                    id, meeting_id, kind, text, owner, due_at, topic, status,
                    source_quote, source_start, source_end, confidence, created_at, updated_at
                ) VALUES (
                    :id, :meeting_id, :kind, :text, :owner, :due_at, :topic, :status,
                    :source_quote, :source_start, :source_end, :confidence, :created_at, :updated_at
                )
                """,
                [
                    {**item, "meeting_id": meeting_id, "updated_at": now}
                    for item in normalized_items
                ],
            )
        return self.get_meeting(meeting_id, include_items=True)

    def upsert_meeting_analysis(
        self, source_id: str, analysis: dict[str, Any]
    ) -> dict[str, Any]:
        """Store the public mapping returned by ``meetings.analyze_transcript``."""
        return self.upsert_meeting(
            source_id,
            title=str(analysis.get("title") or self.get_source(source_id)["title"]),
            occurred_at=analysis.get("occurred_at"),
            participants=list(analysis.get("participants") or []),
            summary=str(analysis.get("summary") or ""),
            items=list(analysis.get("items") or []),
            status=str(analysis.get("status") or "analyzed"),
        )

    def analyze_meeting(
        self,
        source_id: str,
        *,
        title: str | None = None,
        occurred_at: str | None = None,
    ) -> dict[str, Any]:
        """Analyze a local source deterministically and persist the result atomically."""
        from .meetings import analyze_transcript

        source = self.get_source(source_id)
        analysis = analyze_transcript(
            source["content"],
            title=title or source["title"],
            occurred_at=occurred_at,
        )
        return self.upsert_meeting_analysis(source_id, analysis)

    def get_meeting(
        self, meeting_id: str, *, include_items: bool = False
    ) -> dict[str, Any]:
        rows = self._rows(
            """
            SELECT m.*, s.path AS source_path
            FROM meetings m JOIN sources s ON s.id=m.source_id
            WHERE m.id = ?
            """,
            (meeting_id,),
        )
        if not rows:
            raise KeyError(f"Встреча не найдена: {meeting_id}")
        meeting = self._decode_meeting(rows[0])
        if include_items:
            meeting["items"] = self.meeting_items(meeting_id)
        return meeting

    def get_meeting_by_source(
        self, source_id: str, *, include_items: bool = False
    ) -> dict[str, Any]:
        rows = self._rows("SELECT id FROM meetings WHERE source_id = ?", (source_id,))
        if not rows:
            raise KeyError(f"Для источника нет встречи: {source_id}")
        return self.get_meeting(rows[0]["id"], include_items=include_items)

    def list_meetings(
        self,
        workspace_id: str,
        *,
        person: str | None = None,
        period_start: str | None = None,
        period_end: str | None = None,
        kind: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        clauses = ["m.workspace_id = ?"]
        parameters: list[Any] = [workspace_id]
        if period_start:
            clauses.append("COALESCE(m.occurred_at, m.created_at) >= ?")
            parameters.append(period_start)
        if period_end:
            clauses.append("COALESCE(m.occurred_at, m.created_at) <= ?")
            parameters.append(period_end)
        if kind:
            clauses.append("EXISTS (SELECT 1 FROM meeting_items mi WHERE mi.meeting_id=m.id AND mi.kind=?)")
            parameters.append(kind)
        parameters.append(limit)
        meetings = [
            self._decode_meeting(row)
            for row in self._rows(
                f"""
                SELECT m.*, s.path AS source_path,
                       (SELECT COUNT(*) FROM meeting_items mi WHERE mi.meeting_id=m.id) AS item_count,
                       (SELECT COUNT(*) FROM meeting_items mi WHERE mi.meeting_id=m.id
                           AND mi.kind IN ('action', 'commitment') AND mi.status='open') AS open_item_count
                FROM meetings m JOIN sources s ON s.id=m.source_id
                WHERE {' AND '.join(clauses)}
                ORDER BY COALESCE(m.occurred_at, m.created_at) DESC, m.created_at DESC LIMIT ?
                """,
                tuple(parameters),
            )
        ]
        if person:
            needle = person.casefold()
            meetings = [
                meeting
                for meeting in meetings
                if any(needle in str(participant).casefold() for participant in meeting["participants"])
                or any(
                    needle in str(item.get("owner") or "").casefold()
                    for item in self.meeting_items(meeting["id"])
                )
            ]
        for meeting in meetings:
            items = self.meeting_items(meeting["id"])
            counts = {
                item_kind: sum(item["kind"] == item_kind for item in items)
                for item_kind in (
                    "topic",
                    "decision",
                    "action",
                    "commitment",
                    "risk",
                    "question",
                )
            }
            meeting["item_counts"] = counts
            meeting["open_attention"] = sum(
                item["status"] == "open"
                and item["kind"] in {"action", "commitment", "risk", "question"}
                for item in items
            )
        return meetings

    def meeting_items(
        self,
        meeting_id: str | None = None,
        *,
        workspace_id: str | None = None,
        person: str | None = None,
        period_start: str | None = None,
        period_end: str | None = None,
        kind: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        parameters: list[Any] = []
        if meeting_id:
            clauses.append("mi.meeting_id = ?")
            parameters.append(meeting_id)
        if workspace_id:
            clauses.append("m.workspace_id = ?")
            parameters.append(workspace_id)
        if period_start:
            clauses.append("COALESCE(m.occurred_at, m.created_at) >= ?")
            parameters.append(period_start)
        if period_end:
            clauses.append("COALESCE(m.occurred_at, m.created_at) <= ?")
            parameters.append(period_end)
        if kind:
            clauses.append("mi.kind = ?")
            parameters.append(kind)
        if status:
            clauses.append("mi.status = ?")
            parameters.append(status)
        where = " AND ".join(clauses) if clauses else "1=1"
        rows = self._rows(
            f"""
            SELECT mi.*, m.workspace_id, m.source_id, s.path AS source_path,
                   m.title AS meeting_title, m.occurred_at
            FROM meeting_items mi
            JOIN meetings m ON m.id=mi.meeting_id
            JOIN sources s ON s.id=m.source_id
            WHERE {where}
            ORDER BY COALESCE(mi.due_at, m.occurred_at, mi.created_at), mi.created_at
            """,
            tuple(parameters),
        )
        if person:
            needle = person.casefold()
            rows = [row for row in rows if needle in str(row.get("owner") or "").casefold()]
        return rows

    def list_meeting_items(
        self,
        workspace_id: str,
        *,
        kind: str | None = None,
        status: str | None = None,
        person: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> list[dict[str, Any]]:
        """Workspace-level item feed enriched with its meeting and source."""
        return self.meeting_items(
            workspace_id=workspace_id,
            person=person,
            period_start=date_from,
            period_end=date_to,
            kind=kind,
            status=status,
        )

    def meeting_selection_data(self, meeting_id: str) -> dict[str, Any]:
        return self.get_meeting(meeting_id, include_items=True)

    def update_meeting_item_status(self, item_id: str, status: str) -> dict[str, Any]:
        if status not in {"open", "done", "superseded"}:
            raise ValueError(status)
        with self.transaction() as connection:
            cursor = connection.execute(
                "UPDATE meeting_items SET status=?, updated_at=? WHERE id=?",
                (status, utc_now(), item_id),
            )
            if cursor.rowcount == 0:
                raise KeyError(item_id)
        return self._rows("SELECT * FROM meeting_items WHERE id=?", (item_id,))[0]

    def topic_timeline(
        self, workspace_id: str, topic: str, *, limit: int = 100
    ) -> list[dict[str, Any]]:
        needle = topic.casefold()
        rows = self.meeting_items(workspace_id=workspace_id)
        matching = [
            row
            for row in rows
            if needle in str(row.get("topic") or "").casefold()
            or needle in row["text"].casefold()
        ]
        matching.sort(key=lambda row: (row.get("occurred_at") or row["created_at"], row["created_at"]))
        return matching[-limit:]

    @staticmethod
    def normalize_decision_thread(value: str) -> str:
        """Return a conservative exact-match key for a decision topic/title.

        This intentionally performs no fuzzy or semantic grouping.  Two decisions
        share a history only when their Unicode-normalized topic (or fallback
        text) produces the exact same key.
        """

        normalized = unicodedata.normalize("NFKC", value).casefold().replace("ё", "е")
        return " ".join(re.findall(r"[\w-]+", normalized, flags=re.UNICODE))

    @staticmethod
    def _timeline_excerpt(value: Any, limit: int = 600) -> str:
        text = re.sub(r"\s+", " ", str(value or "")).strip()
        return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"

    @staticmethod
    def _timeline_epoch(value: str) -> float:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return parsed.timestamp()
        except (TypeError, ValueError):
            return 0.0

    def workspace_timeline(
        self,
        workspace_id: str,
        *,
        limit: int = 300,
    ) -> list[dict[str, Any]]:
        """Build one deterministic workspace chronology without changing data.

        The feed is a read model over durable entities.  Decision histories use
        exact normalized topic/title keys and expose both sequence position and
        the latest current item for that key.
        """

        self.get_workspace(workspace_id)
        limit = max(1, min(int(limit), 1_000))
        timeline: list[dict[str, Any]] = []

        def target(section: str, entity_type: str, entity_id: str) -> dict[str, str]:
            return {
                "section": section,
                "entity_type": entity_type,
                "entity_id": entity_id,
            }

        for task in self._rows(
            "SELECT * FROM tasks WHERE workspace_id=?",
            (workspace_id,),
        ):
            timeline.append(
                {
                    "id": f"task:{task['id']}",
                    "type": "task",
                    "title": f"Создана задача: {task['title']}",
                    "detail": self._timeline_excerpt(task.get("result") or ""),
                    "timestamp": task["created_at"],
                    "status": task["status"],
                    "target": target("tasks", "task", task["id"]),
                    "source": None,
                }
            )

        for event in self._rows(
            """
            SELECT e.*, t.title AS task_title
            FROM task_events e JOIN tasks t ON t.id=e.task_id
            WHERE t.workspace_id=?
            """,
            (workspace_id,),
        ):
            timeline.append(
                {
                    "id": f"task_event:{event['id']}",
                    "type": "task_event",
                    "title": event["title"],
                    "detail": self._timeline_excerpt(
                        event.get("detail") or f"Задача: {event['task_title']}"
                    ),
                    "timestamp": event["created_at"],
                    "status": event["kind"],
                    "target": target("tasks", "task", event["task_id"]),
                    "source": None,
                }
            )

        for meeting in self._rows(
            """
            SELECT m.*, s.path AS source_path
            FROM meetings m JOIN sources s ON s.id=m.source_id
            WHERE m.workspace_id=?
            """,
            (workspace_id,),
        ):
            timeline.append(
                {
                    "id": f"meeting:{meeting['id']}",
                    "type": "meeting",
                    "title": meeting["title"],
                    "detail": self._timeline_excerpt(meeting.get("summary")),
                    "timestamp": meeting.get("occurred_at") or meeting["created_at"],
                    "status": meeting["status"],
                    "target": target("meetings", "meeting", meeting["id"]),
                    "source": {
                        "id": meeting["source_id"],
                        "title": meeting["title"],
                        "path": meeting.get("source_path"),
                    },
                }
            )

        for source in self._rows(
            "SELECT * FROM sources WHERE workspace_id=?",
            (workspace_id,),
        ):
            timeline.append(
                {
                    "id": f"source:{source['id']}",
                    "type": "source",
                    "title": f"Добавлен источник: {source['title']}",
                    "detail": source["kind"],
                    "timestamp": source["created_at"],
                    "status": source["visibility"],
                    "target": target("workspaces", "source", source["id"]),
                    "source": {
                        "id": source["id"],
                        "title": source["title"],
                        "path": source.get("path"),
                    },
                }
            )

        artifact_sources: dict[str, dict[str, Any]] = {}
        for relation in self._rows(
            """
            SELECT ar.*, s.title AS source_title, s.path AS source_path,
                   s.content AS source_content
            FROM artifact_relations ar
            JOIN artifacts a ON a.id=ar.artifact_id
            JOIN sources s ON s.id=ar.source_id
            WHERE a.workspace_id=? AND ar.relation_type='derived_from_source'
            ORDER BY ar.artifact_id, ar.artifact_version DESC,
                     ar.created_at DESC, ar.id
            """,
            (workspace_id,),
        ):
            if relation["artifact_id"] in artifact_sources:
                continue
            try:
                metadata = json.loads(relation.get("metadata") or "{}")
            except json.JSONDecodeError:
                metadata = {}
            start = metadata.get("char_start")
            end = metadata.get("char_end")
            content = str(relation.get("source_content") or "")
            exact_span = (
                isinstance(start, int)
                and isinstance(end, int)
                and 0 <= start < end <= len(content)
            )
            artifact_sources[relation["artifact_id"]] = {
                "id": relation["source_id"],
                "title": relation["source_title"],
                "path": relation.get("source_path"),
                "char_start": start if exact_span else None,
                "char_end": end if exact_span else None,
                "excerpt": content[start:end] if exact_span else "",
            }

        for artifact in self._rows(
            "SELECT * FROM artifacts WHERE workspace_id=?",
            (workspace_id,),
        ):
            timeline.append(
                {
                    "id": f"artifact:{artifact['id']}",
                    "type": "artifact",
                    "title": f"Материал: {artifact['title']}",
                    "detail": f"{artifact['kind']} · версия {artifact['current_version']}",
                    "timestamp": artifact["created_at"],
                    "status": "ready",
                    "target": target("artifacts", "artifact", artifact["id"]),
                    "source": artifact_sources.get(artifact["id"]),
                }
            )

        for approval in self._rows(
            """
            SELECT a.* FROM approvals a
            JOIN tasks t ON t.id=a.task_id
            WHERE t.workspace_id=?
            """,
            (workspace_id,),
        ):
            timeline.append(
                {
                    "id": f"approval:{approval['id']}",
                    "type": "approval",
                    "title": approval["title"],
                    "detail": self._timeline_excerpt(
                        f"{approval['action_type']} · риск {approval['risk']}"
                    ),
                    "timestamp": approval.get("updated_at") or approval["created_at"],
                    "status": approval["status"],
                    "target": target("approvals", "approval", approval["id"]),
                    "source": None,
                }
            )

        decisions = self._rows(
            """
            SELECT mi.*, m.workspace_id, m.source_id, m.title AS meeting_title,
                   m.occurred_at, m.created_at AS meeting_created_at,
                   s.path AS source_path
            FROM meeting_items mi
            JOIN meetings m ON m.id=mi.meeting_id
            JOIN sources s ON s.id=m.source_id
            WHERE m.workspace_id=? AND mi.kind='decision'
            """,
            (workspace_id,),
        )
        threads: dict[str, list[dict[str, Any]]] = {}
        for decision in decisions:
            thread_title = str(decision.get("topic") or decision["text"]).strip()
            thread_key = self.normalize_decision_thread(thread_title)
            if not thread_key:
                thread_key = f"decision:{decision['id']}"
            decision["timeline_timestamp"] = (
                decision.get("occurred_at")
                or decision.get("meeting_created_at")
                or decision["created_at"]
            )
            decision["thread_title"] = thread_title
            threads.setdefault(thread_key, []).append(decision)

        for thread_key, history in threads.items():
            history.sort(
                key=lambda item: (
                    self._timeline_epoch(item["timeline_timestamp"]),
                    item["created_at"],
                    item["id"],
                )
            )
            current = history[-1]
            for sequence, decision in enumerate(history, start=1):
                timeline.append(
                    {
                        "id": f"decision:{decision['id']}",
                        "type": "decision",
                        "title": decision["thread_title"],
                        "detail": self._timeline_excerpt(decision["text"]),
                        "timestamp": decision["timeline_timestamp"],
                        "status": decision["status"],
                        "target": target(
                            "meetings", "meeting", decision["meeting_id"]
                        ),
                        "source": {
                            "id": decision["source_id"],
                            "title": decision["meeting_title"],
                            "path": decision.get("source_path"),
                            "char_start": decision["source_start"],
                            "char_end": decision["source_end"],
                            "excerpt": decision["source_quote"],
                        },
                        "decision_thread_key": thread_key,
                        "decision_sequence": sequence,
                        "decision_count": len(history),
                        "is_current_decision": decision["id"] == current["id"],
                        "current_decision_id": current["id"],
                        "current_decision_text": self._timeline_excerpt(current["text"]),
                    }
                )

        type_order = {
            "decision": 0,
            "meeting": 1,
            "task_event": 2,
            "task": 3,
            "artifact": 4,
            "source": 5,
            "approval": 6,
        }
        timeline.sort(
            key=lambda item: (
                -self._timeline_epoch(str(item["timestamp"])),
                type_order.get(str(item["type"]), 99),
                str(item["id"]),
            )
        )
        return timeline[:limit]

    @staticmethod
    def _meeting_item_similarity(left: dict[str, Any], right: dict[str, Any]) -> float:
        def normalized(item: dict[str, Any]) -> str:
            return " ".join(re.findall(r"[\w-]+", item["text"].casefold(), flags=re.UNICODE))

        score = SequenceMatcher(None, normalized(left), normalized(right)).ratio()
        if left["kind"] == right["kind"]:
            score += 0.12
        if left.get("topic") and right.get("topic") and left["topic"].casefold() == right["topic"].casefold():
            score += 0.08
        return min(score, 1.0)

    def compare_meetings(self, before_id: str, after_id: str) -> dict[str, Any]:
        before = self.get_meeting(before_id, include_items=True)
        after = self.get_meeting(after_id, include_items=True)
        remaining = list(after["items"])
        removed: list[dict[str, Any]] = []
        changed: list[dict[str, Any]] = []
        unchanged: list[dict[str, Any]] = []
        for old in before["items"]:
            candidates = [item for item in remaining if item["kind"] == old["kind"]]
            if not candidates:
                removed.append(old)
                continue
            match = max(candidates, key=lambda item: self._meeting_item_similarity(old, item))
            similarity = self._meeting_item_similarity(old, match)
            if similarity < 0.48:
                removed.append(old)
                continue
            remaining.remove(match)
            if similarity >= 0.92 and old.get("owner") == match.get("owner") and old.get("due_at") == match.get("due_at"):
                unchanged.append(match)
            else:
                changed.append({"before": old, "after": match, "similarity": round(similarity, 3)})
        return {
            "before": {key: before[key] for key in ("id", "title", "occurred_at", "summary")},
            "after": {key: after[key] for key in ("id", "title", "occurred_at", "summary")},
            "added": remaining,
            "removed": removed,
            "changed": changed,
            "unchanged": unchanged,
        }

    def briefing_data(
        self,
        workspace_id: str,
        *,
        person: str | None = None,
        since: str | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        meetings = self.list_meetings(
            workspace_id, person=person, period_start=since, limit=limit
        )
        meeting_ids = {meeting["id"] for meeting in meetings}
        items = [
            item
            for item in self.meeting_items(workspace_id=workspace_id, person=person)
            if item["meeting_id"] in meeting_ids
        ]
        return {
            "workspace": self.get_workspace(workspace_id),
            "person": person,
            "meetings": meetings,
            "recent_decisions": [item for item in items if item["kind"] == "decision"],
            "open_actions": [
                item
                for item in items
                if item["kind"] in {"action", "commitment"} and item["status"] == "open"
            ],
            "risks": [item for item in items if item["kind"] == "risk" and item["status"] == "open"],
            "questions": [item for item in items if item["kind"] == "question" and item["status"] == "open"],
        }

    def remember(
        self,
        title: str,
        content: str,
        *,
        workspace_id: str | None = None,
        kind: str = "note",
        source_id: str | None = None,
        classification: str | None = None,
    ) -> dict[str, Any]:
        kind = normalize_memory_kind(kind)
        if not self.memory_kind_enabled(kind):
            raise ValueError("Этот тип рабочей памяти отключён в настройках")
        item_id = new_id()
        now = utc_now()
        inherited = "internal"
        if source_id:
            inherited = str(self.get_source(source_id)["classification"])
        elif workspace_id:
            inherited = str(self.get_workspace(workspace_id)["classification"])
        classification = normalize_classification(
            classification,
            default=inherited,
        )
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO memory(
                    id, workspace_id, kind, title, content, source_id,
                    created_at, updated_at, classification
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item_id,
                    workspace_id,
                    kind,
                    title,
                    content,
                    source_id,
                    now,
                    now,
                    classification,
                ),
            )
        self.audit(None, "memory.create", item_id, "success", title)
        return self._rows("SELECT * FROM memory WHERE id = ?", (item_id,))[0]

    def delete_memory(self, item_id: str) -> None:
        with self.transaction() as connection:
            connection.execute("DELETE FROM memory WHERE id = ?", (item_id,))
        self.audit(None, "memory.delete", item_id, "success")

    def update_memory(
        self,
        item_id: str,
        title: str,
        content: str,
        *,
        kind: str | None = None,
    ) -> None:
        current = self._rows("SELECT * FROM memory WHERE id=?", (item_id,))
        if not current:
            raise KeyError(item_id)
        resolved_kind = normalize_memory_kind(kind or current[0]["kind"])
        if not self.memory_kind_enabled(resolved_kind):
            raise ValueError("Этот тип рабочей памяти отключён в настройках")
        with self.transaction() as connection:
            cursor = connection.execute(
                "UPDATE memory SET kind=?, title=?, content=?, updated_at=? WHERE id=?",
                (resolved_kind, title, content, utc_now(), item_id),
            )
            if cursor.rowcount == 0:
                raise KeyError(item_id)
        self.audit(None, "memory.update", item_id, "success", title)

    def memory_kind_enabled(self, kind: str) -> bool:
        resolved = normalize_memory_kind(kind)
        settings = self.settings()
        return (
            settings.get("memory_enabled") == "true"
            and settings.get(MEMORY_KIND_SETTING[resolved], "true") == "true"
        )

    def create_or_update_skill(
        self,
        name: str,
        command: str,
        description: str,
        instruction: str,
        *,
        scope: str = "personal",
        workspace_id: str | None = None,
        skill_id: str | None = None,
        classification: str | None = None,
    ) -> dict[str, Any]:
        now = utc_now()
        command = command if command.startswith("/") else f"/{command}"
        with self.transaction() as connection:
            if skill_id:
                current = connection.execute("SELECT * FROM skills WHERE id = ?", (skill_id,)).fetchone()
                if current is None:
                    raise KeyError(skill_id)
                classification = normalize_classification(
                    classification,
                    default=str(current["classification"]),
                )
                version = int(current["version"]) + 1
                connection.execute(
                    "INSERT INTO skill_versions VALUES (?, ?, ?, ?, ?)",
                    (new_id(), skill_id, current["version"], current["instruction"], now),
                )
                connection.execute(
                    """
                    UPDATE skills SET name=?, command=?, description=?, instruction=?, scope=?,
                                      workspace_id=?, version=?, updated_at=?, classification=?
                    WHERE id=?
                    """,
                    (
                        name,
                        command,
                        description,
                        instruction,
                        scope,
                        workspace_id,
                        version,
                        now,
                        classification,
                        skill_id,
                    ),
                )
            else:
                skill_id = new_id()
                classification = normalize_classification(classification)
                connection.execute(
                    """
                    INSERT INTO skills(
                        id, name, command, description, instruction, scope,
                        workspace_id, version, builtin, enabled, created_at,
                        updated_at, classification
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, 0, 1, ?, ?, ?)
                    """,
                    (
                        skill_id,
                        name,
                        command,
                        description,
                        instruction,
                        scope,
                        workspace_id,
                        now,
                        now,
                        classification,
                    ),
                )
        self.audit(None, "skill.save", skill_id, "success", command)
        return self._rows("SELECT * FROM skills WHERE id = ?", (skill_id,))[0]

    def find_skill(self, text: str, workspace_id: str | None = None) -> dict[str, Any] | None:
        command = text.strip().split(maxsplit=1)[0] if text.strip().startswith("/") else None
        if command:
            rows = self._rows(
                "SELECT * FROM skills WHERE command = ? AND enabled = 1", (command,)
            )
            return rows[0] if rows else None
        lowered = text.casefold()
        mapping = [
            ("meeting-analysis", ("встреч", "транскрипт", "поручен")),
            ("briefing", ("подготовь меня", "брифинг", "briefing")),
            ("digest", ("дайджест", "сводк", "итоги недели")),
            ("document", ("документ", "отчёт", "тз", "артефакт")),
            ("research", ("исслед", "сравни", "проанализируй")),
        ]
        for skill_id, markers in mapping:
            if any(marker in lowered for marker in markers):
                rows = self._rows("SELECT * FROM skills WHERE id = ? AND enabled = 1", (skill_id,))
                if rows:
                    return rows[0]
        if workspace_id:
            rows = self._rows(
                "SELECT * FROM skills WHERE workspace_id = ? AND enabled = 1 ORDER BY updated_at DESC LIMIT 1",
                (workspace_id,),
            )
            if rows:
                return rows[0]
        return None

    def create_artifact(
        self,
        workspace_id: str,
        task_id: str | None,
        title: str,
        content: str,
        kind: str = "markdown",
        *,
        source_refs: list[str | dict[str, Any]] | None = None,
        related_artifact_id: str | None = None,
        related_artifact_version: int | None = None,
        metadata: dict[str, Any] | None = None,
        classification: str | None = None,
    ) -> dict[str, Any]:
        classification_inputs = [
            str(self.get_workspace(workspace_id)["classification"])
        ]
        if task_id:
            task = self.get_task(task_id)
            if task["workspace_id"] != workspace_id:
                raise ValueError("Задача и артефакт должны принадлежать одному Workspace")
            classification_inputs.append(str(task["classification"]))
        normalized_sources = self._artifact_source_refs(source_refs or [], workspace_id)
        classification_inputs.extend(
            str(source["classification"]) for source in normalized_sources
        )
        if related_artifact_id:
            related = self.get_artifact(related_artifact_id)
            if related["workspace_id"] != workspace_id:
                raise ValueError("Связанные артефакты должны принадлежать одному Workspace")
            classification_inputs.append(str(related["classification"]))
            related_artifact_version = related_artifact_version or int(
                related["current_version"]
            )
        if classification is not None:
            classification_inputs.append(normalize_classification(classification))
        classification = highest_classification(classification_inputs)
        artifact_id = new_id()
        directory = self.artifacts_dir / artifact_id
        directory.mkdir(parents=True)
        suffix = {"markdown": ".md", "text": ".txt", "json": ".json"}.get(kind, ".md")
        path = directory / f"v1{suffix}"
        path.write_text(content, encoding="utf-8")
        now = utc_now()
        version_metadata = {
            **(metadata or {}),
            "task_id": task_id,
            "source_ids": [item["id"] for item in normalized_sources],
        }
        if related_artifact_id:
            version_metadata["related_artifact_id"] = related_artifact_id
            version_metadata["related_artifact_version"] = related_artifact_version
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO artifacts(
                    id, workspace_id, task_id, title, kind, path,
                    current_version, created_at, updated_at, classification
                ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
                """,
                (
                    artifact_id,
                    workspace_id,
                    task_id,
                    title,
                    kind,
                    str(path),
                    now,
                    now,
                    classification,
                ),
            )
            connection.execute(
                """
                INSERT INTO artifact_versions(
                    id, artifact_id, version, path, metadata, created_at,
                    classification
                ) VALUES (?, ?, 1, ?, ?, ?, ?)
                """,
                (
                    new_id(),
                    artifact_id,
                    str(path),
                    json.dumps(version_metadata, ensure_ascii=False),
                    now,
                    classification,
                ),
            )
            if task_id:
                self._insert_artifact_relation(
                    connection,
                    artifact_id,
                    1,
                    "produced_by_task",
                    task_id=task_id,
                    metadata=metadata,
                    created_at=now,
                )
            for source in normalized_sources:
                self._insert_artifact_relation(
                    connection,
                    artifact_id,
                    1,
                    "derived_from_source",
                    source_id=source["id"],
                    metadata=source["metadata"],
                    created_at=now,
                )
            if related_artifact_id:
                self._insert_artifact_relation(
                    connection,
                    artifact_id,
                    1,
                    "derived_from_artifact",
                    related_artifact_id=related_artifact_id,
                    related_artifact_version=related_artifact_version,
                    metadata=metadata,
                    created_at=now,
                )
        self.audit(task_id, "artifact.create", artifact_id, "success", title)
        return self.get_artifact(artifact_id)

    def get_artifact(self, artifact_id: str) -> dict[str, Any]:
        rows = self._rows("SELECT * FROM artifacts WHERE id = ?", (artifact_id,))
        if not rows:
            raise KeyError(artifact_id)
        return rows[0]

    def artifact_content(self, artifact_id: str) -> str:
        artifact = self.get_artifact(artifact_id)
        path = Path(str(artifact["path"]))
        if not path.is_file():
            raise FileNotFoundError(f"Файл артефакта не найден: {path}")
        return path.read_text(encoding="utf-8")

    def delete_artifact(self, artifact_id: str) -> dict[str, Any]:
        artifact = self.get_artifact(artifact_id)
        version_paths = [
            Path(row["path"])
            for row in self._rows(
                "SELECT path FROM artifact_versions WHERE artifact_id=?",
                (artifact_id,),
            )
        ]
        candidates: list[Path] = []
        if version_paths:
            parents = {path.parent for path in version_paths}
            candidates.extend(parents if len(parents) == 1 else version_paths)
        elif artifact.get("path"):
            candidates.append(Path(artifact["path"]))
        moved = self._trash_managed_paths(candidates)
        try:
            with self.transaction() as connection:
                connection.execute("DELETE FROM inbox WHERE source_ref=?", (artifact_id,))
                cursor = connection.execute("DELETE FROM artifacts WHERE id=?", (artifact_id,))
                if cursor.rowcount == 0:
                    raise KeyError(artifact_id)
        except BaseException:
            self._restore_trashed_paths(moved)
            raise
        self.audit(
            artifact.get("task_id"),
            "artifact.delete",
            artifact_id,
            "success",
            artifact["title"],
        )
        return {
            "id": artifact_id,
            "title": artifact["title"],
            "trashed_files": len(moved),
        }

    def _source_managed_paths(self, source: dict[str, Any]) -> list[Path]:
        paths: list[Path] = []
        if source.get("path"):
            paths.append(Path(str(source["path"])))
        metadata = source.get("metadata")
        if isinstance(metadata, dict) and metadata.get("managed_audio_path"):
            paths.append(Path(str(metadata["managed_audio_path"])))
        return paths

    def _trash_managed_paths(
        self,
        paths: Iterator[Path] | list[Path] | tuple[Path, ...],
    ) -> list[tuple[Path, Path]]:
        roots = (self.files_dir.resolve(), self.artifacts_dir.resolve())
        normalized: list[Path] = []
        for raw_path in paths:
            path = raw_path.expanduser().resolve()
            if not path.exists() or not any(path.is_relative_to(root) for root in roots):
                continue
            if path not in normalized:
                normalized.append(path)
        normalized.sort(key=lambda item: len(item.parts))
        top_level = [
            path
            for path in normalized
            if not any(path.is_relative_to(parent) for parent in normalized if parent != path)
        ]
        if not top_level:
            return []
        self.trash_dir.mkdir(parents=True, exist_ok=True)
        moved: list[tuple[Path, Path]] = []
        try:
            for source in top_level:
                destination = self.trash_dir / (
                    f"RnD Workbench — {source.stem} — {new_id()[:8]}{source.suffix}"
                )
                shutil.move(str(source), str(destination))
                moved.append((source, destination))
        except BaseException:
            self._restore_trashed_paths(moved)
            raise
        return moved

    @staticmethod
    def _restore_trashed_paths(moved: list[tuple[Path, Path]]) -> None:
        for original, trashed in reversed(moved):
            if not trashed.exists():
                continue
            original.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(trashed), str(original))

    def update_artifact(
        self,
        artifact_id: str,
        content: str,
        *,
        source_refs: list[str | dict[str, Any]] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        current = self.get_artifact(artifact_id)
        normalized_sources = self._artifact_source_refs(
            source_refs or [], current["workspace_id"]
        )
        updated = self._append_artifact_version(
            current,
            content,
            relation_type="revision_of",
            related_version=int(current["current_version"]),
            source_refs=normalized_sources,
            metadata=metadata,
        )
        self.audit(
            current["task_id"],
            "artifact.update",
            artifact_id,
            "success",
            f"v{updated['current_version']}",
        )
        return updated

    def restore_artifact(self, artifact_id: str, version: int) -> dict[str, Any]:
        current = self.get_artifact(artifact_id)
        if current["kind"] != "markdown":
            raise ValueError("Восстановление версий поддерживается для Markdown-артефактов")
        rows = self._rows(
            "SELECT * FROM artifact_versions WHERE artifact_id=? AND version=?",
            (artifact_id, version),
        )
        if not rows:
            raise KeyError(f"Версия v{version} артефакта {artifact_id} не найдена")
        source_path = Path(rows[0]["path"])
        if not source_path.is_file():
            raise FileNotFoundError(f"Файл версии не найден: {source_path}")
        restored = self._append_artifact_version(
            current,
            source_path.read_text(encoding="utf-8"),
            relation_type="restored_from",
            related_version=version,
            metadata={"restored_from_version": version},
            classification=str(rows[0]["classification"]),
        )
        self.audit(
            current["task_id"],
            "artifact.restore",
            artifact_id,
            "success",
            f"v{version} -> v{restored['current_version']}",
        )
        return restored

    def artifact_versions(self, artifact_id: str) -> list[dict[str, Any]]:
        current = self.get_artifact(artifact_id)
        versions = self._rows(
            """
            SELECT id, artifact_id, version, path, metadata, created_at,
                   classification
            FROM artifact_versions WHERE artifact_id=? ORDER BY version
            """,
            (artifact_id,),
        )
        for item in versions:
            item["metadata"] = json.loads(item["metadata"] or "{}")
            item["is_current"] = int(item["version"]) == int(current["current_version"])
        return versions

    def artifact_relations(
        self,
        artifact_id: str,
        *,
        version: int | None = None,
    ) -> list[dict[str, Any]]:
        self.get_artifact(artifact_id)
        if version is None:
            rows = self._rows(
                """
                SELECT * FROM artifact_relations
                WHERE artifact_id=? ORDER BY artifact_version, created_at, id
                """,
                (artifact_id,),
            )
        else:
            rows = self._rows(
                """
                SELECT * FROM artifact_relations
                WHERE artifact_id=? AND artifact_version=? ORDER BY created_at, id
                """,
                (artifact_id, version),
            )
        for item in rows:
            item["metadata"] = json.loads(item["metadata"] or "{}")
        return rows

    def _append_artifact_version(
        self,
        current: dict[str, Any],
        content: str,
        *,
        relation_type: str,
        related_version: int,
        source_refs: list[dict[str, Any]] | None = None,
        metadata: dict[str, Any] | None = None,
        classification: str | None = None,
    ) -> dict[str, Any]:
        version = int(current["current_version"]) + 1
        previous_path = Path(current["path"])
        path = previous_path.with_name(f"v{version}{previous_path.suffix}")
        path.write_text(content, encoding="utf-8")
        now = utc_now()
        source_refs = source_refs or []
        classification_inputs = [str(current["classification"])]
        classification_inputs.extend(
            str(source["classification"]) for source in source_refs
        )
        if classification is not None:
            classification_inputs.append(normalize_classification(classification))
        version_classification = highest_classification(classification_inputs)
        version_metadata = {
            **(metadata or {}),
            "task_id": current["task_id"],
            "source_ids": [item["id"] for item in source_refs],
        }
        with self.transaction() as connection:
            connection.execute(
                """
                UPDATE artifacts
                SET path=?, current_version=?, updated_at=?, classification=?
                WHERE id=?
                """,
                (
                    str(path),
                    version,
                    now,
                    version_classification,
                    current["id"],
                ),
            )
            connection.execute(
                """
                INSERT INTO artifact_versions(
                    id, artifact_id, version, path, metadata, created_at,
                    classification
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    new_id(),
                    current["id"],
                    version,
                    str(path),
                    json.dumps(version_metadata, ensure_ascii=False),
                    now,
                    version_classification,
                ),
            )
            self._insert_artifact_relation(
                connection,
                current["id"],
                version,
                relation_type,
                related_artifact_id=current["id"],
                related_artifact_version=related_version,
                metadata=metadata,
                created_at=now,
            )
            if current["task_id"]:
                self._insert_artifact_relation(
                    connection,
                    current["id"],
                    version,
                    "produced_by_task",
                    task_id=current["task_id"],
                    metadata=metadata,
                    created_at=now,
                )
            for source in source_refs:
                self._insert_artifact_relation(
                    connection,
                    current["id"],
                    version,
                    "derived_from_source",
                    source_id=source["id"],
                    metadata=source["metadata"],
                    created_at=now,
                )
        return self.get_artifact(current["id"])

    def _artifact_source_refs(
        self,
        source_refs: list[str | dict[str, Any]],
        workspace_id: str,
    ) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        seen: set[str] = set()
        for reference in source_refs:
            source_id = str(reference["id"] if isinstance(reference, dict) else reference)
            if source_id in seen:
                continue
            source = self.get_source(source_id)
            if source["workspace_id"] != workspace_id:
                raise ValueError("Источник и артефакт должны принадлежать одному Workspace")
            relation_metadata: dict[str, Any] = {}
            if isinstance(reference, dict):
                for key in (
                    "chunk_id",
                    "char_start",
                    "char_end",
                    "selection",
                    "score",
                ):
                    if reference.get(key) is not None:
                        relation_metadata[key] = reference[key]
            normalized.append(
                {
                    "id": source_id,
                    "metadata": relation_metadata,
                    "classification": source["classification"],
                }
            )
            seen.add(source_id)
        return normalized

    @staticmethod
    def _insert_artifact_relation(
        connection: sqlite3.Connection,
        artifact_id: str,
        artifact_version: int,
        relation_type: str,
        *,
        task_id: str | None = None,
        source_id: str | None = None,
        related_artifact_id: str | None = None,
        related_artifact_version: int | None = None,
        metadata: dict[str, Any] | None = None,
        created_at: str | None = None,
    ) -> None:
        connection.execute(
            """
            INSERT INTO artifact_relations(
                id, artifact_id, artifact_version, relation_type, task_id,
                source_id, related_artifact_id, related_artifact_version,
                metadata, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                new_id(),
                artifact_id,
                artifact_version,
                relation_type,
                task_id,
                source_id,
                related_artifact_id,
                related_artifact_version,
                json.dumps(metadata or {}, ensure_ascii=False),
                created_at or utc_now(),
            ),
        )

    def add_inbox(
        self,
        workspace_id: str | None,
        kind: str,
        title: str,
        detail: str = "",
        *,
        priority: int = 0,
        source_ref: str | None = None,
    ) -> str:
        item_id = new_id()
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO inbox VALUES (?, ?, ?, ?, ?, ?, 'new', ?, ?)",
                (item_id, workspace_id, kind, title, detail, priority, source_ref, utc_now()),
            )
        return item_id

    def set_inbox_status(self, item_id: str, status: str) -> None:
        with self.transaction() as connection:
            connection.execute("UPDATE inbox SET status = ? WHERE id = ?", (status, item_id))

    def upsert_inbox_event(
        self,
        key: str,
        title: str,
        detail: str,
        priority: int,
        kind: str,
        workspace_id: str | None,
        source_ref: str | None = None,
    ) -> str:
        """Create or refresh one stable attention item without duplicating it."""
        stable_ref = source_ref or key
        now = utc_now()
        with self.transaction() as connection:
            current = connection.execute(
                """
                SELECT id FROM inbox
                WHERE kind=? AND source_ref=? AND workspace_id IS ?
                ORDER BY created_at DESC LIMIT 1
                """,
                (kind, stable_ref, workspace_id),
            ).fetchone()
            if current:
                item_id = current["id"]
                connection.execute(
                    """
                    UPDATE inbox SET title=?, detail=?, priority=?, status='new', created_at=?
                    WHERE id=?
                    """,
                    (title, detail, priority, now, item_id),
                )
            else:
                item_id = new_id()
                connection.execute(
                    "INSERT INTO inbox VALUES (?, ?, ?, ?, ?, ?, 'new', ?, ?)",
                    (item_id, workspace_id, kind, title, detail, priority, stable_ref, now),
                )
        return item_id

    def create_approval(
        self,
        task_id: str | None,
        action_type: str,
        title: str,
        payload: dict[str, Any],
        risk: str = "medium",
        *,
        actor: str = "local-user",
        origin: str = "assistant",
        workflow_id: str | None = None,
        step_index: int = 0,
        confirmation_policy: str = "explicit",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        normalized_risk = {
            "safe": "low",
            "confirm": "medium",
        }.get(risk.casefold().strip(), risk.casefold().strip())
        if normalized_risk not in {"low", "medium", "high", "critical"}:
            raise ValueError("Риск действия должен быть low, medium, high или critical")
        confirmation_policy = confirmation_policy.casefold().strip()
        if confirmation_policy not in {"none", "explicit", "two_step"}:
            raise ValueError(
                "Политика подтверждения должна быть none, explicit или two_step"
            )
        if normalized_risk != "low" and confirmation_policy == "none":
            raise ValueError("Значимое внешнее действие требует подтверждения")
        if step_index < 0:
            raise ValueError("Номер шага не может быть отрицательным")
        if not isinstance(payload, dict):
            raise ValueError("Параметры действия должны быть JSON-объектом")

        serialized_payload = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if workflow_id is None:
            workflow_seed = "\x1f".join(
                (task_id or "standalone", origin, action_type, serialized_payload)
            )
            workflow_id = hashlib.sha256(workflow_seed.encode("utf-8")).hexdigest()[:24]
        if idempotency_key is None:
            idempotency_seed = "\x1f".join(
                (
                    workflow_id,
                    str(step_index),
                    action_type,
                    serialized_payload,
                )
            )
            idempotency_key = "act_" + hashlib.sha256(
                idempotency_seed.encode("utf-8")
            ).hexdigest()

        approval_id = new_id()
        now = utc_now()
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM approvals WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                return dict(existing)
            connection.execute(
                """
                INSERT INTO approvals(
                    id, task_id, action_type, title, payload, risk, status,
                    actor, origin, workflow_id, step_index, idempotency_key,
                    confirmation_policy, revision, result, resolved_by,
                    resolved_at, created_at, updated_at
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, 1,
                    '', NULL, NULL, ?, ?
                )
                """,
                (
                    approval_id,
                    task_id,
                    action_type,
                    title,
                    serialized_payload,
                    normalized_risk,
                    actor,
                    origin,
                    workflow_id,
                    step_index,
                    idempotency_key,
                    confirmation_policy,
                    now,
                    now,
                ),
            )
        self.audit(
            task_id,
            "approval.request",
            approval_id,
            "pending",
            self._approval_audit_detail(
                action_type=action_type,
                risk=normalized_risk,
                confirmation_policy=confirmation_policy,
                workflow_id=workflow_id,
                step_index=step_index,
                revision=1,
            ),
            actor=actor,
            origin=origin,
        )
        return self._rows("SELECT * FROM approvals WHERE id = ?", (approval_id,))[0]

    def resolve_approval(
        self,
        approval_id: str,
        status: str,
        *,
        actor: str = "local-user",
        origin: str = "approval_center",
    ) -> dict[str, Any]:
        if status not in {"approved", "rejected"}:
            raise ValueError(status)
        rows = self._rows("SELECT * FROM approvals WHERE id=?", (approval_id,))
        if not rows:
            raise KeyError(approval_id)
        current = rows[0]
        if current["status"] == status:
            return current
        if current["status"] != "pending":
            raise ValueError(
                f"Действие в состоянии {current['status']} нельзя перевести в {status}"
            )
        now = utc_now()
        result = "Действие отклонено пользователем" if status == "rejected" else ""
        with self.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE approvals
                SET status=?, result=?, resolved_by=?, resolved_at=?, updated_at=?
                WHERE id=? AND status='pending'
                """,
                (status, result, actor, now, now, approval_id),
            )
            if cursor.rowcount == 0:
                raise ValueError("Состояние согласования уже изменилось")
        self.audit(
            current["task_id"],
            "approval.decision",
            approval_id,
            status,
            self._approval_audit_detail(
                action_type=current["action_type"],
                risk=current["risk"],
                confirmation_policy=current["confirmation_policy"],
                workflow_id=current["workflow_id"],
                step_index=int(current["step_index"]),
                revision=int(current["revision"]),
            ),
            actor=actor,
            origin=origin,
        )
        return self._rows("SELECT * FROM approvals WHERE id=?", (approval_id,))[0]

    def begin_approval_execution(
        self,
        approval_id: str,
        *,
        actor: str = "system",
        origin: str = "executor",
    ) -> dict[str, Any]:
        rows = self._rows("SELECT * FROM approvals WHERE id=?", (approval_id,))
        if not rows:
            raise KeyError(approval_id)
        current = rows[0]
        if current["status"] == "executing":
            return current
        if current["status"] != "approved":
            raise ValueError("Выполнить можно только подтверждённое действие")
        predecessors = self._rows(
            """
            SELECT step_index, status FROM approvals
            WHERE workflow_id=? AND step_index<?
            ORDER BY step_index
            """,
            (current["workflow_id"], current["step_index"]),
        )
        blocked = [item for item in predecessors if item["status"] != "succeeded"]
        if blocked:
            first = blocked[0]
            raise ValueError(
                "Сначала должен успешно завершиться шаг "
                f"{first['step_index']} (сейчас: {first['status']})"
            )
        with self.transaction() as connection:
            connection.execute(
                "UPDATE approvals SET status='executing', updated_at=? WHERE id=?",
                (utc_now(), approval_id),
            )
        self.audit(
            current["task_id"],
            "approval.execute",
            approval_id,
            "executing",
            self._approval_audit_detail(
                action_type=current["action_type"],
                risk=current["risk"],
                confirmation_policy=current["confirmation_policy"],
                workflow_id=current["workflow_id"],
                step_index=int(current["step_index"]),
                revision=int(current["revision"]),
            ),
            actor=actor,
            origin=origin,
        )
        return self._rows("SELECT * FROM approvals WHERE id=?", (approval_id,))[0]

    def cancel_approval_dependents(
        self,
        approval_id: str,
        *,
        actor: str = "system",
        origin: str = "workflow",
    ) -> list[dict[str, Any]]:
        rows = self._rows("SELECT * FROM approvals WHERE id=?", (approval_id,))
        if not rows:
            raise KeyError(approval_id)
        current = rows[0]
        dependents = self._rows(
            """
            SELECT * FROM approvals
            WHERE workflow_id=? AND step_index>? AND status IN ('pending', 'approved')
            ORDER BY step_index
            """,
            (current["workflow_id"], current["step_index"]),
        )
        if not dependents:
            return []
        now = utc_now()
        with self.transaction() as connection:
            for dependent in dependents:
                connection.execute(
                    """
                    UPDATE approvals
                    SET status='cancelled',
                        result='Отменено после отказа от предыдущего шага',
                        resolved_by=?, resolved_at=?, updated_at=?
                    WHERE id=? AND status IN ('pending', 'approved')
                    """,
                    (actor, now, now, dependent["id"]),
                )
        for dependent in dependents:
            self.audit(
                dependent["task_id"],
                "approval.workflow_cancel",
                dependent["id"],
                "cancelled",
                self._approval_audit_detail(
                    action_type=dependent["action_type"],
                    risk=dependent["risk"],
                    confirmation_policy=dependent["confirmation_policy"],
                    workflow_id=dependent["workflow_id"],
                    step_index=int(dependent["step_index"]),
                    revision=int(dependent["revision"]),
                    result_code="predecessor_rejected",
                ),
                actor=actor,
                origin=origin,
            )
        return self._rows(
            "SELECT * FROM approvals WHERE workflow_id=? ORDER BY step_index",
            (current["workflow_id"],),
        )

    def complete_approval_execution(
        self,
        approval_id: str,
        *,
        success: bool,
        result_code: str,
        result: str,
        actor: str = "system",
        origin: str = "executor",
    ) -> dict[str, Any]:
        rows = self._rows("SELECT * FROM approvals WHERE id=?", (approval_id,))
        if not rows:
            raise KeyError(approval_id)
        current = rows[0]
        target_status = "succeeded" if success else "error"
        if current["status"] == target_status:
            return current
        if current["status"] != "executing" and not (
            current["status"] == "approved" and not success
        ):
            raise ValueError(
                "Успешно завершить можно только выполняющееся действие; "
                "ошибка также допустима после подтверждения"
            )
        with self.transaction() as connection:
            connection.execute(
                "UPDATE approvals SET status=?, result=?, updated_at=? WHERE id=?",
                (target_status, result, utc_now(), approval_id),
            )
        self.audit(
            current["task_id"],
            "approval.execute",
            approval_id,
            target_status,
            self._approval_audit_detail(
                action_type=current["action_type"],
                risk=current["risk"],
                confirmation_policy=current["confirmation_policy"],
                workflow_id=current["workflow_id"],
                step_index=int(current["step_index"]),
                revision=int(current["revision"]),
                result_code=result_code,
            ),
            actor=actor,
            origin=origin,
        )
        return self._rows("SELECT * FROM approvals WHERE id=?", (approval_id,))[0]

    def update_approval_payload(
        self,
        approval_id: str,
        payload: dict[str, Any],
        *,
        actor: str = "local-user",
        origin: str = "approval_center",
    ) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError("Параметры действия должны быть JSON-объектом")
        rows = self._rows("SELECT * FROM approvals WHERE id=?", (approval_id,))
        if not rows:
            raise KeyError(approval_id)
        current = rows[0]
        if current["status"] not in {"pending", "error"}:
            raise ValueError(
                "Изменить можно только ожидающее или завершившееся ошибкой действие"
            )
        serialized_payload = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        idempotency_seed = "\x1f".join(
            (
                current["workflow_id"],
                str(current["step_index"]),
                current["action_type"],
                serialized_payload,
            )
        )
        idempotency_key = "act_" + hashlib.sha256(
            idempotency_seed.encode("utf-8")
        ).hexdigest()
        revision = int(current["revision"]) + 1
        with self.transaction() as connection:
            duplicate = connection.execute(
                "SELECT id FROM approvals WHERE idempotency_key=? AND id!=?",
                (idempotency_key, approval_id),
            ).fetchone()
            if duplicate is not None:
                raise ValueError("Такое действие уже находится в плане")
            cursor = connection.execute(
                """
                UPDATE approvals
                SET payload=?, idempotency_key=?, revision=?, status='pending',
                    result='', resolved_by=NULL, resolved_at=NULL, updated_at=?
                WHERE id=? AND status IN ('pending', 'error')
                """,
                (
                    serialized_payload,
                    idempotency_key,
                    revision,
                    utc_now(),
                    approval_id,
                ),
            )
            if cursor.rowcount == 0:
                raise ValueError("Состояние согласования уже изменилось")
        self.audit(
            current["task_id"],
            "approval.replan",
            approval_id,
            "pending",
            self._approval_audit_detail(
                action_type=current["action_type"],
                risk=current["risk"],
                confirmation_policy=current["confirmation_policy"],
                workflow_id=current["workflow_id"],
                step_index=int(current["step_index"]),
                revision=revision,
            ),
            actor=actor,
            origin=origin,
        )
        return self._rows("SELECT * FROM approvals WHERE id=?", (approval_id,))[0]

    def approval_history(self, approval_id: str) -> list[dict[str, Any]]:
        return self._rows(
            """
            SELECT action, status, detail, actor, origin, created_at
            FROM audit_log
            WHERE target=? AND action LIKE 'approval.%'
            ORDER BY rowid
            """,
            (approval_id,),
        )

    @staticmethod
    def _approval_audit_detail(
        *,
        action_type: str,
        risk: str,
        confirmation_policy: str,
        workflow_id: str,
        step_index: int,
        revision: int,
        result_code: str | None = None,
    ) -> str:
        """Return audit metadata only; never copy the external payload."""

        detail: dict[str, Any] = {
            "action_type": action_type,
            "confirmation_policy": confirmation_policy,
            "revision": revision,
            "risk": risk,
            "step_index": step_index,
            "workflow_id": workflow_id,
        }
        if result_code:
            detail["result_code"] = result_code
        return json.dumps(detail, ensure_ascii=False, sort_keys=True)

    def create_automation(
        self,
        workspace_id: str | None,
        name: str,
        prompt: str,
        schedule: str,
        *,
        classification: str | None = None,
    ) -> dict[str, Any]:
        automation_id = new_id()
        now = utc_now()
        # Natural-language schedules are expressed in the Mac's local timezone,
        # while persisted timestamps stay comparable in UTC.
        local_now = datetime.now().astimezone()
        next_run_at = None
        if not self.is_event_schedule(schedule):
            next_run_at = (
                next_run(schedule, after=local_now)
                .astimezone(UTC)
                .isoformat(timespec="seconds")
            )
        inherited = (
            str(self.get_workspace(workspace_id)["classification"])
            if workspace_id
            else "internal"
        )
        classification = normalize_classification(
            classification,
            default=inherited,
        )
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO automations(
                    id, workspace_id, name, prompt, schedule, enabled,
                    last_run_at, next_run_at, created_at, updated_at,
                    classification
                ) VALUES (?, ?, ?, ?, ?, 1, NULL, ?, ?, ?, ?)
                """,
                (
                    automation_id,
                    workspace_id,
                    name,
                    prompt,
                    schedule,
                    next_run_at,
                    now,
                    now,
                    classification,
                ),
            )
        self.audit(None, "automation.create", automation_id, "success", schedule)
        return self._rows("SELECT * FROM automations WHERE id = ?", (automation_id,))[0]

    def due_automations(self, at: str | None = None) -> list[dict[str, Any]]:
        at = at or utc_now()
        return self._rows(
            """
            SELECT * FROM automations
            WHERE enabled = 1 AND next_run_at IS NOT NULL AND next_run_at <= ?
            ORDER BY next_run_at LIMIT 5
            """,
            (at,),
        )

    def mark_automation_run(self, automation_id: str, schedule: str) -> None:
        now = datetime.now(UTC)
        if self.is_event_schedule(schedule):
            following = None
        else:
            try:
                local_now = datetime.now().astimezone()
                following = (
                    next_run(schedule, after=local_now)
                    .astimezone(UTC)
                    .isoformat(timespec="seconds")
                )
            except ValueError:
                following = None
        enabled = 1 if self.is_event_schedule(schedule) or following is not None else 0
        with self.transaction() as connection:
            connection.execute(
                "UPDATE automations SET last_run_at=?, next_run_at=?, enabled=?, updated_at=? WHERE id=?",
                (
                    now.isoformat(timespec="seconds"),
                    following,
                    enabled,
                    now.isoformat(timespec="seconds"),
                    automation_id,
                ),
            )

    @staticmethod
    def is_event_schedule(schedule: str) -> bool:
        normalized = schedule.casefold().strip()
        return normalized in {
            "при новом источнике",
            "при изменении контекста",
            "on new source",
        }

    def event_automations(self, workspace_id: str) -> list[dict[str, Any]]:
        rows = self._rows(
            "SELECT * FROM automations WHERE enabled=1 AND workspace_id=? AND next_run_at IS NULL",
            (workspace_id,),
        )
        return [item for item in rows if self.is_event_schedule(item["schedule"])]

    def update_automation(
        self,
        automation_id: str,
        *,
        name: str,
        prompt: str,
        schedule: str,
    ) -> None:
        local_now = datetime.now().astimezone()
        next_run_at = None
        if not self.is_event_schedule(schedule):
            next_run_at = (
                next_run(schedule, after=local_now)
                .astimezone(UTC)
                .isoformat(timespec="seconds")
            )
        with self.transaction() as connection:
            cursor = connection.execute(
                "UPDATE automations SET name=?, prompt=?, schedule=?, next_run_at=?, updated_at=? WHERE id=?",
                (name, prompt, schedule, next_run_at, utc_now(), automation_id),
            )
            if cursor.rowcount == 0:
                raise KeyError(automation_id)
        self.audit(None, "automation.update", automation_id, "success", schedule)

    def delete_automation(self, automation_id: str) -> None:
        with self.transaction() as connection:
            connection.execute("DELETE FROM automations WHERE id=?", (automation_id,))
        self.audit(None, "automation.delete", automation_id, "success")

    def set_automation_enabled(self, automation_id: str, enabled: bool) -> None:
        with self.transaction() as connection:
            connection.execute(
                "UPDATE automations SET enabled=?, updated_at=? WHERE id=?",
                (int(enabled), utc_now(), automation_id),
            )

    def set_setting(self, key: str, value: str) -> None:
        self.set_settings({key: value})

    def set_classification(
        self,
        entity_type: str,
        entity_id: str,
        classification: str,
        *,
        reason: str = "user",
    ) -> dict[str, Any]:
        """Explicitly reclassify a user-visible entity and audit only labels.

        Classification does not cascade.  Existing messages and artifact
        versions keep their historical labels, which prevents a parent card
        from silently downgrading already derived content.
        """

        tables = {
            "workspace": "workspaces",
            "task": "tasks",
            "source": "sources",
            "memory": "memory",
            "skill": "skills",
            "artifact": "artifacts",
        }
        table = tables.get(entity_type.strip().casefold())
        if table is None:
            raise ValueError("Классификацию можно менять только у рабочего пространства, задачи, источника, памяти, skill или материала")
        normalized = normalize_classification(classification)
        rows = self._rows(
            f"SELECT id, classification FROM {table} WHERE id=?",
            (entity_id,),
        )
        if not rows:
            raise KeyError(entity_id)
        previous = normalize_classification(rows[0]["classification"])
        if previous == normalized:
            return self._rows(f"SELECT * FROM {table} WHERE id=?", (entity_id,))[0]
        with self.transaction() as connection:
            cursor = connection.execute(
                f"UPDATE {table} SET classification=?, updated_at=? WHERE id=?",
                (normalized, utc_now(), entity_id),
            )
            if cursor.rowcount == 0:
                raise KeyError(entity_id)
        self.audit(
            None,
            "classification.update",
            f"{entity_type}:{entity_id}",
            "success",
            f"{previous}->{normalized}; source={reason[:40]}",
        )
        return self._rows(f"SELECT * FROM {table} WHERE id=?", (entity_id,))[0]

    def set_settings(self, values: dict[str, str]) -> None:
        """Persist a related group of non-secret settings atomically."""

        if not values:
            return
        with self.transaction() as connection:
            connection.executemany(
                "INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                list(values.items()),
            )
        for key, value in values.items():
            self.audit(None, "settings.update", key, "success", value)

    def settings(self) -> dict[str, str]:
        return {row["key"]: row["value"] for row in self._rows("SELECT * FROM settings")}

    def audit(
        self,
        task_id: str | None,
        action: str,
        target: str,
        status: str,
        detail: str = "",
        *,
        actor: str = "system",
        origin: str = "application",
    ) -> None:
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO audit_log(
                    id, task_id, action, target, status, detail,
                    actor, origin, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    new_id(),
                    task_id,
                    action,
                    target,
                    status,
                    detail,
                    actor,
                    origin,
                    utc_now(),
                ),
            )

    def snapshot(
        self,
        *,
        workspace_id: str | None = None,
        task_id: str | None = None,
        meeting_id: str | None = None,
    ) -> dict[str, Any]:
        workspace_id = workspace_id or self.default_workspace_id()
        tasks = self._rows(
            "SELECT * FROM tasks WHERE workspace_id = ? ORDER BY updated_at DESC LIMIT 100",
            (workspace_id,),
        )
        for task in tasks:
            task["plan"] = json.loads(task["plan"])
        selected_task = task_id or (tasks[0]["id"] if tasks else None)
        settings = self.settings()
        source_scope, source_scope_parameters = self._source_scope(
            "s", workspace_id, selected_task
        )
        today = {
            "active_tasks": self._rows(
                "SELECT COUNT(*) AS value FROM tasks WHERE status IN ('new', 'running', 'needs_user')"
            )[0]["value"],
            "attention": self._rows(
                "SELECT COUNT(*) AS value FROM inbox WHERE status = 'new'"
            )[0]["value"],
            "sources": self._rows(
                f"SELECT COUNT(*) AS value FROM sources s WHERE {source_scope}",
                source_scope_parameters,
            )[0]["value"],
            "artifacts": self._rows("SELECT COUNT(*) AS value FROM artifacts")[0]["value"],
            "meetings": self._rows(
                "SELECT COUNT(*) AS value FROM meetings WHERE workspace_id = ?",
                (workspace_id,),
            )[0]["value"],
            "open_meeting_items": self._rows(
                """
                SELECT COUNT(*) AS value FROM meeting_items mi
                JOIN meetings m ON m.id=mi.meeting_id
                WHERE m.workspace_id=? AND mi.status='open'
                  AND mi.kind IN ('action', 'commitment', 'risk', 'question')
                """,
                (workspace_id,),
            )[0]["value"],
        }
        meetings = self.list_meetings(workspace_id)
        meeting_status_counts = {
            row["status"]: row["value"]
            for row in self._rows(
                "SELECT status, COUNT(*) AS value FROM meetings WHERE workspace_id=? GROUP BY status",
                (workspace_id,),
            )
        }
        selected_meeting = meeting_id if meeting_id and any(
            item["id"] == meeting_id for item in meetings
        ) else None
        return {
            "workspaces": self._rows(
                "SELECT * FROM workspaces WHERE status != 'archived' ORDER BY updated_at DESC"
            ),
            "archived_workspaces": self._rows(
                "SELECT * FROM workspaces WHERE status = 'archived' ORDER BY updated_at DESC"
            ),
            "current_workspace_id": workspace_id,
            "workspace_timeline": self.workspace_timeline(workspace_id),
            "tasks": tasks,
            "current_task_id": selected_task,
            "messages": self.messages(selected_task) if selected_task else [],
            "task_events": self._rows(
                "SELECT * FROM task_events WHERE task_id = ? ORDER BY created_at DESC LIMIT 80",
                (selected_task,),
            ) if selected_task else [],
            "task_sources": self.task_sources(selected_task) if selected_task else [],
            "meetings": meetings,
            "meeting_counts": {
                "total": today["meetings"],
                "analyzed": meeting_status_counts.get("analyzed", 0),
                "error": meeting_status_counts.get("error", 0),
                "open_items": today["open_meeting_items"],
            },
            "current_meeting_id": selected_meeting,
            "meeting_items": self.meeting_items(selected_meeting) if selected_meeting else [],
            "sources": self._rows(
                """
                SELECT id, workspace_id, kind, title, path, metadata,
                       classification, created_at
                FROM sources
                WHERE workspace_id = ? AND visibility = 'workspace'
                ORDER BY updated_at DESC LIMIT 100
                """,
                (workspace_id,),
            ),
            "memory": self._rows(
                "SELECT * FROM memory WHERE workspace_id IS NULL OR workspace_id = ? ORDER BY updated_at DESC LIMIT 100",
                (workspace_id,),
            ),
            "skills": self._rows(
                "SELECT * FROM skills WHERE enabled = 1 AND (workspace_id IS NULL OR workspace_id = ?) ORDER BY builtin DESC, name",
                (workspace_id,),
            ),
            "capabilities": self._rows("SELECT * FROM capabilities ORDER BY category, name"),
            "artifacts": self._rows(
                "SELECT * FROM artifacts WHERE workspace_id = ? ORDER BY updated_at DESC LIMIT 100",
                (workspace_id,),
            ),
            "inbox": self._rows(
                "SELECT * FROM inbox WHERE workspace_id IS NULL OR workspace_id = ? ORDER BY status = 'new' DESC, priority DESC, created_at DESC LIMIT 100",
                (workspace_id,),
            ),
            "approvals": self._rows(
                "SELECT * FROM approvals ORDER BY status = 'pending' DESC, created_at DESC LIMIT 100"
            ),
            "automations": self._rows(
                "SELECT * FROM automations WHERE workspace_id IS NULL OR workspace_id = ? ORDER BY updated_at DESC",
                (workspace_id,),
            ),
            "audit": self._rows(
                "SELECT * FROM audit_log ORDER BY created_at DESC LIMIT 100"
            ),
            "settings": settings,
            "today": today,
            "model": (
                f"{settings.get('llm_model') or 'Внешняя модель'} · внешний API"
                if settings.get("model_mode") == "external"
                else "Qwen3-4B-Instruct-2507 4-bit · local MLX"
            ),
        }
