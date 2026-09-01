from __future__ import annotations

from datetime import UTC, datetime, timedelta
import re
from typing import Any

from .store import AssistantStore, highest_classification, normalize_classification


_PLAN_PREFIX = r"(?:пожалуйста[,.]?\s+)?"
_DIGEST_TITLES = {
    "morning": "Утренний дайджест",
    "evening": "Вечерний дайджест",
    "weekly": "Итоги недели",
}
_DIGEST_SECTION_ORDER = ("tasks", "meeting_items", "inbox", "artifacts")
_DIGEST_SECTION_TITLES = {
    "tasks": "задачи",
    "meeting_items": "встречи",
    "inbox": "уведомления",
    "artifacts": "материалы",
}
_DIGEST_KIND_TITLES = {
    "decision": "решения",
    "action": "поручения",
    "commitment": "обязательства",
    "risk": "риски",
    "question": "вопросы",
    "topic": "темы",
}
_MEETING_LABELS = {
    "decision": "Решение",
    "action": "Поручение",
    "commitment": "Обязательство",
    "risk": "Риск",
    "question": "Вопрос",
    "topic": "Тема",
}


def _clean_plan_step(value: str) -> str:
    step = value.strip().strip("—–-: \t\r\n")
    if len(step) >= 2 and (step[0], step[-1]) in {
        ("«", "»"),
        ('"', '"'),
    }:
        step = step[1:-1].strip()
    if not step or not re.search(r"\w", step, flags=re.UNICODE):
        raise ValueError("Текст шага не может быть пустым")
    if len(step) > 500:
        raise ValueError("Шаг плана не может быть длиннее 500 символов")
    return step


def parse_task_plan_command(text: str) -> dict[str, Any] | None:
    """Parse a bounded Russian grammar without asking an LLM to mutate state."""

    normalized = re.sub(r"\s+", " ", text.strip())
    if not normalized:
        return None
    add = re.fullmatch(
        _PLAN_PREFIX
        + r"(?:добавь|добавить)\s+(?:(?:новый\s+)?шаг\s+)?"
        r"в\s+план\s*(?:[:—–-]\s*)?(.+)",
        normalized,
        flags=re.IGNORECASE,
    )
    if add:
        return {"action": "add", "step": _clean_plan_step(add.group(1))}

    replace = re.fullmatch(
        _PLAN_PREFIX
        + r"(?:замени|заменить)\s+(?:в\s+плане\s+)?"
        r"(?:шаг\s*№?\s*(\d+)|(\d+)(?:-й|-ый)?\s+шаг)\s+"
        r"на\s+(.+)",
        normalized,
        flags=re.IGNORECASE,
    )
    if replace:
        return {
            "action": "replace",
            "index": int(replace.group(1) or replace.group(2)),
            "step": _clean_plan_step(replace.group(3)),
        }

    delete = re.fullmatch(
        _PLAN_PREFIX
        + r"(?:удали|удалить)\s+(?:из\s+плана\s+)?"
        r"(?:шаг\s*№?\s*(\d+)|(\d+)(?:-й|-ый)?\s+шаг)\s*[.!]?",
        normalized,
        flags=re.IGNORECASE,
    )
    if delete:
        return {
            "action": "delete",
            "index": int(delete.group(1) or delete.group(2)),
        }

    looks_like_command = re.match(
        _PLAN_PREFIX + r"(?:добавь|добавить|замени|заменить|удали|удалить)\b",
        normalized,
        flags=re.IGNORECASE,
    ) and re.search(r"\b(?:план|шаг)\w*\b", normalized, flags=re.IGNORECASE)
    if looks_like_command:
        raise ValueError(
            "Не удалось изменить план. Используйте: «добавь в план …», "
            "«замени шаг 2 на …» или «удали шаг 2»"
        )
    return None


