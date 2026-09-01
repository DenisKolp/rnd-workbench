# ADR-0002: Windows session background execution

- Status: accepted for pilot source milestone 0.9.14
- Date: 2026-09-01
- Scope: Windows Electron pilot only

## Context

Timed local work must continue when the compact or full window is hidden, but
the pilot does not yet have an enterprise installer, managed startup policy,
service account, Windows code-signing certificate or rules for unattended
remote model access. Treating those missing pieces as a persistent OS service
would overstate readiness and could execute work outside the user's visible
session.

## Decision

The Windows pilot uses a **session tray** model:

1. Electron owns one `BrowserWindow`, one backend child process and one tray
   entry.
2. Closing the window hides it in the Windows notification area. It does not
   stop the backend.
3. The Python scheduler polls the shared SQLite store while the application is
   running. It executes only deterministic local `/digest` automations with a
   time schedule.
4. Digest period, selected sections and meeting-item kinds are parsed by a
   bounded grammar and persisted with the artifact. No LLM or remote endpoint
   is selected for this path.
5. The tray menu has an explicit **Выход** action. Choosing it stops the backend
   and scheduler cleanly.
6. Startup with Windows is `false`. Event-triggered rules and unattended model
   or connector actions are rejected by the Windows backend.

The Electron process owns window/tray lifecycle because that is platform UI
behavior. Python owns digest scheduling and local content because it already
owns SQLite and the deterministic workflow. Java 21 remains the metadata-only
policy and idempotency boundary; digest content is not moved through it.

## Security and product limits

- Hiding is not quitting; the close control and UI copy say that the window is
  hidden in the notification area.
- The scheduler does not receive or persist an API key.
- Remote and corporate endpoints are not called by background automations.
- No task claims to run after logout, reboot or explicit exit.
- Event schedules such as “при новом источнике” are rejected on Windows until
  the event intake and lifecycle are verified together.
- The application still needs Windows hardware QA, code signing and managed
  deployment before a company pilot.

## Verification

Automated contracts cover deterministic digest filtering, short TTS versus full
chat text, automation create/update/toggle/delete, next-run persistence, the
session-only capability label, tray hide/restore/exit behavior and wrapping of
automation controls. GitHub Actions must additionally build and smoke-test the
frozen Windows backend and unsigned portable package.

## Future extension

OS startup or a service may be added only after the pilot owner supplies:

- an installer and signed binaries;
- an administrator-approved startup/uninstall policy;
- a user-visible pause/exit model;
- credential storage and rotation rules;
- reconciliation tests for sleep, reboot, duplicate instances and missed runs;
- explicit policy for any unattended corporate or external model call.
