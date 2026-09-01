from voice_assistant.preflight import PilotPreflightInputs, build_pilot_preflight
from voice_assistant.store import AssistantStore


def _inputs(**overrides):  # noqa: ANN003, ANN201
    values = {
        "platform": "windows",
        "storage_ready": True,
        "llm_ready": True,
        "stt_ready": True,
        "tts_ready": True,
        "microphone_verified": True,
        "java_policy_ready": True,
        "action_journal_ready": True,
        "connected_systems": ("synapse",),
        "manual_meeting_import_ready": True,
        "distribution_verified": True,
        "metrics_summary": {
            "metrics": {
                name: {"slo": {"status": "pass"}}
                for name in (
                    "listen_ready_seconds",
                    "transcript_ready_seconds",
                    "first_audio_seconds",
                    "barge_in_stop_seconds",
                    "tts_rtf",
                    "input_clipping_ratio",
                    "output_clipping_ratio",
                    "stt_clean_wer",
                    "stt_corporate_wer",
                )
            },
            "usage": {
                "observed_session_exits": 20,
                "crash_free_session_rate": 1.0,
            },
        },
    }
    values.update(overrides)
    return PilotPreflightInputs(**values)


def test_complete_preflight_is_ready_and_content_free() -> None:
    result = build_pilot_preflight(_inputs())

    assert result["overall"] == "ready"
    assert result["counts"] == {"pass": 12, "warn": 0, "block": 0, "unverified": 0}
    assert result["content_transmitted"] is False
    assert all(set(check) == {"id", "title", "status", "detail", "action"} for check in result["checks"])


def test_missing_voice_runtime_blocks_but_external_dependencies_only_warn() -> None:
    result = build_pilot_preflight(
        _inputs(
            stt_ready=False,
            tts_ready=False,
            connected_systems=(),
            distribution_verified=False,
        )
    )
    statuses = {check["id"]: check["status"] for check in result["checks"]}

    assert result["overall"] == "blocked"
    assert statuses["stt"] == "block"
    assert statuses["tts"] == "block"
    assert statuses["corporate_connectors"] == "warn"
    assert statuses["distribution"] == "warn"


def test_voice_slo_requires_five_sample_aggregate_and_fails_honestly() -> None:
    insufficient = build_pilot_preflight(
        _inputs(metrics_summary={"metrics": {}})
    )
    failing = build_pilot_preflight(
        _inputs(
            metrics_summary={
                "metrics": {
                    "listen_ready_seconds": {"slo": {"status": "fail"}}
                }
            }
        )
    )

    insufficient_check = next(
        check for check in insufficient["checks"] if check["id"] == "voice_slo"
    )
    failing_check = next(
        check for check in failing["checks"] if check["id"] == "voice_slo"
    )
    assert insufficient["overall"] == "limited"
    assert insufficient_check["status"] == "unverified"
    assert failing["overall"] == "blocked"
    assert failing_check["status"] == "block"


def test_session_reliability_requires_twenty_exits_and_blocks_below_99_percent() -> None:
    insufficient = build_pilot_preflight(
        _inputs(
            metrics_summary={
                "metrics": _inputs().metrics_summary["metrics"],
                "usage": {
                    "observed_session_exits": 19,
                    "crash_free_session_rate": 1.0,
                },
            }
        )
    )
    failing = build_pilot_preflight(
        _inputs(
            metrics_summary={
                "metrics": _inputs().metrics_summary["metrics"],
                "usage": {
                    "observed_session_exits": 20,
                    "crash_free_session_rate": 0.95,
                },
            }
        )
    )

    insufficient_check = next(
        check for check in insufficient["checks"] if check["id"] == "session_reliability"
    )
    failing_check = next(
        check for check in failing["checks"] if check["id"] == "session_reliability"
    )
    assert insufficient_check["status"] == "unverified"
    assert failing_check["status"] == "block"
    assert failing["overall"] == "blocked"


def test_store_health_check_has_no_path_or_content(tmp_path) -> None:
    store = AssistantStore(tmp_path / "private-name.sqlite3")
    result = store.health_check()

    assert result["ready"] is True
    assert result["integrity"] == "ok"
    assert result["content_transmitted"] is False
    assert "private-name" not in str(result)
