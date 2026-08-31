"""Deterministic, offline extraction of structured facts from Russian meetings."""

from __future__ import annotations

from datetime import date, datetime, timedelta
import re
from typing import Any


_RESERVED_LABELS = {
    "тема",
    "решение",
    "решили",
    "поручение",
    "задача",
    "риск",
    "вопрос",
    "итог",
    "повестка",
    "обсудим",
    "обсуждаем",
    "участники",
    "присутствовали",
}

_CUES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "decision",
        (
            r"\b(?:решил[иа]?|решено|принято решение|договорил(?:ись|ся)|утвердил[иа]?|согласовал[иа]?)\b",
            r"^\s*(?:решение|итог)\s*[:—-]",
        ),
    ),
    (
        "commitment",
        (
            r"\b(?:обещаю|обещал[аи]?|обязуюсь|беру на себя|возьм[уе] на себя|мы сделаем|я сделаю)\b",
        ),
    ),
    (
        "risk",
        (
            r"\b(?:риск|рискуем|проблема|блокер|угроза|опасени[ея]|может сорвать|не успе(?:ем|ть))\b",
            r"^\s*риск\s*[:—-]",
        ),
    ),
    (
        "question",
        (
            r"\?\s*$",
            r"\b(?:открытый вопрос|нужно выяснить|надо уточнить|вопрос оста[её]тся)\b",
            r"^\s*вопрос\s*[:—-]",
        ),
    ),
    (
        "action",
        (
            r"\b(?:поручил[аи]?|поручить|задача|нужно|необходимо|следует)\b",
            r"\b[А-ЯЁA-Z][а-яёa-z-]+\s+(?:подготовит|сделает|проверит|отправит|согласует|создаст|обновит|провед[её]т|предоставит|закроет)\b",
            r"^\s*(?:поручение|действие)\s*[:—-]",
        ),
    ),
    (
        "topic",
        (
            r"^\s*(?:тема|повестка|обсуждаем|обсудим)\s*[:—-]",
        ),
    ),
)

_MONTHS = {
    "января": 1,
    "февраля": 2,
    "марта": 3,
    "апреля": 4,
    "мая": 5,
    "июня": 6,
    "июля": 7,
    "августа": 8,
    "сентября": 9,
    "октября": 10,
    "ноября": 11,
    "декабря": 12,
}

_WEEKDAYS = {
    "понедельник": 0,
    "понедельника": 0,
    "понедельнику": 0,
    "понедельнике": 0,
    "вторник": 1,
    "вторника": 1,
    "вторнику": 1,
    "вторнике": 1,
    "среда": 2,
    "среды": 2,
    "среду": 2,
    "среде": 2,
    "четверг": 3,
    "четверга": 3,
    "четвергу": 3,
    "четверге": 3,
    "пятница": 4,
    "пятницы": 4,
    "пятницу": 4,
    "пятнице": 4,
    "суббота": 5,
    "субботы": 5,
    "субботу": 5,
    "субботе": 5,
    "воскресенье": 6,
    "воскресенья": 6,
    "воскресенью": 6,
}


