from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
import hashlib
import re
from typing import Any, Iterable, Mapping, Sequence


_PROACTIVITY_THRESHOLD = {
    "quiet": 80,
    "balanced": 50,
    "proactive": 0,
}


@dataclass(frozen=True, slots=True)
class AttentionEvent:
    key: str
    kind: str
    title: str
    detail: str
    reason: str
    score: int
    severity: str
    source_ref: str | None
    workspace_id: str | None
    owner: str | None
    due_at: str | None
    status: str
    actionable: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class AttentionEngine:
    """Deterministic local prioritisation for work that needs attention.

    The engine accepts storage-shaped dictionaries instead of depending on the
    SQLite layer.  That keeps policy testable and allows the UI backend to rank
    a snapshot without another model request.
    """

    def __init__(self, now: datetime | None = None) -> None:
        current = now or datetime.now(UTC)
        if current.tzinfo is None or current.utcoffset() is None:
            raise ValueError("AttentionEngine требует timezone-aware now")
        self.now = current

    def rank(
        self,
        *,
        meeting_items: Iterable[Mapping[str, Any]] = (),
        tasks: Iterable[Mapping[str, Any]] = (),
        inbox: Iterable[Mapping[str, Any]] = (),
        approvals: Iterable[Mapping[str, Any]] = (),
        automations: Iterable[Mapping[str, Any]] = (),
        proactivity: str = "balanced",
        workspace_id: str | None = None,
        person: str | None = None,
    ) -> list[dict[str, Any]]:
        if proactivity not in _PROACTIVITY_THRESHOLD:
            raise ValueError(f"Неизвестный уровень проактивности: {proactivity}")

        candidates: list[AttentionEvent] = []
        candidates.extend(
            self._meeting_event(item)
            for item in meeting_items
            if self._matches(item, workspace_id, person)
        )
        candidates.extend(
            self._task_event(item)
            for item in tasks
            if self._matches(item, workspace_id, person)
        )
        candidates.extend(
            self._inbox_event(item)
            for item in inbox
            if self._matches(item, workspace_id, person)
        )
        candidates.extend(
            self._approval_event(item)
            for item in approvals
            if self._matches(item, workspace_id, person)
        )
        candidates.extend(
            self._automation_event(item)
            for item in automations
            if self._matches(item, workspace_id, person)
        )

        threshold = _PROACTIVITY_THRESHOLD[proactivity]
        deduplicated: dict[str, AttentionEvent] = {}
        for event in candidates:
            if not event.title or event.score < threshold:
                continue
            previous = deduplicated.get(event.key)
            if previous is None or self._sort_key(event) < self._sort_key(previous):
                deduplicated[event.key] = event
        ordered = sorted(deduplicated.values(), key=self._sort_key)
        return [event.to_dict() for event in ordered]

    def _meeting_event(self, item: Mapping[str, Any]) -> AttentionEvent:
        kind = str(item.get("kind", ""))
        status = str(item.get("status", "open"))
        text = _text(item, "text", "title")
        owner = _optional_text(item.get("owner"))
        due = _parse_datetime(item.get("due_at"), self.now)
        score = -1
        severity = "low"
        reason = ""
        event_kind = f"meeting_{kind or 'item'}"

        if status in {"done", "closed", "resolved"}:
            return self._ignored(event_kind, item)
        if kind in {"action", "commitment"}:
            if due is not None:
                delta = due.date() - self.now.astimezone(due.tzinfo).date()
                if delta.days < 0:
                    score = min(120, 100 + abs(delta.days))
                    severity = "critical"
                    reason = f"Просрочено на {_days_phrase(abs(delta.days))}"
                elif delta.days == 0:
                    score = 88
                    severity = "high"
                    reason = "Срок сегодня"
                elif delta.days <= 3:
                    score = 72 - delta.days
                    severity = "medium"
                    reason = f"Срок через {_days_phrase(delta.days)}"
                else:
                    score = 35
                    reason = f"Открытое обязательство со сроком {_format_date(due)}"
            else:
                score = 45 if kind == "commitment" else 40
                reason = "Открытое обязательство без указанного срока"
        elif kind == "risk":
            score, severity = 76, "high"
            reason = "Незакрытый риск из встречи"
        elif kind == "question":
            score, severity = 55, "medium"
            reason = "Открытый вопрос без решения"
        elif kind == "decision" and status in {"superseded", "changed"}:
            score, severity = 68, "medium"
            reason = "Решение изменилось относительно прошлой встречи"

        return self._event(
            item,
            kind=event_kind,
            title=text,
            detail=_text(item, "source_quote", "topic"),
            reason=reason,
            score=score,
            severity=severity,
            owner=owner,
            due=due,
            status=status,
            actionable=kind in {"action", "commitment", "risk", "question"},
        )

    def _task_event(self, item: Mapping[str, Any]) -> AttentionEvent:
        status = str(item.get("status", ""))
        title = _text(item, "title", "result")
        score, severity, reason = -1, "low", ""
        if status == "error":
            score, severity, reason = 96, "critical", "Задача завершилась ошибкой"
        elif status == "needs_user":
            score, severity, reason = 82, "high", "Задача ожидает решения пользователя"
        elif status == "done" and _is_recent(item.get("updated_at"), self.now, hours=24):
            score, reason = 30, "Появился новый готовый результат"
        return self._event(
            item,
            kind="task",
            title=title,
            detail=_text(item, "result"),
            reason=reason,
            score=score,
            severity=severity,
            status=status,
            actionable=status in {"error", "needs_user"},
        )

    def _inbox_event(self, item: Mapping[str, Any]) -> AttentionEvent:
        status = str(item.get("status", "new"))
        raw_priority = _integer(item.get("priority"), default=0)
        kind = str(item.get("kind", "inbox"))
        score = 35 + max(0, raw_priority) * 10 if status == "new" else -1
        severity = "high" if kind in {"error", "overdue"} else "medium" if score >= 55 else "low"
        reason = _text(item, "reason") or (
            "Ошибка требует внимания" if kind == "error" else "Новое событие во входящих"
        )
        return self._event(
            item,
            kind=f"inbox_{kind}",
            title=_text(item, "title"),
            detail=_text(item, "detail"),
            reason=reason,
            score=score,
            severity=severity,
            status=status,
            actionable=status == "new",
        )

    def _approval_event(self, item: Mapping[str, Any]) -> AttentionEvent:
        status = str(item.get("status", "pending"))
        pending = status == "pending"
        return self._event(
            item,
            kind="approval",
            title=_text(item, "title", "action_type"),
            detail=_text(item, "detail", "payload"),
            reason="Требуется подтверждение внешнего действия" if pending else "",
            score=90 if pending else -1,
            severity="high",
            status=status,
            actionable=pending,
        )

    def _automation_event(self, item: Mapping[str, Any]) -> AttentionEvent:
        status = str(item.get("last_status", item.get("status", "")))
        failed = status in {"error", "failed"}
        return self._event(
            item,
            kind="automation",
            title=_text(item, "name", "title"),
            detail=_text(item, "detail", "last_error"),
            reason="Последний запуск автоматизации завершился ошибкой" if failed else "",
            score=94 if failed else -1,
            severity="critical",
            status=status,
            actionable=failed,
        )

    def _event(
        self,
        item: Mapping[str, Any],
        *,
        kind: str,
        title: str,
        detail: str,
        reason: str,
        score: int,
        severity: str,
        status: str,
        actionable: bool,
        owner: str | None = None,
        due: datetime | None = None,
    ) -> AttentionEvent:
        source_ref = _optional_text(
            item.get("source_ref") or item.get("source_id") or item.get("id")
        )
        workspace_id = _optional_text(item.get("workspace_id"))
        key = _stable_key(kind, item, title, source_ref)
        return AttentionEvent(
            key=key,
            kind=kind,
            title=title.strip(),
            detail=detail.strip(),
            reason=reason,
            score=score,
            severity=severity,
            source_ref=source_ref,
            workspace_id=workspace_id,
            owner=owner,
            due_at=due.isoformat() if due is not None else None,
            status=status,
            actionable=actionable,
        )

    def _ignored(self, kind: str, item: Mapping[str, Any]) -> AttentionEvent:
        return self._event(
            item,
            kind=kind,
            title=_text(item, "text", "title"),
            detail="",
            reason="",
            score=-1,
            severity="low",
            status=str(item.get("status", "")),
            actionable=False,
        )

    @staticmethod
    def _matches(
        item: Mapping[str, Any],
        workspace_id: str | None,
        person: str | None,
    ) -> bool:
        if workspace_id and item.get("workspace_id") not in {None, workspace_id}:
            return False
        if person:
            haystack = " ".join(
                str(item.get(key, ""))
                for key in ("owner", "participants", "text", "title", "detail")
            ).casefold()
            if person.casefold() not in haystack:
                return False
        return True

    @staticmethod
    def _sort_key(event: AttentionEvent) -> tuple[int, str, str]:
        return (-event.score, event.due_at or "9999", event.key)


