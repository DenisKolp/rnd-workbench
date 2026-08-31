from pathlib import Path


SWIFT_SOURCE = Path(__file__).parents[1] / "macos" / "VoiceAssistantApp.swift"
BUILD_SCRIPT = Path(__file__).parents[1] / "macos" / "build.sh"
INFO_PLIST = Path(__file__).parents[1] / "macos" / "Info.plist"
CI_WORKFLOW = Path(__file__).parents[1] / ".github" / "workflows" / "ci.yml"


def test_full_and_compact_views_are_singleton_mutually_exclusive_windows() -> None:
    source = SWIFT_SOURCE.read_text(encoding="utf-8")

    assert "WindowGroup(" not in source
    assert source.count('Window("RnD Workbench", id: "assistant")') == 1
    assert 'Window("RnD Workbench — компактный режим", id: "compact")' not in source
    assert "struct AssistantRootView: View" in source
    assert "controller.presentationMode == .compact" in source
    assert "AssistantWorkspaceView(controller: controller)" in source
    assert "CompactAssistantView(controller: controller)" in source
    assert "openWindow" not in source
    assert "dismissWindow" not in source
    assert "func presentCompact()" in source
    assert "func presentFull()" in source
    assert "AssistantWindowBridge.captureFullFrame()" in source
    assert "window.isReleasedWhenClosed = false" in source


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


def test_auto_model_routing_exposes_an_explicit_safe_remote_policy() -> None:
    source = SWIFT_SOURCE.read_text(encoding="utf-8")

    assert "var isAutoLLMActive: Bool" in source
    assert 'llmMode == "auto"' in source
    assert '.tag("auto")' in source
    assert 'controller.settings["auto_remote_policy"]' in source
    assert '.tag("local_only")' in source
    assert '.tag("eligible")' in source
    configure_block = source.split("func configureExternalLLM", maxsplit=1)[1].split(
        "func useLocalLLM", maxsplit=1
    )[0]
    assert '"auto_remote_policy":' in configure_block
    assert '"provider_type": providerType' in configure_block


def test_routing_metrics_and_non_mutating_fallback_are_visible_in_swift_ui() -> None:
    source = SWIFT_SOURCE.read_text(encoding="utf-8")

    assert "@Published var firstTokenSeconds: Double?" in source
    assert "@Published var firstAudioSeconds: Double?" in source
    assert 'case "metric":' in source

    assert 'case "llm_first_token": firstTokenSeconds = number(event["seconds"])' in source
    assert 'case "voice_first_audio": firstAudioSeconds = number(event["seconds"])' in source
    assert 'case "response_total": responseSeconds = number(event["seconds"])' in source

    assert 'case "routing_fallback":' in source
    fallback_block = source.split('case "routing_fallback":', maxsplit=1)[1].split(
        'case "', maxsplit=1
    )[0]
    assert "statusText" in fallback_block
    assert 'event["message"]' in fallback_block
    assert 'settings["llm_mode"]' not in fallback_block


def test_macos_settings_expose_content_free_pilot_metrics_and_json_export() -> None:
    source = SWIFT_SOURCE.read_text(encoding="utf-8")

    assert "@Published private(set) var pilotMetrics: [String: Any] = [:]" in source
    assert 'pilotMetrics = (data["pilot_metrics"] as? [String: Any]) ?? [:]' in source
    assert 'Section("Качество пилота")' in source
    assert "controller.pilotMetricsSummaryLabel" in source
    assert "func exportPilotMetrics()" in source
    assert '"command": "export_pilot_metrics"' in source
    assert "без запросов, транскриптов, ответов и идентификаторов сессий" in source


def test_java_action_journal_and_reconciliation_are_visible_in_settings() -> None:
    source = SWIFT_SOURCE.read_text(encoding="utf-8")

    assert "@Published private(set) var javaActionJournalReady = false" in source
    assert "@Published private(set) var actionRecoveryAttention = 0" in source
    assert 'javaActionJournalReady = bool(actionJournal["ready"])' in source
    assert '"Защита внешних действий"' in source
    assert 'return "Нужна сверка: \\(actionRecoveryAttention)"' in source