def mutate_task_plan(
    store: AssistantStore,
    text: str,
    task_id: str,
) -> dict[str, Any] | None:
    """Apply one plan mutation, preserving the current task status and result."""

    command = parse_task_plan_command(text)
    if command is None:
        return None
    task = store.get_task(task_id)
    plan = list(task["plan"])
    action = str(command["action"])
    old_step: str | None = None
    if action == "add":
        plan.append(str(command["step"]))
        index = len(plan)
        title = "Шаг добавлен в план"
        detail = f"Шаг {index}: {command['step']}"
        acknowledgement = f"Добавил шаг {index}: «{command['step']}»."
    else:
        index = int(command["index"])
        if index < 1 or index > len(plan):
            raise ValueError(f"В плане {len(plan)} шагов; шаг {index} не найден")
        old_step = plan[index - 1]
        if action == "replace":
            plan[index - 1] = str(command["step"])
            title = "Шаг плана заменён"
            detail = f"Шаг {index}: {old_step} → {command['step']}"
            acknowledgement = (
                f"Заменил шаг {index}: «{old_step}» → «{command['step']}»."
            )
        else:
            if len(plan) == 1:
                raise ValueError("Нельзя удалить единственный шаг плана")
            del plan[index - 1]
            title = "Шаг удалён из плана"
            detail = f"Шаг {index}: {old_step}"
            acknowledgement = f"Удалил шаг {index}: «{old_step}»."

    store.update_task(task_id, plan=plan)
    store.add_task_event(task_id, "plan", title, detail)
    store.audit(
        task_id,
        f"task.plan.{action}",
        task_id,
        "success",
        f"step={index}",
    )
    return {
        "task_id": task_id,
        "action": action,
        "index": index,
        "old_step": old_step,
        "new_step": command.get("step"),
        "plan": plan,
        "message": acknowledgement,
    }


def normalize_digest_period(value: str) -> str:
    normalized = re.sub(r"\s+", " ", value.casefold().strip())
    aliases = {
        "morning": {"morning", "утро", "утренний", "утренний дайджест"},
        "evening": {"evening", "вечер", "вечерний", "вечерний дайджест"},
        "weekly": {
            "weekly",
            "week",
            "неделя",
            "недельный",
            "weekly review",
            "итоги недели",
        },
    }
    for period, values in aliases.items():
        if normalized in values:
            return period
    raise ValueError("Тип дайджеста должен быть morning, evening или weekly")


def parse_digest_command(
    text: str,
    *,
    now: datetime | None = None,
) -> str | None:
    request = parse_digest_request(text, now=now)
    return None if request is None else str(request["period"])


