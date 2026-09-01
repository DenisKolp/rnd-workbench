"""Cross-platform rendering for a provenance-aware meeting briefing."""

from __future__ import annotations

from typing import Any


def render_meeting_briefing(
    store: Any,
    orchestrator: Any,
    meeting: dict[str, Any],
    data: dict[str, Any],
) -> str:
    lines = [f"Брифинг к следующей встрече по «{meeting['title']}».\n"]

    scope = data.get("scope")
    if isinstance(scope, dict):
        if scope.get("mode") == "express_series":
            lines.append(
                "Контекст: серия eXpress, "
                f"{scope.get('meeting_count', 1)} встреч(и)."
            )
        else:
            lines.append(
                "Контекст: только выбранная встреча — идентификатор серии "
                "eXpress не передан."
            )

    comparison = data.get("comparison")
    if isinstance(comparison, dict):
        lines.append("Изменения с прошлой встречи")
        changed = [*comparison.get("added", []), *comparison.get("changed", [])]
        if not changed and not comparison.get("removed"):
            lines.append("— Существенных изменений не найдено.")
        for item in comparison.get("added", [])[:4]:
            lines.append(f"— Добавлено: {item['text']}")
        for change in comparison.get("changed", [])[:4]:
            lines.append(
                f"— Изменено: {change['before']['text']} → "
                f"{change['after']['text']}"
            )
        if comparison.get("removed"):
            lines.append(f"— Не повторилось пунктов: {len(comparison['removed'])}.")

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
        synapse_context = orchestrator.synapse_meeting_context(
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

    topics = list(
        dict.fromkeys(
            item.get("topic") for item in meeting["items"] if item.get("topic")
        )
    )
    if topics:
        lines.append("История тем")
        for topic in topics[:5]:
            timeline = store.topic_timeline(meeting["workspace_id"], topic, limit=4)
            lines.append(f"— {topic}: {len(timeline)} связанных упоминаний")
            for item in timeline[-2:]:
                date = (item.get("occurred_at") or item["created_at"])[:10]
                lines.append(f"  {date}: {item['text']}")
    return "\n".join(lines)