def test_editable_voice_dictation_is_returned_to_the_shared_composer() -> None:
    source = SWIFT_SOURCE.read_text(encoding="utf-8")

    assert 'case "dictation_ready":' in source
    dictation_block = source.split('case "dictation_ready":', maxsplit=1)[1].split(
        'case "', maxsplit=1
    )[0]
    assert "composerDraft" in dictation_block
    assert 'event["text"]' in dictation_block
    assert "statusText" in dictation_block

    assert 'controller.settings["voice_review_before_send"]' in source
    assert 'controller.setSetting("voice_review_before_send"' in source


def test_memory_controls_and_editor_preserve_semantic_memory_kind() -> None:
    source = SWIFT_SOURCE.read_text(encoding="utf-8")

    for setting in (
        "memory_preferences_enabled",
        "memory_facts_enabled",
        "memory_commitments_enabled",
        "memory_work_enabled",
    ):
        assert f'controller.settings["{setting}"]' in source
        assert f'controller.setSetting("{setting}"' in source

    save_block = source.split("func saveMemory", maxsplit=1)[1].split(
        "func ", maxsplit=1
    )[0]
    update_block = source.split("func updateMemory", maxsplit=1)[1].split(
        "func ", maxsplit=1
    )[0]
    assert '"kind": kind' in save_block
    assert '"kind": kind' in update_block

    assert "@State private var memoryKind" in source
    assert "selection: $memoryKind" in source
    assert '.tag("preference")' in source
    assert '.tag("fact")' in source
    assert '.tag("commitment")' in source
    assert '.tag("note")' in source
    assert "memoryKind = item.kind" in source


def test_compact_voice_widget_keeps_latest_turn_visible_and_controls_accessible() -> None:
    source = SWIFT_SOURCE.read_text(encoding="utf-8")
    compact = source.split("struct CompactAssistantView: View", maxsplit=1)[1].split(
        "struct MenuBarContent: View", maxsplit=1
    )[0]

    assert ".frame(width: 400, height: 238)" in compact
    assert "controller.compactLLMRouteStatusLabel" in compact
    assert ".font(.system(size: 9, weight: .semibold))" in compact
    assert "controller.firstAudioSeconds" in compact
    assert 'Text("Звук \\(value, specifier: \"%.2f\")с")' in compact
    assert ".onChange(of: controller.messages.last?.text)" in compact
    assert ".onChange(of: controller.quickActions.count)" in compact
    assert ".accessibilityLabel(controller.voiceSessionActionLabel)" in compact
    assert ".accessibilityHint(controller.voiceSessionActionHint)" in compact
    assert ".accessibilityValue(controller.voiceSessionAccessibilityValue)" in compact


def test_voice_session_can_be_stopped_while_pending_and_never_hides_behind_chat() -> None:
    source = SWIFT_SOURCE.read_text(encoding="utf-8")
    compact = source.split("struct CompactAssistantView: View", maxsplit=1)[1].split(
        "struct MenuBarContent: View", maxsplit=1
    )[0]

    toggle = source.split("func toggleSession()", maxsplit=1)[1].split(
        "func submit", maxsplit=1
    )[0]
    assert "if isSessionActive || isVoiceStartPending { stopVoiceSession() }" in toggle
    assert "func stopVoiceSession()" in toggle
    assert 'send(["command": "stop"])' in toggle
    assert "if controller.isSessionActive || controller.isVoiceStartPending { return .voice }" in compact
    assert "if newMode == .chat { controller.stopVoiceSession() }" in compact
    assert 'Button { controller.hideAssistantWindow() }' in compact


def test_tts_retry_is_disabled_until_the_backend_can_accept_it() -> None:
    source = SWIFT_SOURCE.read_text(encoding="utf-8")
    retry_policy = source.split("var canRetrySpeech: Bool", maxsplit=1)[1].split(
        "var voiceSessionActionLabel", maxsplit=1
    )[0]
    retry_action = source.split("func retrySpeech()", maxsplit=1)[1].split(
        "func dismissSpeechError", maxsplit=1
    )[0]
    banner = source.split("struct SpeechFailureBanner: View", maxsplit=1)[1].split(
        "struct ComposerSuggestionsView", maxsplit=1
    )[0]

    assert "speechErrorRetryable && isReady" in retry_policy
    assert "!isSessionActive && !isVoiceStartPending" in retry_policy
    assert "!isLLMTurnPending && !state.isBusy" in retry_policy
    assert 'return "Сначала остановите голосовой режим."' in retry_policy
    assert "guard canRetrySpeech else { return }" in retry_action
    assert source.count("retryable: controller.canRetrySpeech") == 2
    assert "Text(message)" in banner
    assert "Text(retryUnavailableReason)" in banner


