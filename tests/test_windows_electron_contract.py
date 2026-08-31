from pathlib import Path
import re


ROOT = Path(__file__).parents[1]
MAIN = ROOT / "windows" / "electron" / "main.js"
PACKAGE = ROOT / "windows" / "electron" / "package.json"
PRELOAD = ROOT / "windows" / "electron" / "preload.js"
RENDERER = ROOT / "windows" / "electron" / "renderer" / "app.js"
HTML = ROOT / "windows" / "electron" / "renderer" / "index.html"
DOCS = ROOT / "windows" / "README.md"
BUILD_REQUIREMENTS = ROOT / "windows" / "requirements-build.txt"
VOICE_REQUIREMENTS = ROOT / "windows" / "requirements-voice.txt"
CI_WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
PTT_HOTKEY = ROOT / "windows" / "electron" / "ptt-hotkey.ps1"
PTT_INSERT = ROOT / "windows" / "electron" / "ptt-insert.ps1"


def test_windows_client_is_electron_only_and_has_no_tkinter_shell() -> None:
    package = PACKAGE.read_text(encoding="utf-8")
    assert '"electron"' in package
    assert '"electron-builder"' in package
    assert not (ROOT / "windows" / "rnd_workbench.py").exists()


def test_compact_and_full_are_states_of_one_browser_window() -> None:
    source = MAIN.read_text(encoding="utf-8")
    assert source.count("new BrowserWindow(") == 1
    assert "width: 410, height: 420" in source
    assert 'let currentMode = "compact"' in source
    assert "function setWindowMode(mode)" in source
    assert "mainWindow.setBounds" in source
    assert "mainWindow.setAlwaysOnTop" in source
    assert "app.requestSingleInstanceLock()" in source


def test_electron_renderer_is_isolated_from_node_and_remote_content() -> None:
    main = MAIN.read_text(encoding="utf-8")
    preload = PRELOAD.read_text(encoding="utf-8")
    html = HTML.read_text(encoding="utf-8")
    assert "contextIsolation: true" in main
    assert "nodeIntegration: false" in main
    assert "sandbox: true" in main
    assert "contextBridge.exposeInMainWorld" in preload
    assert "connect-src 'none'" in html
    assert '<script src="http' not in html
    assert '<link rel="stylesheet" href="http' not in html


def test_electron_blocks_untrusted_navigation_and_ipc_senders() -> None:
    main = MAIN.read_text(encoding="utf-8")
    assert "setWindowOpenHandler" in main
    assert 'webContents.on("will-navigate"' in main
    assert 'action: "deny"' in main
    assert "function isTrustedRendererEvent(event)" in main
    assert "event.sender === mainWindow.webContents" in main
    assert "event.senderFrame === mainWindow.webContents.mainFrame" in main


def test_voice_ui_is_capability_gated_and_uses_bounded_pcm_contract() -> None:
    source = RENDERER.read_text(encoding="utf-8")
    html = HTML.read_text(encoding="utf-8")
    styles = (ROOT / "windows" / "electron" / "renderer" / "styles.css").read_text(
        encoding="utf-8"
    )
    assert "state.platform.voice_available" in source
    assert "button.disabled = !available" in source
    assert "navigator.mediaDevices.getUserMedia" in source
    assert "echoCancellation: true" in source
    assert "createScriptProcessor(2048, 1, 1)" in source
    assert 'sendCommand("voice_session_start"' in source
    assert 'sendCommand("voice_utterance_start"' in source
    assert 'sendCommand("voice_audio_chunk"' in source
    assert 'sendCommand("voice_utterance_end"' in source
    assert 'sendCommand("voice_cancel"' in source
    assert 'sendCommand("voice_session_stop"' in source
    assert "performance.now() < this.calibrationUntil && !state.speaking" in source
    assert "if (this.candidateMs >= 120) this.beginUtterance(false)" in source
    assert "Faster-Whisper" in source
    assert (
        'body[data-mode="compact"][data-compact-view="voice"] .chat-card { display: none; }'
        in styles
    )