def _base_date(occurred_at: str | None) -> date | None:
    if not occurred_at:
        return None
    try:
        return datetime.fromisoformat(occurred_at.replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return date.fromisoformat(occurred_at[:10])
        except ValueError:
            return None


def parse_due_date(text: str, occurred_at: str | None) -> str | None:
    """Resolve explicit and relative Russian deadlines to an ISO calendar date."""
    match = re.search(r"\b(20\d{2})-(\d{2})-(\d{2})\b", text)
    if match:
        try:
            return date(*(int(value) for value in match.groups())).isoformat()
        except ValueError:
            pass

    match = re.search(r"\b(\d{1,2})[./](\d{1,2})[./](20\d{2})\b", text)
    if match:
        day, month, year = (int(value) for value in match.groups())
        try:
            return date(year, month, day).isoformat()
        except ValueError:
            pass

    base = _base_date(occurred_at)
    match = re.search(
        r"\b(?:до|к|не позднее)\s+(\d{1,2})\s+(" + "|".join(_MONTHS) + r")(?:\s+(20\d{2}))?\b",
        text.casefold(),
    )
    if match:
        day = int(match.group(1))
        month = _MONTHS[match.group(2)]
        year = int(match.group(3)) if match.group(3) else (base.year if base else date.today().year)
        try:
            candidate = date(year, month, day)
            if base and not match.group(3) and candidate < base:
                candidate = date(year + 1, month, day)
            return candidate.isoformat()
        except ValueError:
            pass

    if not base:
        return None
    lowered = text.casefold()
    if re.search(r"\bпослезавтра\b", lowered):
        return (base + timedelta(days=2)).isoformat()
    if re.search(r"\bзавтра\b", lowered):
        return (base + timedelta(days=1)).isoformat()
    match = re.search(r"\bчерез\s+(\d+)\s+(?:день|дня|дней)\b", lowered)
    if match:
        return (base + timedelta(days=int(match.group(1)))).isoformat()
    match = re.search(
        r"\b(?:до|к|в|на)\s+(?:следующ(?:ий|ую)\s+)?(" + "|".join(_WEEKDAYS) + r")\b",
        lowered,
    )
    if match:
        target = _WEEKDAYS[match.group(1)]
        days = (target - base.weekday()) % 7
        if days == 0 or "следующ" in match.group(0):
            days += 7
        return (base + timedelta(days=days)).isoformat()
    return None


def _classify(text: str) -> tuple[str | None, float]:
    lowered = text.casefold()
    for kind, patterns in _CUES:
        if any(re.search(pattern, lowered, flags=re.IGNORECASE) for pattern in patterns):
            confidence = 0.96 if re.match(r"\s*(?:тема|решение|поручение|риск|вопрос)\s*[:—-]", lowered) else 0.88
            return kind, confidence
    return None, 0.0


def _clean_text(text: str) -> str:
    cleaned = re.sub(r"^\s*[-–—*•]\s*", "", text)
    cleaned = re.sub(
        r"^\s*(?:тема|повестка|решение|итог|поручение|действие|риск|вопрос)\s*[:—-]\s*",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    return cleaned.strip()


def _extract_owner(text: str, speaker: str | None, kind: str) -> str | None:
    patterns = (
        r"\b(?:ответственн(?:ый|ая)|исполнитель|владелец)\s*[:—-]\s*([А-ЯЁA-Z][А-Яа-яЁёA-Za-z-]+(?:\s+[А-ЯЁA-Z][А-Яа-яЁёA-Za-z-]+)?)",
        r"\b([А-ЯЁA-Z][а-яёa-z-]+(?:\s+[А-ЯЁA-Z][а-яёa-z-]+)?)\s+(?:отвечает|подготовит|сделает|проверит|отправит|согласует|создаст|обновит|провед[её]т|предоставит|закроет)\b",
        r"\bпоруч(?:ить|или|ено)\s+([А-ЯЁA-Z][а-яёa-z-]+(?:\s+[А-ЯЁA-Z][а-яёa-z-]+)?)",
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            owner = match.group(1).strip(" ,.—-")
            if owner.casefold() not in {"кто", "что", "когда", "как"}:
                return owner
    if speaker and kind in {"action", "commitment"} and re.search(
        r"\b(?:я|сделаю|подготовлю|проверю|отправлю|обещаю|беру на себя)\b",
        text.casefold(),
    ):
        return speaker
    return None


def _line_body(line: str, absolute_start: int) -> tuple[str | None, str, int]:
    """Return speaker, spoken body, and body's absolute character offset."""
    patterns = (
        re.compile(
            r"^\s*(?:\[(?:\d{1,2}:)?\d{1,2}:\d{2}\]|(?:\d{1,2}:)?\d{1,2}:\d{2})?\s*"
            r"(?P<speaker>[А-ЯЁA-Z][\wЁёА-Яа-я .'-]{0,48}?)\s*:\s*(?P<body>.*)$"
        ),
        re.compile(
            r"^\s*(?P<speaker>[А-ЯЁA-Z][\wЁёА-Яа-я .'-]{0,48}?)\s*"
            r"\[(?:\d{1,2}:)?\d{1,2}:\d{2}\]\s*:\s*(?P<body>.*)$"
        ),
    )
    for pattern in patterns:
        match = pattern.match(line)
        if match and match.group("speaker").strip().casefold() not in _RESERVED_LABELS:
            return (
                match.group("speaker").strip(),
                match.group("body"),
                absolute_start + match.start("body"),
            )
    leading = len(line) - len(line.lstrip())
    return None, line.strip(), absolute_start + leading


def _segments(body: str, body_start: int) -> list[tuple[str, int, int]]:
    segments: list[tuple[str, int, int]] = []
    for match in re.finditer(r"[^.!?;]+(?:[.!?;]+|$)", body):
        raw = match.group(0)
        left = len(raw) - len(raw.lstrip())
        right = len(raw.rstrip())
        if right <= left:
            continue
        start = body_start + match.start() + left
        end = body_start + match.start() + right
        segments.append((raw[left:right], start, end))
    if not segments and body.strip():
        left = len(body) - len(body.lstrip())
        segments.append((body.strip(), body_start + left, body_start + len(body.rstrip())))
    return segments


def _count_phrase(value: int, forms: tuple[str, str, str]) -> str:
    if value % 10 == 1 and value % 100 != 11:
        form = forms[0]
    elif value % 10 in {2, 3, 4} and value % 100 not in {12, 13, 14}:
        form = forms[1]
    else:
        form = forms[2]
    return f"{value} {form}"


def analyze_transcript(
    transcript: str,
    *,
    title: str = "Встреча",
    occurred_at: str | None = None,
) -> dict[str, Any]:
    """Extract participants and traceable meeting facts without network or an LLM."""
    participants: list[str] = []
    items: list[dict[str, Any]] = []
    current_topic: str | None = None

    for line_match in re.finditer(r"[^\n]+", transcript):
        raw_line = line_match.group(0)
        participant_line = re.match(
            r"^\s*(?:участники|присутствовали)\s*:\s*(.+)$",
            raw_line,
            flags=re.IGNORECASE,
        )
        if participant_line:
            for name in re.split(r"[,;]", participant_line.group(1)):
                normalized_name = name.strip()
                if normalized_name and normalized_name not in participants:
                    participants.append(normalized_name)
            continue
        speaker, body, body_start = _line_body(raw_line, line_match.start())
        if speaker and speaker not in participants:
            participants.append(speaker)
        for quote, start, end in _segments(body, body_start):
            kind, confidence = _classify(quote)
            if not kind:
                continue
            text = _clean_text(quote)
            if not text:
                continue
            if kind == "topic":
                current_topic = text.rstrip(".!?; ")
            owner = _extract_owner(quote, speaker, kind)
            items.append(
                {
                    "kind": kind,
                    "text": text,
                    "owner": owner,
                    "due_at": parse_due_date(quote, occurred_at),
                    "topic": current_topic,
                    "status": "open",
                    "source_quote": transcript[start:end],
                    "source_start": start,
                    "source_end": end,
                    "confidence": confidence,
                }
            )

    counts = {kind: sum(item["kind"] == kind for item in items) for kind, _ in _CUES}
    labels = (
        ("decision", ("решение", "решения", "решений")),
        ("action", ("поручение", "поручения", "поручений")),
        ("commitment", ("обязательство", "обязательства", "обязательств")),
        ("risk", ("риск", "риска", "рисков")),
        ("question", ("вопрос", "вопроса", "вопросов")),
        ("topic", ("тема", "темы", "тем")),
    )
    summary = "Извлечено: " + ", ".join(
        _count_phrase(counts[kind], forms) for kind, forms in labels
    )
    decisions = [item["text"].rstrip(".!?; ") for item in items if item["kind"] == "decision"]
    if decisions:
        summary += ". Главное решение: " + decisions[0]
    summary += "."
    return {
        "title": title.strip() or "Встреча",
        "occurred_at": occurred_at,
        "participants": participants,
        "summary": summary,
        "status": "analyzed",
        "items": items,
    }


__all__ = ["analyze_transcript", "parse_due_date"]
