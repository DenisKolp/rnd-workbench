---
id: BACKLOG-RND-WORKBENCH-CORPORATE-NEXT
type: backlog
status: ready
priority: P1
agent_size: large
title: Continue the remaining corporate-assistant specification
created_at: '2026-08-30T17:49:40+03:00'
updated_at: '2026-08-30T17:49:40+03:00'
approved_at: '2026-08-30T17:49:40+03:00'
approved_by: user
source:
  kind: chat_prompt
source_refs:
  - DECISION-20260830-011
  - VERIFY-20260830-005
modules:
  - backend
  - desktop
tags:
  - backlog
  - corporate-mvp
  - resume
depends_on: []
blocked_by: []
files:
  - README.md
  - src/voice_assistant/store.py
  - src/voice_assistant/orchestrator.py
  - src/voice_assistant/ui_backend.py
  - macos/VoiceAssistantApp.swift
acceptance_criteria:
  - Add explicit data-classification labels and enforce them before external routing.
  - Add fragment-level source deep links and native artifact version, restore and provenance controls.
  - Add slash and source autocomplete plus natural-language task-plan mutation.
  - Add workspace timeline, richer digest configuration and persistent background execution design.
  - Keep unavailable corporate connectors, SSO, RBAC and office-format generators explicitly blocked until real APIs or policy are supplied.
checks:
  - .venv/bin/python -m pytest -q
  - swiftc typecheck macos/VoiceAssistantApp.swift
  - endpoint canonicalization self-test
  - codesign --verify --deep --strict RnD Workbench.app
retention: keep
---

# Continue the remaining corporate-assistant specification

## Description

Resume the active product goal from the verified 0.8.0 build 6 baseline after
the Codex usage limit refreshes. The complete requirement matrix and honest
coverage status are in `../Corporate_Assistant_MVP_Coverage.md`.

Prioritize the locally feasible product gaps: data classification for external
LLM routing, source-fragment navigation, native artifact history/provenance,
command and source autocomplete, task-plan mutation, workspace chronology,
digest configuration and a reviewed background-execution model.

Do not simulate Email, Calendar, Синапс, Project 360, Service Desk, SSO, RBAC or
corporate office-document generation. Those remain external dependencies until
the user supplies real endpoints, credentials, policies and distribution
requirements.

## Acceptance criteria

- Every newly claimed capability has a native user path and regression tests.
- External routing rejects content above the user-approved classification.
- Existing local voice, meeting, deletion and compact-window contracts remain
  green.
- The coverage matrix and HTML overview are updated from verification evidence,
  not assumptions.

## Checks

- `.venv/bin/python -m pytest -q`
- Swift typecheck and endpoint self-test
- Local external-provider mock
- Final build, strict codesign and live data-integrity smoke