def test_compact_voice_start_is_isolated_from_f8_dictation_state() -> None:
    source = RENDERER.read_text(encoding="utf-8")
    voice_capture = source.split("class VoiceCaptureController", maxsplit=1)[1].split(
        "class PushToTalkDictationController", maxsplit=1
    )[0]

    assert voice_capture.count("generation !== this.startGeneration") == 2
    assert "this.requestId" not in voice_capture
    assert "this.heldRequestId" not in voice_capture
    assert "this.generation" not in voice_capture
    assert "requestId" not in voice_capture
    assert "state.ptt" not in voice_capture
    assert 'sendCommand("ptt_dictation_cancel"' not in voice_capture
    assert "if (context.state !== \"closed\") await context.close()" in voice_capture
    assert "if (generation === this.startGeneration) this.starting = false" in voice_capture


def test_streaming_tts_playback_has_limiter_and_click_free_barge_in_fade() -> None:
    source = RENDERER.read_text(encoding="utf-8")
    assert "createDynamicsCompressor()" in source
    assert "this.limiter.ratio.value = 16" in source
    assert "linearRampToValueAtTime(0, now + 0.012)" in source
    assert "source.stop(stopAt)" in source
    assert 'case "audio_start"' in source
    assert 'case "audio_chunk"' in source
    assert 'case "audio_end"' in source
    assert 'case "audio_cancel"' in source
    assert 'kind: "playback_signal"' in source
    assert 'kind: "playback_cancel_scheduled"' in source
    assert 'kind: "playback_first_audio"' in source
    assert "scheduledAtMs - this.voiceTimingOriginMs" in source
    assert "this.player.setVoiceTimingOrigin(performance.now() - speechTailMs)" in source
    assert "speech_tail_ms: Math.round(speechTailMs)" in source
    assert "reason," in source
    assert 'hardware_measured: false' in source


def test_media_permission_is_audio_only_and_ipc_rejects_large_audio_blocks() -> None:
    source = MAIN.read_text(encoding="utf-8")
    assert "setPermissionCheckHandler" in source
    assert "setPermissionRequestHandler" in source
    assert 'permission !== "media"' in source
    assert 'requested.every((type) => type === "audio")' in source
    assert 'payload.command === "voice_audio_chunk"' in source
    assert "payload.data.length > 96 * 1024" in source
    renderer = RENDERER.read_text(encoding="utf-8")
    assert 'kind: "capture_ready"' in renderer
    assert 'kind: "capture_signal"' in renderer
    assert 'kind: "listen_ready"' in renderer
    assert "performance.now() - requestedAt" in renderer


def test_pilot_voice_metrics_are_visible_and_exported_via_trusted_save_dialog() -> None:
    main = MAIN.read_text(encoding="utf-8")
    preload = PRELOAD.read_text(encoding="utf-8")
    renderer = RENDERER.read_text(encoding="utf-8")
    html = HTML.read_text(encoding="utf-8")

    assert 'ipcMain.handle("pilot:export-metrics"' in main
    assert "dialog.showSaveDialog" in main
    assert 'ipcRenderer.invoke("pilot:export-metrics")' in preload
    assert 'sendCommand("export_pilot_metrics"' in renderer
    assert "state.snapshot.pilot_metrics" in renderer
    assert 'id="pilotMetricsSummary"' in html
    assert 'id="pilotUsageSummary"' in html
    assert 'id="pilotUsefulnessRating"' in html
    assert 'sendCommand("set_pilot_feedback"' in renderer
    assert "без запросов, транскриптов, документов и идентификаторов сессий" in html


def test_dynamic_pilot_preflight_is_rendered_and_can_be_refreshed() -> None:
    renderer = RENDERER.read_text(encoding="utf-8")
    html = HTML.read_text(encoding="utf-8")

    assert "function renderPilotPreflight()" in renderer
    assert "state.snapshot.pilot_preflight" in renderer
    assert 'case "pilot_preflight":' in renderer
    assert 'sendCommand("pilot_preflight")' in renderer
    assert 'id="pilotPreflightOverall"' in html
    assert 'id="pilotPreflightList"' in html