def render_attention(events: Sequence[Mapping[str, Any]], limit: int = 10) -> str:
    visible = list(events[: max(0, limit)])
    if not visible:
        return "Сейчас нет событий, требующих вашего внимания."
    lines = ["Требует внимания:"]
    for index, event in enumerate(visible, start=1):
        title = _text(event, "title") or "Без названия"
        reason = _text(event, "reason") or "Причина не указана"
        owner = _optional_text(event.get("owner"))
        suffix = f" Исполнитель: {owner}." if owner else ""
        lines.append(f"{index}. {title} — {reason}.{suffix}".replace("..", "."))
    return "\n".join(lines)


def _stable_key(
    kind: str,
    item: Mapping[str, Any],
    title: str,
    source_ref: str | None,
) -> str:
    explicit_id = _optional_text(item.get("id"))
    if explicit_id:
        return f"{kind}:{explicit_id}"
    normalized = re.sub(r"\W+", " ", title.casefold()).strip()
    raw = "|".join((kind, source_ref or "", normalized, str(item.get("due_at", ""))))
    return f"{kind}:{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:20]}"


def _parse_datetime(value: Any, now: datetime) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, datetime.min.time(), tzinfo=now.tzinfo)
    else:
        text = str(value).strip()
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            try:
                parsed = datetime.combine(date.fromisoformat(text), datetime.min.time())
            except ValueError:
                return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=now.tzinfo)
    return parsed


def _is_recent(value: Any, now: datetime, *, hours: int) -> bool:
    parsed = _parse_datetime(value, now)
    if parsed is None:
        return False
    delta = now.astimezone(UTC) - parsed.astimezone(UTC)
    return timedelta(0) <= delta <= timedelta(hours=hours)


def _days_phrase(days: int) -> str:
    ending = "день" if days % 10 == 1 and days % 100 != 11 else "дня" if days % 10 in {2, 3, 4} and days % 100 not in {12, 13, 14} else "дней"
    return f"{days} {ending}"


def _format_date(value: datetime) -> str:
    return value.strftime("%d.%m.%Y")


def _text(item: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        value = item.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _integer(value: Any, *, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