def test_primary_icon_controls_have_explicit_accessibility_names() -> None:
    source = SWIFT_SOURCE.read_text(encoding="utf-8")

    assert source.count('.accessibilityLabel("Добавить файлы")') == 2
    assert source.count('.accessibilityLabel("Отправить сообщение")') == 2
    assert source.count(".accessibilityLabel(controller.voiceSessionActionLabel)") == 2
    assert source.count(".accessibilityValue(controller.voiceSessionAccessibilityValue)") == 2
    assert '.accessibilityLabel("Развернуть в полное окно")' in source
    assert '.accessibilityLabel("Скрыть окно RnD Workbench")' in source
    assert '.accessibilityLabel("Ошибка озвучивания. \\(message)")' in source


def test_compact_expand_action_reuses_single_full_window() -> None:
    source = SWIFT_SOURCE.read_text(encoding="utf-8")
    compact = source.split("struct CompactAssistantView: View", maxsplit=1)[1].split(
        "struct MenuBarContent: View", maxsplit=1
    )[0]

    assert 'Button { controller.presentFull() }' in compact
    assert source.count('Window("RnD Workbench", id: "assistant")') == 1
    assert 'Window("RnD Workbench — компактный режим", id: "compact")' not in source
    assert "window.setContentSize(NSSize(width: 400, height: 238))" in source
    assert "window.setFrame(fullFrame, display: true, animate: true)" in source


def test_global_right_option_push_to_talk_has_truthful_permissions_and_backend_contract() -> None:
    source = SWIFT_SOURCE.read_text(encoding="utf-8")

    assert "private final class GlobalPushToTalkMonitor" in source
    assert "static let rightOptionKeyCode: CGKeyCode = 61" in source
    assert "CGEventType.flagsChanged.rawValue" in source
    assert "options: .listenOnly" in source
    assert "CGEventSource.keyState(" in source
    disabled_tap = source.split(
        "if type == .tapDisabledByTimeout || type == .tapDisabledByUserInput",
        maxsplit=1,
    )[1].split("guard type == .flagsChanged", maxsplit=1)[0]
    assert "rightOptionIsDown = false" in disabled_tap
    assert "self?.onRelease?()" in disabled_tap
    assert "CGPreflightListenEventAccess()" in source
    assert "CGRequestListenEventAccess()" in source
    assert "AXIsProcessTrustedWithOptions(options)" in source
    assert "func startExternalDictation()" in source
    assert "func stopExternalDictation()" in source
    assert 'send(["command": "dictation_start", "destination": "system"])' in source
    assert 'send(["command": "dictation_stop", "destination": "system"])' in source
    assert "controller.globalPushToTalkStatusLabel" in source
    assert 'LabeledContent("Глобальная диктовка", value: "Правая ⌥ · удерживать")' in source