def test_content_free_onboarding_routes_to_existing_windows_flows() -> None:
    renderer = RENDERER.read_text(encoding="utf-8")
    html = HTML.read_text(encoding="utf-8")
    styles = (ROOT / "windows" / "electron" / "renderer" / "styles.css").read_text(
        encoding="utf-8"
    )

    assert "function renderPilotOnboarding()" in renderer
    assert "state.snapshot.pilot_onboarding" in renderer
    assert "function performPilotOnboardingAction()" in renderer
    for action_id in (
        "review_preflight",
        "start_voice",
        "open_chat",
        "show_meeting_import",
        "prepare_briefing",
    ):
        assert f'actionId === "{action_id}"' in renderer
    assert 'composer.value = "/briefing ";' in renderer
    assert 'id="pilotOnboardingButton" type="button" hidden' in html
    assert 'id="pilotOnboardingProgress"' in html
    assert ".pilot-onboarding-card .secondary-button { width: 100%; min-height: 36px;" in styles


def test_windows_full_diagnostics_show_content_free_session_reliability() -> None:
    renderer = RENDERER.read_text(encoding="utf-8")

    assert "usage.observed_session_exits" in renderer
    assert "usage.clean_session_exits" in renderer
    assert "usage.crash_free_session_rate" in renderer
    assert "штатных завершений:" in renderer
    assert "надёжность: ожидает завершений" in renderer


def test_express_sync_control_is_conditional_and_uses_backend_contract() -> None:
    renderer = RENDERER.read_text(encoding="utf-8")
    html = HTML.read_text(encoding="utf-8")

    assert 'id="expressSyncButton"' in html
    assert 'id="expressSyncButton" type="button" hidden' in html
    assert "state.snapshot.express_connector" in renderer
    assert 'sendCommand("sync_express_meetings")' in renderer
    assert 'case "express_sync_completed":' in renderer
    assert 'case "express_sync_error":' in renderer


def test_compact_chat_state_stops_hidden_microphone_capture() -> None:
    source = RENDERER.read_text(encoding="utf-8")
    transition = source.split("function setCompactView(view)", maxsplit=1)[1].split(
        "function setVoicePhase", maxsplit=1
    )[0]
    assert 'if (view === "chat")' in transition
    assert "voiceCapture.active || voiceCapture.starting" in transition
    assert "void voiceCapture.stop()" in transition
    assert 'void pttDictation.cancel("switch_to_chat")' in transition


def test_compact_controls_and_settings_remain_usable_at_410_by_420() -> None:
    renderer = RENDERER.read_text(encoding="utf-8")
    html = HTML.read_text(encoding="utf-8")
    styles = (ROOT / "windows" / "electron" / "renderer" / "styles.css").read_text(
        encoding="utf-8"
    )

    assert 'id="composerInput" rows="1" placeholder="Сообщение…"' in html
    assert 'id="stopButton" class="secondary-button" title="Остановить ответ" hidden' in html
    assert "function setResponsePending(active)" in renderer
    assert 'composer.placeholder = state.mode === "compact"' in renderer
    assert 'setResponsePending(true);' in renderer
    assert renderer.count('setResponsePending(false);') >= 4
    assert '<div class="dialog-fields">' in html
    assert 'dialog { width: min(490px, calc(100vw - 32px)); max-height: calc(100vh - 20px);' in styles
    assert '.dialog-fields { display: grid; gap: 13px; min-height: 0;' in styles
    assert "Полный ответ — в чате. Говорите, чтобы перебить. F8 — диктовка." in html
    assert 'font-size: 11px; line-height: 1.35;' in styles


