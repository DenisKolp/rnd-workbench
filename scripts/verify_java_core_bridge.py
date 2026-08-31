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
    return parser


def main() -> None:
    args = build_parser().parse_args()
    client = JavaCorePolicyClient(
        [str(args.java), "-cp", str(args.lib_dir / "*"), JAVA_CORE_MAIN_CLASS],
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
        if result != expected:
            raise SystemExit("Java core route contract mismatch")
        print(json.dumps(result, ensure_ascii=True, separators=(",", ":")))
    finally:
        client.close()


if __name__ == "__main__":
    main()