def test_system_dictation_inserts_safely_and_never_targets_secure_fields() -> None:
    source = SWIFT_SOURCE.read_text(encoding="utf-8")

    assert "private final class SystemTextInserter" in source
    assert "kAXFocusedUIElementAttribute" in source
    assert "kAXSelectedTextAttribute" in source
    assert "kAXSecureTextFieldSubrole" in source
    assert "func isEditableTarget(_ element: AXUIElement) -> Bool" in source
    assert 'boolAttribute("AXReadOnly", of: element) == true' in source
    assert 'boolAttribute("AXEditable", of: element) == false' in source
    assert 'return .failed("Активный элемент не поддерживает ввод текста")' in source
    assert '"password"' in source
    assert '"парол"' in source
    assert "CGPreflightPostEventAccess()" in source
    assert "CFEqual(focusedNow, element)" in source
    assert 'return .failed("Фокус изменился — текст не вставлен")' in source
    assert 'virtualKey: 0, keyDown: true' in source
    assert "keyboardSetUnicodeString" in source
    assert "keyDown.postToPid(targetPid)" in source
    assert "NSPasteboard.general" not in source
    assert "case keyboardUnverified" in source

    ready = source.split('case "dictation_ready":', maxsplit=1)[1].split(
        'case "dictation_error":', maxsplit=1
    )[0]
    assert 'destination == "system"' in ready
    assert "systemTextInserter.insert(text, into: externalDictationTarget)" in ready
    assert "externalDictationFocusChanged" in ready
    assert "composerDraft = cleanText" in ready
    assert "Текст сохранён в черновик RnD Workbench" in ready
    assert "else if case .keyboardUnverified = result" in ready
    assert "Копия сохранена в черновик RnD Workbench" in ready
    assert "composerDraft = text" in ready

    error = source.split('case "dictation_error":', maxsplit=1)[1].split(
        'case "user":', maxsplit=1
    )[0]
    assert "externalDictationTarget = nil" in error
    assert "externalDictationStartPending = false" in error
    assert "externalDictationActive = false" in error


def test_macos_build_links_global_input_frameworks_and_advances_build_number() -> None:
    build = BUILD_SCRIPT.read_text(encoding="utf-8")
    plist = INFO_PLIST.read_text(encoding="utf-8")

    assert "-framework ApplicationServices" in build
    assert "-framework CoreGraphics" in build
    assert "clean test installDist" in build
    assert "--compress=zip-6" in build
    assert "verify_java_core_bridge.py" in build
    assert "--external-models-enabled" in build
    assert "<string>9</string>" in plist


def test_macos_app_launches_bundled_java_policy_and_shows_safe_fallback() -> None:
    source = SWIFT_SOURCE.read_text(encoding="utf-8")

    assert 'environment["RND_WORKBENCH_JAVA_CORE_JAVA"]' in source
    assert 'environment["RND_WORKBENCH_JAVA_CORE_LIB_DIR"]' in source
    assert 'environment["RND_WORKBENCH_JAVA_CORE_EXTERNAL_MODELS_ENABLED"] = "1"' in source
    assert "javaCorePolicyConfigured" in source
    assert "javaCorePolicyReady" in source
    assert 'return "Java 21 · активна"' in source
    assert 'return "Встроенная резервная политика"' in source
    assert '"Общая политика маршрутизации"' in source
    assert ".help(controller.routeStatusHelp)" in source


def test_macos_ci_builds_jlink_and_verifies_public_external_route() -> None:
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")

    assert "macOS Swift and Java policy boundary" in workflow
    assert "runs-on: macos-14" in workflow
    assert "swiftc -parse macos/VoiceAssistantApp.swift" in workflow
    assert "tests/test_macos_contract.py tests/test_java_core_bridge.py" in workflow
    assert "Build bounded Java policy runtime" in workflow
    assert "--compress=zip-6" in workflow
    assert "Verify macOS bundled-style public external policy contract" in workflow
    assert "--external-models-enabled" in workflow


def test_macos_app_supports_isolated_qa_profile_without_touching_user_data() -> None:
    source = SWIFT_SOURCE.read_text(encoding="utf-8")

    assert 'processEnvironment["RND_WORKBENCH_SUPPORT_DIR"]' in source
    assert 'supportRoot.appendingPathComponent("assistant.sqlite3")' in source


def test_meeting_import_menu_supports_audio_transcript_and_synapse_package() -> None:
    source = SWIFT_SOURCE.read_text(encoding="utf-8")

    assert "func chooseSynapseMeetingPackage()" in source
    assert '"command": "import_synapse_package"' in source
    assert 'case "synapse_package_imported":' in source
    assert 'Label("Добавить встречу", systemImage: "plus.circle.fill")' in source
    assert 'Label("Аудиозапись", systemImage: "waveform.badge.plus")' in source
    assert 'Label("Готовый транскрипт", systemImage: "doc.badge.plus")' in source
    assert 'Label("Папка или ZIP eXpress (Синапс)", systemImage: "shippingbox.and.arrow.backward")' in source