def test_full_window_wraps_long_task_titles_and_preserves_route_label() -> None:
    renderer = RENDERER.read_text(encoding="utf-8")
    styles = (ROOT / "windows" / "electron" / "renderer" / "styles.css").read_text(
        encoding="utf-8"
    )

    assert 'compact ? "полностью локально" : "локальная модель · данные на устройстве"' in renderer
    assert 'compact ? "корпоративный контур" : "корпоративная модель · защищённый API"' in renderer
    assert "overflow-wrap: anywhere" in styles
    assert "white-space: normal" in styles
    assert 'body[data-mode="full"] .brand-copy span { max-width: 300px; font-size: 10px; }' in styles
    assert '.compact-switch button { min-width: 88px; min-height: 32px;' in styles
    assert '.mode-button { min-height: 32px;' in styles


def test_full_window_uses_vertical_tool_panel_and_collapses_diagnostics() -> None:
    html = HTML.read_text(encoding="utf-8")
    styles = (ROOT / "windows" / "electron" / "renderer" / "styles.css").read_text(
        encoding="utf-8"
    )

    assert '<p class="eyebrow">РАБОЧИЙ КОНТУР</p>' in html
    assert '<h2>Инструменты</h2>' in html
    assert '<details class="pilot-diagnostics">' in html
    assert '<summary>Диагностика пилота</summary>' in html
    assert '<option value="0" selected disabled>Не указана</option>' in html
    assert 'body[data-mode="full"] .full-only { display: block !important; }' in styles
    assert 'body[data-mode="full"] .pilot-panel { display: block !important; }' in styles
    assert 'body[data-mode="full"] .chat-heading { display: flex !important; }' in styles
    assert '.pilot-diagnostics > summary { display: flex; min-height: 32px;' in styles
    assert '.pilot-usage-details summary { display: flex; min-height: 30px;' in styles


def test_global_f8_push_to_talk_is_packaged_and_bridged_without_node_exposure() -> None:
    main = MAIN.read_text(encoding="utf-8")
    preload = PRELOAD.read_text(encoding="utf-8")
    renderer = RENDERER.read_text(encoding="utf-8")
    package = (ROOT / "windows" / "electron" / "package.json").read_text(
        encoding="utf-8"
    )
    hotkey = PTT_HOTKEY.read_text(encoding="utf-8")

    assert 'key: "F8"' in main
    assert "startPttHotkey()" in main
    assert "if (!backendPttAvailable)" in main
    assert "F8 не перехватывается" in main
    assert 'webContents.send("ptt:key"' in main
    assert 'ipcRenderer.on("ptt:key"' in preload
    assert "class PushToTalkDictationController" in renderer
    assert 'sendCommand("ptt_dictation_start"' in renderer
    assert 'sendCommand("ptt_audio_chunk"' in renderer
    assert 'sendCommand("ptt_dictation_end"' in renderer
    assert "WhKeyboardLl = 13" in hotkey
    assert "SetWindowsHookEx" in hotkey
    assert "return new IntPtr(1)" in hotkey
    assert "if (isDown && !f8Down)" in hotkey
    assert "else if (isUp && f8Down)" in hotkey
    assert "PostThreadMessage" in hotkey
    assert "global_exclusive_hold" in hotkey
    assert "swallowed" in hotkey
    assert "foreground_hwnd" in hotkey
    assert "extern short GetAsyncKeyState" not in hotkey
    assert '"-ParentPid"' in main
    assert 'phase: "cancel"' in main
    assert '"ptt-hotkey.ps1"' in package
    assert '"ptt-insert.ps1"' in package


def test_push_to_talk_capability_and_microphone_permission_are_truthful() -> None:
    source = RENDERER.read_text(encoding="utf-8")
    assert 'event.id === "windows_push_to_talk"' in source
    assert 'navigator.permissions.query({ name: "microphone" })' in source
    assert 'state.ptt.permission = "granted"' in source
    assert 'state.ptt.permission = error?.name === "NotAllowedError" ? "denied" : "error"' in source
    assert "state.ptt.hotkeyAvailable && state.ptt.sttAvailable" not in source
    assert "!state.ptt.hotkeyAvailable || !state.ptt.sttAvailable" in source


