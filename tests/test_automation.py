from datetime import UTC, datetime

import pytest

from voice_assistant.automation import next_run


def test_daily_schedule() -> None:
    after = datetime(2026, 8, 29, 10, 0, tzinfo=UTC)
    assert next_run("ежедневно 09:00", after=after) == datetime(
        2026, 8, 30, 9, 0, tzinfo=UTC
    )


def test_weekly_schedule_in_russian() -> None:
    after = datetime(2026, 8, 29, 10, 0, tzinfo=UTC)  # Saturday
    assert next_run("каждую пятницу 17:00", after=after) == datetime(
        2026, 9, 4, 17, 0, tzinfo=UTC
    )


def test_invalid_schedule_is_explicit() -> None:
    with pytest.raises(ValueError, match="Расписание"):
        next_run("иногда")
