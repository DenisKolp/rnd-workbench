from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

from voice_assistant.java_core import (
    JAVA_CORE_MAIN_CLASS,
    JavaCorePolicyClient,
    JavaCoreProtocolError,
    JavaCoreUnavailable,
    java_classification,
)


FAKE_CORE = r'''
import json
from pathlib import Path
import sys

journal = Path(sys.argv[sys.argv.index("--journal") + 1])
frames = journal.with_suffix(".frames")
for line in sys.stdin:
    request = json.loads(line)
    with frames.open("a", encoding="utf-8") as output:
        output.write(line)
    correlation = request["correlationId"]
    if request["type"] == "health.check":
        payload = {"protocolVersion": "1.0", "status": "ready"}
        response_type = "health.status"
    elif request["type"] == "route.decide":
        route = request["payload"]["preference"]
        payload = {
            "status": "SELECTED",
            "route": route,
            "reason": route + "_SELECTED",
            "localFallbackBeforeFirstOutput": route == "CORPORATE",
        }
        response_type = "route.decision"
    elif request["type"] == "autonomy.decide":
        kind = request["payload"]["actionKind"]
        if kind in {"DELETE_DATA", "MASS_OPERATION", "CHANGE_PERMISSIONS", "PUBLISH_EXTERNAL", "IRREVERSIBLE_HIGH_RISK"}:
            level = "REQUIRE_EXPLICIT_CONFIRMATION"
            reason = "pilot.explicit-confirmation"
        elif kind in {"READ_CONTEXT", "SEARCH_AND_ANALYZE", "TRANSCRIBE_AUDIO", "UPDATE_WORKING_MEMORY", "CREATE_DRAFT", "SELECT_MODEL"}:
            level = "ALLOW"
            reason = "pilot.allow"
        else:
            level = "REQUIRE_PREVIEW"
            reason = "pilot.preview"
        payload = {
            "level": level,
            "notificationRequired": level != "ALLOW",
            "undoRequired": False,
            "previewRequired": level != "ALLOW",
            "explicitConfirmationRequired": level == "REQUIRE_EXPLICIT_CONFIRMATION",
            "reasonCode": reason,
        }
        response_type = "autonomy.decision"
    elif request["type"] == "action.claim":
        payload = {
            "disposition": "CLAIMED",
            "claimToken": "00000000-0000-4000-8000-000000000001",
        }
        response_type = "action.claim.result"
    elif request["type"] == "action.inspect":
        payload = {
            "disposition": "IN_PROGRESS",
            "claimToken": "00000000-0000-4000-8000-000000000001",
        }
        response_type = "action.inspect.result"
    elif request["type"] == "action.complete":
        completion = request["payload"]
        payload = {
            "disposition": "RECORDED",
            "result": {
                "outcome": completion["outcome"],
                "resultCode": completion["resultCode"],
                "externalReference": completion.get("externalReference"),
                "completedAt": completion["completedAt"],
            },
        }
        if payload["result"]["externalReference"] is None:
            del payload["result"]["externalReference"]
        response_type = "action.complete.result"
    else:
        continue
    print(json.dumps({
        "correlationId": correlation,
        "ok": True,
        "payload": payload,
        "type": response_type,
        "version": "1.0",
    }, separators=(",", ":")), flush=True)
'''


def fake_client(tmp_path: Path, script_text: str = FAKE_CORE) -> JavaCorePolicyClient:
    script = tmp_path / "fake_core.py"
    script.write_text(script_text, encoding="utf-8")
    return JavaCorePolicyClient(
        [sys.executable, "-u", str(script)],
        tmp_path / "actions.sqlite3",
        timeout_seconds=0.5,
    )


def test_java_classification_mapping_matches_ipc_enum() -> None:
    assert java_classification("public") == "PUBLIC"
    assert java_classification("internal") == "CORPORATE_INTERNAL"
    assert java_classification("confidential") == "CONFIDENTIAL"
    assert java_classification("restricted") == "RESTRICTED"
    with pytest.raises(ValueError, match="classification"):
        java_classification("secret")


def test_java_core_client_health_and_route_golden_contract(tmp_path: Path) -> None:
    client = fake_client(tmp_path)
    assert client.start() is True
    assert client.diagnostics() == {
        "configured": True,
        "ready": True,
        "protocol_version": "1.0",
        "policy": "java21",
        "autonomy_policy_ready": True,
    }

    decision = client.decide_route(
        classification="internal",
        preference="corporate",
        local_available=False,
        corporate_available=True,
        corporate_scope_authorized=True,
    )
    assert decision.status == "SELECTED"
    assert decision.route == "CORPORATE"
    assert decision.reason == "CORPORATE_SELECTED"
    assert decision.local_fallback_before_first_output is True
    autonomy = client.decide_autonomy(action_kind="ASSIGN_WORK_ITEM")
    assert autonomy.level == "REQUIRE_PREVIEW"
    assert autonomy.notification_required is True
    assert autonomy.undo_required is False
    assert autonomy.preview_required is True
    assert autonomy.explicit_confirmation_required is False
    assert autonomy.reason_code == "pilot.preview"
    client.close()

    frames = [
        json.loads(line)
        for line in (tmp_path / "actions.frames").read_text(encoding="utf-8").splitlines()
    ]
    assert frames[0] == {
        "version": "1.0",
        "type": "health.check",
        "correlationId": "desktop-1",
        "payload": {},
    }
    assert frames[1] == {
        "version": "1.0",
        "type": "route.decide",
        "correlationId": "desktop-2",
        "payload": {
            "classification": "CORPORATE_INTERNAL",
            "preference": "CORPORATE",
            "availableRoutes": {
                "local": False,
                "corporate": True,
                "external": False,
            },
            "corporateScopeAuthorized": True,
            "explicitExternalConsent": False,
        },
    }
    assert frames[2] == {
        "version": "1.0",
        "type": "autonomy.decide",
        "correlationId": "desktop-3",
        "payload": {"actionKind": "ASSIGN_WORK_ITEM"},
    }
    serialized = json.dumps(frames, ensure_ascii=False)
    assert "prompt" not in serialized.casefold()
    assert "transcript" not in serialized.casefold()
    assert "api_key" not in serialized.casefold()