def test_dictation_insertion_is_clipboard_free_and_rejects_secure_fields() -> None:
    main = MAIN.read_text(encoding="utf-8")
    renderer = RENDERER.read_text(encoding="utf-8")
    helper = PTT_INSERT.read_text(encoding="utf-8")

    combined = main + renderer + helper
    assert "clipboard" not in combined.casefold()
    assert "AutomationElement]::IsPasswordProperty" in helper
    assert "if ($isPassword -eq $true) { exit 6 }" in helper
    assert "SendInput" in helper
    assert "ExpectedForegroundHwnd" in helper
    assert "GetForegroundWindow" in helper
    assert "exit 9" in helper
    assert '"-ExpectedForegroundHwnd"' in main
    assert "targetInfo.releaseHwnd" in main
    assert "code === 9" in main
    assert 'element.type === "password"' in renderer
    assert "fieldLooksSecure" in renderer
    assert "document.hasFocus()" in renderer
    assert "keepPttTextInComposer" in renderer
    assert 'const transcript = String(event.text || "").trim()' in renderer
    assert 'transcript.length <= 20000 ? transcript : ""' in renderer
    assert "text.length > 20000" in main
    backend_event = main.split("function emitBackendEvent(event)", maxsplit=1)[1].split(
        "function writeBackendCommand", maxsplit=1
    )[0]
    assert backend_event.index('webContents.send("backend:event"') < backend_event.index(
        "handlePttDictationResult(event)"
    )


def test_electron_bundles_java_policy_companion_behind_python_ml_bridge() -> None:
    source = MAIN.read_text(encoding="utf-8")
    package = PACKAGE.read_text(encoding="utf-8")
    docs = DOCS.read_text(encoding="utf-8")
    assert "RND_WORKBENCH_CORE_EXECUTABLE" in source
    assert '"backend", "rnd-workbench-backend.exe"' in source
    assert "rnd-workbench-core.exe" not in source
    assert "RND_WORKBENCH_JAVA_CORE_JAVA" in source
    assert "RND_WORKBENCH_JAVA_CORE_LIB_DIR" in source
    assert '"from": "../dist/java-core"' in package
    assert '"from": "../dist/licenses"' in package
    assert "JSONL" in docs
    assert "Java 21 action journal" in docs
    renderer = RENDERER.read_text(encoding="utf-8")
    assert "java_core_policy" in renderer
    assert "javaFallbackNotified" in renderer
    assert "резервная встроенная политика" in renderer


def test_windows_approval_center_exposes_safe_action_state_and_commands() -> None:
    renderer = RENDERER.read_text(encoding="utf-8")
    html = HTML.read_text(encoding="utf-8")
    styles = (ROOT / "windows" / "electron" / "renderer" / "styles.css").read_text(
        encoding="utf-8"
    )

    assert 'id="approvalCount"' in html
    assert 'id="approvalList"' in html
    assert "function renderApprovals()" in renderer
    assert 'sendCommand("resolve_approval"' in renderer
    assert "java_action_journal" in renderer
    assert "сверк" in renderer.casefold()
    assert ".approval-actions" in styles


def test_windows_full_window_imports_express_transcript_or_package_through_trusted_ipc() -> None:
    main = MAIN.read_text(encoding="utf-8")
    preload = PRELOAD.read_text(encoding="utf-8")
    renderer = RENDERER.read_text(encoding="utf-8")
    html = HTML.read_text(encoding="utf-8")

    assert 'dialog.showOpenDialog(mainWindow' in main
    assert 'ipcMain.handle("synapse:choose-package"' in main
    assert "isTrustedRendererEvent(event)" in main
    assert 'properties: ["openFile"]' in main
    assert 'extensions: ["zip"]' in main
    assert 'ipcRenderer.invoke("synapse:choose-package")' in preload
    assert 'ipcMain.handle("meeting:choose-transcript"' in main
    assert 'ipcRenderer.invoke("meeting:choose-transcript")' in preload
    assert 'ipcMain.handle("meeting:choose-audio"' in main
    assert 'ipcRenderer.invoke("meeting:choose-audio")' in preload
    assert 'id="meetingAudioImportButton"' in html
    assert 'id="meetingTranscriptImportButton"' in html
    assert 'id="synapseImportButton"' in html
    assert '>ZIP с контекстом</button>' in html
    assert "аудиозапись, готовый транскрипт или ZIP" in html
    assert 'sendCommand("import_meeting_transcript"' in renderer
    assert 'sendCommand("import_meeting_audio"' in renderer
    assert 'sendCommand("import_synapse_package"' in renderer
    assert 'case "meeting_transcript_imported":' in renderer
    assert 'case "meeting_transcript_import_error":' in renderer
    assert 'case "meeting_audio_imported":' in renderer
    assert 'case "meeting_audio_import_error":' in renderer
    assert 'case "synapse_package_imported":' in renderer
    assert 'case "synapse_package_import_error":' in renderer


