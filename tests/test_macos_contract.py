from pathlib import Path


SWIFT_SOURCE = Path(__file__).parents[1] / "macos" / "VoiceAssistantApp.swift"


def test_full_and_compact_views_are_singleton_mutually_exclusive_windows() -> None:
    source = SWIFT_SOURCE.read_text(encoding="utf-8")

    assert "WindowGroup(" not in source
    assert source.count('Window("RnD Workbench", id: "assistant")') == 1
    assert source.count(
        'Window("RnD Workbench — компактный режим", id: "compact")'
    ) == 1
    assert source.count('openWindow(id: "compact")') >= 2
    assert source.count('dismissWindow(id: "assistant")') >= 2
    assert source.count('openWindow(id: "assistant")') >= 3
    assert source.count('dismissWindow(id: "compact")') >= 3


def test_composer_draft_is_shared_across_full_and_compact_states() -> None:
    source = SWIFT_SOURCE.read_text(encoding="utf-8")

    assert '@Published var composerDraft = ""' in source
    assert source.count("$controller.composerDraft") == 1
    assert "struct ComposerTextField: View" in source
    assert source.count("ComposerTextField(") == 2
    assert "@State private var inputText" not in source


def test_full_and_compact_composers_share_command_palette_and_staged_files() -> None:
    source = SWIFT_SOURCE.read_text(encoding="utf-8")

    assert "struct ComposerSuggestionsView: View" in source
    assert source.count("ComposerSuggestionsView(controller: controller") == 2
    assert ".onKeyPress(.tab)" in source
    assert ".onKeyPress(.upArrow)" in source
    assert ".onKeyPress(.downArrow)" in source
    assert "func chooseComposerAttachments()" in source
    assert source.count("controller.chooseComposerAttachments()") == 2
    assert 'payload["attachments"] = pendingAttachments.map' in source
    assert "disabled(controller.currentTaskID == nil)" not in source


def test_quick_actions_and_skill_apply_are_wired_to_backend_events() -> None:
    source = SWIFT_SOURCE.read_text(encoding="utf-8")

    assert 'case "quick_action_completed":' in source
    assert 'case "artifact_versions":' in source
    assert '"command": "quick_action"' in source
    assert "struct QuickActionsBar: View" in source
    assert "controller.runSkillCommand(item)" in source
    assert "controller.insertSkillCommand(item)" in source
    assert 'Button("Применить", action: onApply)' in source


def test_workspace_timeline_snapshot_is_rendered_with_filters_and_deep_links() -> None:
    source = SWIFT_SOURCE.read_text(encoding="utf-8")

    assert 'rows(data, "workspace_timeline")' in source
    assert "struct WorkspaceTimelineRecord: Identifiable, Hashable" in source
    assert "struct WorkspaceTimelineSection: View" in source
    assert "WorkspaceTimelineSection(controller: controller)" in source
    assert "WorkspaceTimelineFilter.allCases" in source
    assert 'case all, tasks, meetings, decisions, sources, artifacts, approvals' in source
    assert "controller.openWorkspaceTimelineItem(item)" in source
    assert "controller.openWorkspaceTimelineSource(item)" in source


def test_workspace_timeline_shows_explicit_decision_history() -> None:
    source = SWIFT_SOURCE.read_text(encoding="utf-8")

    assert 'Text("Версия решения \\(sequence) из \\(count)")' in source
    assert 'Text("Текущее")' in source
    assert 'Text("Текущее решение: \\(item.currentDecisionText)")' in source
    assert "decisionThreadKey: row[\"decision_thread_key\"] as? String" in source
    assert "isCurrentDecision: bool(row[\"is_current_decision\"])" in source