def parse_digest_request(
    text: str,
    *,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Parse a local digest request with an optional transparent focus.

    The supported user form is deliberately small and inspectable, for example
    ``/digest неделя только риски и поручения``.  The returned section and
    meeting-kind filters are persisted with the artifact, so a scheduled digest
    can be reproduced without asking an LLM to reinterpret its scope.
    """

    match = re.fullmatch(r"\s*/digest(?:\s+(.+?))?\s*", text, flags=re.IGNORECASE)
    if not match:
        return None
    argument = re.sub(r"\s+", " ", (match.group(1) or "").casefold().strip())
    focus = ""
    focus_match = re.search(r"\bтолько\b\s+(.+)$", argument)
    if focus_match:
        focus = focus_match.group(1).strip(" .,:;—–-")
        argument = argument[: focus_match.start()].strip(" .,:;—–-")

    if argument:
        if re.search(r"\b(?:недел\w*|weekly|week)\b", argument):
            period = "weekly"
        elif re.search(r"\b(?:вечер\w*|итоги\s+дня|evening)\b", argument):
            period = "evening"
        elif re.search(r"\b(?:утр\w*|начало\s+дня|morning)\b", argument):
            period = "morning"
        elif re.search(r"\b(?:дайджест\w*|сводк\w*|обнов\w*)\b", argument):
            local_now = (now or datetime.now().astimezone()).astimezone()
            period = "morning" if local_now.hour < 15 else "evening"
        else:
            raise ValueError("После /digest укажите: утро, вечер или неделя")
    else:
        local_now = (now or datetime.now().astimezone()).astimezone()
        period = "morning" if local_now.hour < 15 else "evening"

    sections = list(_DIGEST_SECTION_ORDER)
    meeting_kinds: list[str] = []
    focus_label = ""
    if focus:
        selected_sections: set[str] = set()
        if re.search(r"\bзадач\w*\b", focus):
            selected_sections.add("tasks")
        if re.search(r"\b(?:уведомлен\w*|входящ\w*)\b", focus):
            selected_sections.add("inbox")
        if re.search(r"\b(?:материал\w*|артефакт\w*|документ\w*)\b", focus):
            selected_sections.add("artifacts")
        if re.search(r"\b(?:встреч\w*|решени\w*|поручени\w*|обязательств\w*|риск\w*|вопрос\w*|тем\w*)\b", focus):
            selected_sections.add("meeting_items")
        kind_patterns = {
            "decision": r"\bрешени\w*\b",
            "action": r"\bпоручени\w*\b",
            "commitment": r"\bобязательств\w*\b",
            "risk": r"\bриск\w*\b",
            "question": r"\bвопрос\w*\b",
            "topic": r"\bтем\w*\b",
        }
        meeting_kinds = [
            kind for kind, pattern in kind_patterns.items() if re.search(pattern, focus)
        ]
        if not selected_sections:
            raise ValueError(
                "После «только» укажите: задачи, встречи, риски, поручения, "
                "уведомления или материалы"
            )
        sections = [
            section for section in _DIGEST_SECTION_ORDER if section in selected_sections
        ]
        labels = (
            [_DIGEST_KIND_TITLES[kind] for kind in meeting_kinds]
            if selected_sections == {"meeting_items"} and meeting_kinds
            else [_DIGEST_SECTION_TITLES[section] for section in sections]
        )
        focus_label = " и ".join(labels)

    return {
        "period": period,
        "sections": sections,
        "meeting_kinds": meeting_kinds,
        "focus_label": focus_label,
    }


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def build_digest(
    store: AssistantStore,
    workspace_id: str,
    period: str,
    *,
    sections: list[str] | tuple[str, ...] | None = None,
    meeting_kinds: list[str] | tuple[str, ...] | None = None,
    focus_label: str = "",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build a deterministic digest exclusively from local structured rows."""

    period = normalize_digest_period(period)
    selected_sections = tuple(
        section
        for section in _DIGEST_SECTION_ORDER
        if section in set(sections or _DIGEST_SECTION_ORDER)
    )
    if not selected_sections:
        raise ValueError("Дайджест должен включать хотя бы один раздел")
    selected_meeting_kinds = tuple(
        kind for kind in _MEETING_LABELS if kind in set(meeting_kinds or ())
    )
    generated_at = now or datetime.now(UTC)
    if generated_at.tzinfo is None:
        generated_at = generated_at.replace(tzinfo=UTC)
    generated_at = generated_at.astimezone(UTC)
    local_day = generated_at.astimezone().date()
    weekly_start = generated_at - timedelta(days=7)
    workspace = store.get_workspace(workspace_id)

    tasks = store._rows(
        """
        SELECT id, title, status, updated_at, classification
        FROM tasks WHERE workspace_id=? ORDER BY updated_at DESC LIMIT 100
        """,
        (workspace_id,),
    )
    meeting_items = store.list_meeting_items(workspace_id)
    source_ids = {
        str(item["source_id"])
        for item in meeting_items
        if item.get("source_id")
    }
    source_classifications = {
        source_id: str(store.get_source(source_id)["classification"])
        for source_id in source_ids
    }
    inbox = store._rows(
        """
        SELECT * FROM inbox
        WHERE workspace_id IS NULL OR workspace_id=?
        ORDER BY status='new' DESC, priority DESC, created_at DESC LIMIT 100
        """,
        (workspace_id,),
    )
    artifacts = store._rows(
        """
        SELECT id, title, kind, path, current_version, updated_at, classification
        FROM artifacts WHERE workspace_id=? ORDER BY updated_at DESC LIMIT 100
        """,
        (workspace_id,),
    )

    def is_today(value: Any) -> bool:
        parsed = _parse_datetime(value)
        return bool(parsed and parsed.astimezone().date() == local_day)

    def is_week(value: Any) -> bool:
        parsed = _parse_datetime(value)
        return bool(parsed and parsed.astimezone(UTC) >= weekly_start)

    if period == "morning":
        selected_tasks = [
            item
            for item in tasks
            if item["status"] in {"new", "running", "needs_user", "error"}
        ][:8]
        selected_meetings = [
            item
            for item in meeting_items
            if item["status"] == "open"
            and item["kind"] in {"action", "commitment", "risk", "question"}
        ][:8]
        selected_inbox = [item for item in inbox if item["status"] == "new"][:8]
        selected_artifacts = artifacts[:5]
    elif period == "evening":
        selected_tasks = [item for item in tasks if is_today(item["updated_at"])][:8]
        selected_meetings = [
            item for item in meeting_items if is_today(item["updated_at"])
        ][:8]
        selected_inbox = [item for item in inbox if is_today(item["created_at"])][:8]
        selected_artifacts = [
            item for item in artifacts if is_today(item["updated_at"])
        ][:5]
    else:
        selected_tasks = [item for item in tasks if is_week(item["updated_at"])][:12]
        selected_meetings = [
            item for item in meeting_items if is_week(item["updated_at"])
        ][:12]
        selected_inbox = [item for item in inbox if is_week(item["created_at"])][:12]
        selected_artifacts = [
            item for item in artifacts if is_week(item["updated_at"])
        ][:8]

    if selected_meeting_kinds:
        selected_meetings = [
            item for item in selected_meetings if item["kind"] in selected_meeting_kinds
        ]

    references: list[dict[str, Any]] = []
    classifications = [str(workspace["classification"])]

    def referenced_item(
        *,
        reference_type: str,
        entity_id: str,
        title: str,
        detail: str,
        classification: str,
        **reference_detail: Any,
    ) -> dict[str, Any]:
        label = f"D{len(references) + 1}"
        classification = normalize_classification(classification)
        references.append(
            {
                "label": label,
                "type": reference_type,
                "id": entity_id,
                "title": title,
                "classification": classification,
                **reference_detail,
            }
        )
        classifications.append(classification)
        return {"reference": label, "title": title, "detail": detail}

    task_items = [
        referenced_item(
            reference_type="task",
            entity_id=str(item["id"]),
            title=str(item["title"]),
            detail=f"Статус: {item['status']}; обновлено {str(item['updated_at'])[:16]}",
            classification=str(item["classification"]),
            status=str(item["status"]),
            updated_at=str(item["updated_at"]),
        )
        for item in selected_tasks
    ] if "tasks" in selected_sections else []
    meeting_result_items = []
    for item in (
        selected_meetings if "meeting_items" in selected_sections else []
    ):
        detail_parts = [str(item["text"])]
        if item.get("owner"):
            detail_parts.append(f"Исполнитель: {item['owner']}")
        if item.get("due_at"):
            detail_parts.append(f"Срок: {item['due_at']}")
        source_id = str(item["source_id"])
        meeting_result_items.append(
            referenced_item(
                reference_type="meeting_item",
                entity_id=str(item["id"]),
                title=(
                    f"{_MEETING_LABELS.get(str(item['kind']), str(item['kind']))}: "
                    f"{item['meeting_title']}"
                ),
                detail=" · ".join(detail_parts),
                classification=source_classifications.get(
                    source_id,
                    str(workspace["classification"]),
                ),
                meeting_id=str(item["meeting_id"]),
                source_id=source_id,
                source_path=item.get("source_path"),
                char_start=int(item["source_start"]),
                char_end=int(item["source_end"]),
                status=str(item["status"]),
                due_at=item.get("due_at"),
            )
        )
    inbox_items = [
        referenced_item(
            reference_type="inbox",
            entity_id=str(item["id"]),
            title=str(item["title"]),
            detail=str(item["detail"]),
            classification=str(workspace["classification"]),
            source_ref=item.get("source_ref"),
            status=str(item["status"]),
            priority=int(item["priority"]),
            created_at=str(item["created_at"]),
        )
        for item in selected_inbox
    ] if "inbox" in selected_sections else []
    artifact_items = [
        referenced_item(
            reference_type="artifact",
            entity_id=str(item["id"]),
            title=str(item["title"]),
            detail=(
                f"{item['kind']}; версия {item['current_version']}; "
                f"обновлено {str(item['updated_at'])[:16]}"
            ),
            classification=str(item["classification"]),
            path=str(item["path"]),
            version=int(item["current_version"]),
            updated_at=str(item["updated_at"]),
        )
        for item in selected_artifacts
    ] if "artifacts" in selected_sections else []
    digest_sections = [
        {"id": "tasks", "title": "Задачи", "items": task_items},
        {
            "id": "meeting_items",
            "title": "Решения и обязательства встреч",
            "items": meeting_result_items,
        },
        {"id": "inbox", "title": "Уведомления", "items": inbox_items},
        {"id": "artifacts", "title": "Материалы", "items": artifact_items},
    ]
    digest_sections = [
        section for section in digest_sections if section["id"] in selected_sections
    ]
    normalized_focus_label = re.sub(r"\s+", " ", focus_label.strip())[:120]
    digest = {
        "period": period,
        "title": (
            f"{_DIGEST_TITLES[period]} · {normalized_focus_label}"
            if normalized_focus_label
            else _DIGEST_TITLES[period]
        ),
        "configuration": {
            "sections": list(selected_sections),
            "meeting_kinds": list(selected_meeting_kinds),
            "focus_label": normalized_focus_label,
        },
        "workspace": {"id": workspace_id, "name": workspace["name"]},
        "generated_at": generated_at.isoformat(timespec="seconds"),
        "local_only": True,
        "scope_note": (
            "Сводка построена только по локальным данным RnD Workbench. "
            "Почта, календарь, Синапс и другие корпоративные системы не подключены."
        ),
        "classification": highest_classification(classifications),
        "sections": digest_sections,
        "references": references,
        "counts": {
            section["id"]: len(section["items"]) for section in digest_sections
        },
        "empty": not references,
    }
    digest["text"] = render_digest(digest)
    return digest


def render_digest(digest: dict[str, Any]) -> str:
    lines = [
        f"# {digest['title']}",
        "",
        f"Рабочее пространство: {digest['workspace']['name']}",
        f"Сформировано: {digest['generated_at']}",
        "",
        f"> {digest['scope_note']}",
    ]
    for section in digest["sections"]:
        lines.extend(["", f"## {section['title']}"])
        if not section["items"]:
            lines.append("— Нет локальных данных за выбранный период.")
            continue
        for item in section["items"]:
            lines.append(
                f"- **{item['title']}** — {item['detail']} [{item['reference']}]"
            )
    lines.extend(["", "## Ссылки"])
    if not digest["references"]:
        lines.append("— Нет ссылок: локальные записи за период отсутствуют.")
    for reference in digest["references"]:
        location = ""
        if reference["type"] == "meeting_item":
            location = (
                f"; источник {reference['source_id']}; "
                f"символы {reference['char_start']}:{reference['char_end']}"
            )
        lines.append(
            f"- [{reference['label']}] {reference['type']}: "
            f"{reference['title']} (id: {reference['id']}{location})"
        )
    return "\n".join(lines)


def persist_digest(
    store: AssistantStore,
    workspace_id: str,
    period: str,
    *,
    sections: list[str] | tuple[str, ...] | None = None,
    meeting_kinds: list[str] | tuple[str, ...] | None = None,
    focus_label: str = "",
    request_text: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Persist a local digest as a task, assistant message and artifact."""

    digest = build_digest(
        store,
        workspace_id,
        period,
        sections=sections,
        meeting_kinds=meeting_kinds,
        focus_label=focus_label,
        now=now,
    )
    task = store.create_task(
        workspace_id,
        digest["title"],
        ["Собрать локальные записи", "Сгруппировать изменения", "Сохранить дайджест"],
        classification=str(digest["classification"]),
    )
    request_text = request_text or f"/digest {digest['period']}"
    store.add_message(
        task["id"],
        "user",
        request_text,
        classification=str(digest["classification"]),
    )
    store.update_task(task["id"], status="running", skill_id="digest")
    store.add_task_event(task["id"], "status", "Выполняется локально")

    source_references: list[dict[str, Any]] = []
    seen_sources: set[str] = set()
    for reference in digest["references"]:
        source_id = reference.get("source_id")
        if not source_id or source_id in seen_sources:
            continue
        source = store.get_source(str(source_id))
        source_references.append(
            {
                "id": str(source_id),
                "title": str(source["title"]),
                "kind": str(source["kind"]),
                "path": source.get("path"),
                "char_start": reference.get("char_start"),
                "char_end": reference.get("char_end"),
                "selection": "digest",
            }
        )
        seen_sources.add(str(source_id))
    store.add_message(
        task["id"],
        "assistant",
        str(digest["text"]),
        metadata={
            "deterministic": True,
            "digest": {
                "period": digest["period"],
                "configuration": digest["configuration"],
                "counts": digest["counts"],
                "references": digest["references"],
            },
            "llm_route": {
                "requested_route": "local",
                "actual_route": "local_command",
                "provider_type": "local",
                "policy_engine": "deterministic",
                "content_transmitted": False,
            },
            "sources": source_references,
        },
        classification=str(digest["classification"]),
    )
    store.update_task(task["id"], status="done", result=str(digest["text"]))
    store.add_task_event(
        task["id"],
        "digest",
        f"Сформирован: {digest['title']}",
        ", ".join(
            f"{section_id}={count}"
            for section_id, count in digest["counts"].items()
        ),
    )
    store.add_task_event(task["id"], "completed", "Задача завершена")
    artifact = store.create_artifact(
        workspace_id,
        task["id"],
        digest["title"],
        str(digest["text"]),
        source_refs=source_references,
        metadata={
            "origin": "structured_digest",
            "period": digest["period"],
            "configuration": digest["configuration"],
            "local_only": True,
            "references": digest["references"],
        },
        classification=str(digest["classification"]),
    )
    store.add_inbox(
        workspace_id,
        "artifact",
        f"Готов результат: {digest['title']}",
        "Локальный структурированный дайджест сохранён в материалах.",
        priority=2,
        source_ref=artifact["id"],
    )
    store.audit(
        task["id"],
        "digest.generate",
        artifact["id"],
        "success",
        f"period={digest['period']}; refs={len(digest['references'])}",
    )
    return {
        **digest,
        "task": store.get_task(task["id"]),
        "artifact": artifact,
        "source_references": source_references,
    }
