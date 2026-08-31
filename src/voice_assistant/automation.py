from __future__ import annotations

from datetime import UTC, datetime, timedelta
import re


_WEEKDAYS = {
    "mon": 0,
    "monday": 0,
    "понедельник": 0,
    "понедельникам": 0,
    "tue": 1,
    "tuesday": 1,
    "вторник": 1,
    "вторникам": 1,
    "wed": 2,
    "wednesday": 2,
    "среда": 2,
    "средам": 2,
    "thu": 3,
    "thursday": 3,
    "четверг": 3,
    "четвергам": 3,
    "fri": 4,
    "friday": 4,
    "пятница": 4,
    "пятницу": 4,
    "пятницам": 4,
    "sat": 5,
    "saturday": 5,
    "суббота": 5,
    "субботам": 5,
    "sun": 6,
    "sunday": 6,
    "воскресенье": 6,
    "воскресеньям": 6,
}


def next_run(schedule: str, *, after: datetime | None = None) -> datetime:
    """Parse a deliberately small, transparent local scheduling language."""

    after = after or datetime.now(UTC)
    if after.tzinfo is None:
        after = after.replace(tzinfo=UTC)
    normalized = schedule.casefold().strip()
    time_match = re.search(r"(?:в\s*)?(\d{1,2}):(\d{2})", normalized)
    hour, minute = (9, 0) if time_match is None else map(int, time_match.groups())
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError("Некорректное время автоматизации")

    if normalized.startswith("once ") or normalized.startswith("однократно "):
        date_match = re.search(r"(\d{4})-(\d{2})-(\d{2})", normalized)
        if date_match is None:
            raise ValueError("Для однократного запуска нужна дата YYYY-MM-DD")
        year, month, day = map(int, date_match.groups())
        result = datetime(year, month, day, hour, minute, tzinfo=after.tzinfo)
        if result <= after:
            raise ValueError("Время однократного запуска уже прошло")
        return result

    if any(marker in normalized for marker in ("daily", "ежеднев", "каждый день")):
        result = after.replace(hour=hour, minute=minute, second=0, microsecond=0)
        return result if result > after else result + timedelta(days=1)

    weekday = next(
        (value for name, value in _WEEKDAYS.items() if re.search(rf"\b{re.escape(name)}\b", normalized)),
        None,
    )
    if "weekly" in normalized or "кажд" in normalized or weekday is not None:
        if weekday is None:
            weekday = 0
        days = (weekday - after.weekday()) % 7
        result = (after + timedelta(days=days)).replace(
            hour=hour, minute=minute, second=0, microsecond=0
        )
        return result if result > after else result + timedelta(days=7)

    raise ValueError(
        "Расписание: «ежедневно 09:00», «каждую пятницу 17:00» или «once 2026-09-01 10:00»"
    )
