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
    serialized = json.dumps(frames, ensure_ascii=False)
    assert "prompt" not in serialized.casefold()
    assert "transcript" not in serialized.casefold()
    assert "api_key" not in serialized.casefold()


def test_java_core_client_rejects_invalid_response_envelope(tmp_path: Path) -> None:
    invalid_core = FAKE_CORE.replace('"version": "1.0"', '"version": "2.0"')
    client = fake_client(tmp_path, invalid_core)
    assert client.start() is False
    assert client.ready is False


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
