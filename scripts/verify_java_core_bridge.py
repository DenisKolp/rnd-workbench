"""Exercise the real Java 21 IPC process with metadata-only route requests."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from voice_assistant.java_core import JAVA_CORE_MAIN_CLASS, JavaCorePolicyClient


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--java", type=Path, required=True)
    parser.add_argument("--lib-dir", type=Path, required=True)
    parser.add_argument("--journal", type=Path, required=True)
    parser.add_argument("--external-models-enabled", action="store_true")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    command = [str(args.java), "-cp", str(args.lib_dir / "*"), JAVA_CORE_MAIN_CLASS]
    if args.external_models_enabled:
        command.append("--external-models-enabled")
    client = JavaCorePolicyClient(
        command,
        args.journal,
    )
    try:
        if not client.start():
            raise SystemExit("Java core health check failed")
        local = client.decide_route(
            classification="restricted",
            preference="local",
            local_available=True,
            corporate_available=True,
            corporate_scope_authorized=True,
        )
        corporate = client.decide_route(
            classification="internal",
            preference="corporate",
            local_available=True,
            corporate_available=True,
            corporate_scope_authorized=True,
        )
        external = (
            client.decide_route(
                classification="public",
                preference="external",
                local_available=True,
                corporate_available=True,
                external_available=True,
                explicit_external_consent=True,
            )
            if args.external_models_enabled
            else None
        )
        result = {
            "ready": client.ready,
            "protocol_version": client.diagnostics()["protocol_version"],
            "local": {
                "status": local.status,
                "route": local.route,
                "reason": local.reason,
            },
            "corporate": {
                "status": corporate.status,
                "route": corporate.route,
                "reason": corporate.reason,
                "local_fallback_before_first_output": (
                    corporate.local_fallback_before_first_output
                ),
            },
            "content_transmitted": False,
        }
        if external is not None:
            result["external"] = {
                "status": external.status,
                "route": external.route,
                "reason": external.reason,
                "local_fallback_before_first_output": (
                    external.local_fallback_before_first_output
                ),
            }
        expected = {
            "ready": True,
            "protocol_version": "1.0",
            "local": {
                "status": "SELECTED",
                "route": "LOCAL",
                "reason": "LOCAL_SELECTED",
            },
            "corporate": {
                "status": "SELECTED",
                "route": "CORPORATE",
                "reason": "CORPORATE_SELECTED",
                "local_fallback_before_first_output": True,
            },
            "content_transmitted": False,
        }
        if args.external_models_enabled:
            expected["external"] = {
                "status": "SELECTED",
                "route": "EXTERNAL",
                "reason": "EXTERNAL_SELECTED",
                "local_fallback_before_first_output": True,
            }
        if result != expected:
            raise SystemExit("Java core route contract mismatch")
        print(json.dumps(result, ensure_ascii=True, separators=(",", ":")))
    finally:
        client.close()


if __name__ == "__main__":
    main()
