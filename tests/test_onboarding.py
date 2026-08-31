from voice_assistant.onboarding import build_pilot_onboarding


def _preflight(**overrides: str) -> dict:
    statuses = {
        "storage": "pass",
        "llm": "pass",
        "stt": "pass",
        "tts": "pass",
        "microphone": "unverified",
    }
    statuses.update(overrides)
    return {
        "checks": [
            {
                "id": check_id,
                "status": status,
                "detail": "private detail must not escape",
            }
            for check_id, status in statuses.items()
        ]
    }


def _usage(**overrides):  # noqa: ANN003, ANN201
    values = {
        "first_value_seconds": None,
        "voice_turns": 0,
        "meeting_imports": 0,
        "meeting_briefings": 0,
    }
    values.update(overrides)
    return values


def test_onboarding_prioritizes_core_blocker_without_leaking_details() -> None:
    result = build_pilot_onboarding(
        _preflight(llm="block"),
        _usage(secret="do not expose"),
    )

    assert result["status"] == "blocked"
    assert result["stage"] == "repair_core"
    assert result["action_id"] == "review_preflight"
    assert result["content_transmitted"] is False
    assert "private" not in str(result)
    assert "secret" not in str(result)


def test_first_result_prefers_voice_but_falls_back_to_chat() -> None:
    voice = build_pilot_onboarding(_preflight(), _usage())
    chat = build_pilot_onboarding(_preflight(tts="block"), _usage())

    assert voice["stage"] == "first_voice_result"
    assert voice["action_id"] == "start_voice"
    assert chat["stage"] == "first_text_result"
    assert chat["action_id"] == "open_chat"


def test_onboarding_advances_through_voice_and_meeting_vertical() -> None:
    first_value = build_pilot_onboarding(
        _preflight(),
        _usage(first_value_seconds=42.0),
    )
    voice_done = build_pilot_onboarding(
        _preflight(),
        _usage(first_value_seconds=42.0, voice_turns=1),
    )
    meeting_done = build_pilot_onboarding(
        _preflight(),
        _usage(first_value_seconds=42.0, voice_turns=1, meeting_imports=1),
    )

    assert first_value["stage"] == "try_voice"
    assert first_value["progress"] == {"completed": 1, "total": 4}
    assert voice_done["stage"] == "import_meeting"
    assert voice_done["progress"] == {"completed": 2, "total": 4}
    assert meeting_done["stage"] == "prepare_briefing"
    assert meeting_done["progress"] == {"completed": 3, "total": 4}


def test_onboarding_completes_only_after_all_four_milestones() -> None:
    result = build_pilot_onboarding(
        _preflight(),
        _usage(
            first_value_seconds=42.0,
            voice_turns=2,
            meeting_imports=1,
            meeting_briefings=1,
        ),
    )

    assert result["status"] == "completed"
    assert result["stage"] == "completed"
    assert result["action_id"] == ""
    assert result["progress"] == {"completed": 4, "total": 4}