def test_renderer_element_references_are_present_and_ids_are_unique() -> None:
    source = RENDERER.read_text(encoding="utf-8")
    html = HTML.read_text(encoding="utf-8")
    identifiers = re.findall(r'\bid="([^"]+)"', html)
    referenced = set(re.findall(r'byId\("([^"]+)"\)', source))
    assert len(identifiers) == len(set(identifiers))
    assert referenced <= set(identifiers)


def test_packaging_includes_the_backend_bridge_and_requires_windows() -> None:
    package = (ROOT / "windows" / "electron" / "package.json").read_text(encoding="utf-8")
    build = (ROOT / "windows" / "build.ps1").read_text(encoding="utf-8")
    assert "rnd-workbench-backend.exe" in package
    assert '"portable"' in package
    assert '$env:OS -ne "Windows_NT"' in build


def test_windows_build_uses_isolated_pinned_non_mlx_dependencies() -> None:
    build = (ROOT / "windows" / "build.ps1").read_text(encoding="utf-8")
    package = PACKAGE.read_text(encoding="utf-8")
    base = BUILD_REQUIREMENTS.read_text(encoding="utf-8")
    voice = VOICE_REQUIREMENTS.read_text(encoding="utf-8")
    assert "numpy==2.2.6" in base
    assert "pyinstaller==6.15.0" in base
    assert "faster-whisper==1.2.0" in voice
    assert "requests==2.34.2" in voice
    package_lines = "\n".join(
        line for line in (base + voice).splitlines() if not line.lstrip().startswith("#")
    )
    assert "mlx" not in package_lines.casefold()
    assert "[switch]$WithVoice" in build
    assert 'Assert-PythonPackageVersion -Package "faster-whisper"' in build
    assert "RND_WORKBENCH_WINDOWS_WHISPER_MODEL" not in build
    assert "RND_WORKBENCH_WINDOWS_OMNIVOICE_URL" not in build
    assert '"--collect-all", "faster_whisper"' in build
    assert "clean test installDist" in build
    assert "JDK 21 jlink is required" in build
    assert "verify_java_core_bridge.py" in build
    assert "THIRD_PARTY_NOTICES.md" in build
    assert '"../dist/java-core"' in package
    assert "onnxruntime" not in build
    assert "pip install" not in build
    assert "& $NodePackageManager ci" in build

    workflow = CI_WORKFLOW.read_text(encoding="utf-8")
    assert "windows/requirements-voice.txt" in workflow
    assert "windows/build.ps1 -Python python -WithVoice" in workflow
    assert '"command":"voice_dependency_probe"' in workflow
    assert '"command":"core_policy_probe"' in workflow
    assert '"command":"core_action_journal_probe"' in workflow
    assert "Java core policy probe:" in workflow
    assert "Java action journal probe:" in workflow
    assert "Voice dependency probe:" in workflow
    assert "RnD-Workbench-Windows-voice-ready-unsigned-QA" in workflow


def test_readiness_matrix_does_not_claim_unimplemented_voice_or_connectors() -> None:
    docs = DOCS.read_text(encoding="utf-8")
    assert "Windows" in docs
    assert "| Jira / Kaiten / Confluence / почта / календарь | Не подключено |" in docs
    assert "| Подписанный установщик | Не проверено |" in docs