def test_java_core_client_rejects_invalid_response_envelope(tmp_path: Path) -> None:
    invalid_core = FAKE_CORE.replace('"version": "1.0"', '"version": "2.0"')
    client = fake_client(tmp_path, invalid_core)
    assert client.start() is False
    assert client.ready is False


def test_java_core_client_rejects_inconsistent_autonomy_decision(tmp_path: Path) -> None:
    invalid_core = FAKE_CORE.replace(
        '"notificationRequired": level != "ALLOW"',
        '"notificationRequired": False',
    )
    client = fake_client(tmp_path, invalid_core)
    assert client.start() is True

    with pytest.raises(JavaCoreProtocolError, match="autonomy"):
        client.decide_autonomy(action_kind="ASSIGN_WORK_ITEM")
    client.close()


def test_java_core_client_rejects_unknown_autonomy_action_before_ipc(
    tmp_path: Path,
) -> None:
    client = fake_client(tmp_path)
    assert client.start() is True

    with pytest.raises(ValueError, match="action kind"):
        client.decide_autonomy(action_kind="SEND_WITHOUT_CONFIRMATION")
    client.close()

    frames = (tmp_path / "actions.frames").read_text(encoding="utf-8").splitlines()
    assert len(frames) == 1


def test_java_core_action_journal_contract_transmits_only_safe_metadata(
    tmp_path: Path,
) -> None:
    client = fake_client(tmp_path)
    assert client.start() is True
    fingerprint = "a" * 64

    claim = client.claim_action(
        idempotency_key="act_example_0001",
        request_fingerprint=fingerprint,
    )
    inspection = client.inspect_action(
        idempotency_key="act_example_0001",
        request_fingerprint=fingerprint,
    )
    completion = client.complete_action(
        idempotency_key="act_example_0001",
        request_fingerprint=fingerprint,
        claim_token=str(claim.claim_token),
        outcome="success",
        result_code="ISSUE.CREATED",
        external_reference="RND-42",
        completed_at="2026-08-31T16:00:00Z",
    )
    client.close()

    assert claim.disposition == "CLAIMED"
    assert inspection.disposition == "IN_PROGRESS"
    assert inspection.claim_token == claim.claim_token
    assert completion.disposition == "RECORDED"
    assert completion.result is not None
    assert completion.result.result_code == "ISSUE.CREATED"
    frames = [
        json.loads(line)
        for line in (tmp_path / "actions.frames").read_text(encoding="utf-8").splitlines()
    ]
    action_frames = frames[1:]
    assert [frame["type"] for frame in action_frames] == [
        "action.claim",
        "action.inspect",
        "action.complete",
    ]
    serialized = json.dumps(action_frames, ensure_ascii=False).casefold()
    assert "prompt" not in serialized
    assert "transcript" not in serialized
    assert "payload_text" not in serialized


def test_java_core_client_times_out_without_exposing_peer_output(tmp_path: Path) -> None:
    silent_core = "import time\ntime.sleep(2)\n"
    client = fake_client(tmp_path, silent_core)
    assert client.start() is False
    with pytest.raises(JavaCoreUnavailable, match="unavailable"):
        client.decide_route(
            classification="public",
            preference="local",
            local_available=True,
            corporate_available=False,
        )


def test_java_core_constants_match_distribution_entrypoint() -> None:
    assert JAVA_CORE_MAIN_CLASS == "com.rndworkbench.core.ipc.CoreIpcApplication"
    assert issubclass(JavaCoreProtocolError, RuntimeError)


def test_environment_can_enable_separately_gated_public_external_route(
    monkeypatch,
    tmp_path: Path,
) -> None:
    java = tmp_path / "java"
    java.write_text("", encoding="utf-8")
    libraries = tmp_path / "lib"
    libraries.mkdir()
    monkeypatch.setenv("RND_WORKBENCH_JAVA_CORE_JAVA", str(java))
    monkeypatch.setenv("RND_WORKBENCH_JAVA_CORE_LIB_DIR", str(libraries))
    monkeypatch.setenv("RND_WORKBENCH_JAVA_CORE_EXTERNAL_MODELS_ENABLED", "true")

    client = JavaCorePolicyClient.from_environment(tmp_path / "assistant.sqlite3")

    assert client.configured is True
    assert client._command_prefix[-1] == "--external-models-enabled"
