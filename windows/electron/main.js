"use strict";

const { app, BrowserWindow, dialog, ipcMain, screen } = require("electron");
const { spawn } = require("node:child_process");
const { randomUUID } = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");
const readline = require("node:readline");

const COMPACT_SIZE = Object.freeze({ width: 410, height: 420 });
const FULL_SIZE = Object.freeze({ width: 1120, height: 760 });

let mainWindow = null;
let backendProcess = null;
let backendReady = false;
let backendPttAvailable = false;
let currentMode = "compact";
let pendingCommands = [];
let pttHotkeyProcess = null;
let activePttPress = null;
const pttTargets = new Map();
let pttHotkeyCapability = {
  available: false,
  status: process.platform === "win32" ? "waiting_for_stt" : "unsupported",
  key: "F8",
  detail: process.platform === "win32"
    ? "F8 не перехватывается: ожидаю готовность локального STT"
    : "Глобальная push-to-talk диктовка доступна только в Windows",
};

function projectDirectory() {
  return path.resolve(__dirname, "..", "..");
}

function pttHelperPath(name) {
  return app.isPackaged
    ? path.join(process.resourcesPath, "ptt", name)
    : path.join(__dirname, name);
}

function windowsPowerShell() {
  const windowsDirectory = process.env.SystemRoot || process.env.WINDIR || "C:\\Windows";
  return path.join(
    windowsDirectory,
    "System32",
    "WindowsPowerShell",
    "v1.0",
    "powershell.exe",
  );
}

function emitPttHotkeyCapability() {
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.webContents.send("ptt:capability", { ...pttHotkeyCapability });
  }
}

function setPttHotkeyCapability(status, detail) {
  pttHotkeyCapability = {
    available: status === "available",
    status,
    key: "F8",
    detail,
  };
  emitPttHotkeyCapability();
}

function handlePttHotkeyEvent(event) {
  if (!event || event.key !== "F8" || !["down", "up"].includes(event.phase)) return;
  if (event.phase === "down") {
    if (activePttPress) return;
    const requestId = randomUUID();
    const target = mainWindow?.isFocused() ? "renderer" : "system";
    activePttPress = { requestId, target, releaseHwnd: null };
    pttTargets.set(requestId, activePttPress);
    mainWindow?.webContents.send("ptt:key", { phase: "down", key: "F8", requestId });
    return;
  }
  if (!activePttPress) return;
  const released = activePttPress;
  activePttPress = null;
  const foregroundHwnd = Number(event.foreground_hwnd);
  released.releaseHwnd = Number.isSafeInteger(foregroundHwnd) && foregroundHwnd > 0
    ? foregroundHwnd
    : null;
  pttTargets.set(released.requestId, released);
  mainWindow?.webContents.send("ptt:key", {
    phase: "up",
    key: "F8",
    requestId: released.requestId,
  });
}

function startPttHotkey() {
  if (process.platform !== "win32") {
    emitPttHotkeyCapability();
    return;
  }
  if (!backendPttAvailable) {
    setPttHotkeyCapability(
      "not_available",
      "F8 не перехватывается: локальный Faster-Whisper ещё не готов",
    );
    return;
  }
  if (pttHotkeyProcess && pttHotkeyProcess.exitCode === null) return;
  const helper = pttHelperPath("ptt-hotkey.ps1");
  const powershell = windowsPowerShell();
  if (!fs.existsSync(helper) || !fs.existsSync(powershell)) {
    setPttHotkeyCapability(
      "not_available",
      "Не найден локальный Windows helper для удерживаемой F8",
    );
    return;
  }
  try {
    pttHotkeyProcess = spawn(
      powershell,
      [
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        helper,
        "-ParentPid",
        String(process.pid),
      ],
      { windowsHide: true, shell: false, stdio: ["ignore", "pipe", "pipe"] },
    );
  } catch (error) {
    setPttHotkeyCapability(
      "not_available",
      `Не удалось запустить системную F8 (${error.name})`,
    );
    return;
  }
  const processInstance = pttHotkeyProcess;
  const lines = readline.createInterface({ input: processInstance.stdout, crlfDelay: Infinity });
  lines.on("line", (line) => {
    if (pttHotkeyProcess !== processInstance) return;
    let event;
    try { event = JSON.parse(line); } catch (_error) { return; }
    if (event?.type === "ready" && event.key === "F8") {
      if (event.mode === "global_exclusive_hold" && event.swallowed === true) {
        setPttHotkeyCapability(
          "available",
          "Удерживайте F8: клавиша перехватывается только на время локальной диктовки",
        );
      } else {
        setPttHotkeyCapability(
          "not_available",
          "Системный helper не подтвердил безопасный перехват F8",
        );
        if (processInstance.exitCode === null) processInstance.kill();
      }
      return;
    }
    if (event?.type === "key") handlePttHotkeyEvent(event);
  });
  // Never surface helper stderr; it can contain local paths.
  processInstance.stderr.on("data", () => {});
  processInstance.on("error", (error) => {
    if (pttHotkeyProcess !== processInstance) return;
    setPttHotkeyCapability("not_available", `Системная F8 недоступна (${error.name})`);
  });
  processInstance.on("exit", () => {
    if (pttHotkeyProcess !== processInstance) return;
    pttHotkeyProcess = null;
    if (activePttPress) {
      mainWindow?.webContents.send("ptt:key", {
        phase: "cancel",
        key: "F8",
        requestId: activePttPress.requestId,
      });
      activePttPress = null;
    }
    if (!app.isQuitting) {
      setPttHotkeyCapability("not_available", "Системный helper F8 остановлен");
    }
  });
}

function stopPttHotkey(detail = "") {
  const processToStop = pttHotkeyProcess;
  pttHotkeyProcess = null;
  if (activePttPress) {
    mainWindow?.webContents.send("ptt:key", {
      phase: "cancel",
      key: "F8",
      requestId: activePttPress.requestId,
    });
  }
  activePttPress = null;
  if (processToStop && processToStop.exitCode === null) processToStop.kill();
  if (detail && !app.isQuitting) setPttHotkeyCapability("not_available", detail);
}

function backendLaunchSpec() {
  const dataDirectory = process.env.LOCALAPPDATA
    ? path.join(process.env.LOCALAPPDATA, "RnD Workbench")
    : app.getPath("userData");
  const dataPath = path.join(dataDirectory, "assistant.sqlite3");
  const explicitCore = process.env.RND_WORKBENCH_CORE_EXECUTABLE;
  if (explicitCore) {
    return { command: explicitCore, args: ["--data", dataPath], kind: "configured-core" };
  }

  if (app.isPackaged) {
    // The Python JSONL process owns ML/audio and starts the bundled Java 21
    // companion for metadata-only policy decisions. Electron still speaks to
    // one backend process, so the renderer never receives a Java classpath.
    return {
      command: path.join(process.resourcesPath, "backend", "rnd-workbench-backend.exe"),
      args: ["--data", dataPath],
      kind: "python-bridge",
    };
  }

  const configuredPython = process.env.RND_WORKBENCH_PYTHON;
  const windowsVirtualPython = path.join(projectDirectory(), ".venv", "Scripts", "python.exe");
  const posixVirtualPython = path.join(projectDirectory(), ".venv", "bin", "python");
  const virtualPython = fs.existsSync(windowsVirtualPython)
    ? windowsVirtualPython
    : fs.existsSync(posixVirtualPython)
      ? posixVirtualPython
      : null;
  const command = configuredPython || virtualPython || (process.platform === "win32" ? "py" : "python3");
  const commandName = path.basename(command).toLowerCase();
  const pythonArgs = commandName === "py" || commandName === "py.exe" ? ["-3.11"] : [];
  return {
    command,
    args: [
      ...pythonArgs,
      path.join(projectDirectory(), "windows", "backend_entry.py"),
      "--data",
      dataPath,
    ],
    kind: "python-bridge",
  };
}

function backendEnvironment() {
  const environment = { ...process.env, PYTHONUTF8: "1", PYTHONUNBUFFERED: "1" };
  if (app.isPackaged) {
    environment.RND_WORKBENCH_JAVA_CORE_JAVA = path.join(
      process.resourcesPath,
      "java-core",
      "runtime",
      "bin",
      "java.exe",
    );
    environment.RND_WORKBENCH_JAVA_CORE_LIB_DIR = path.join(
      process.resourcesPath,
      "java-core",
      "lib",
    );
  }
  return environment;
}

function createMainWindow() {
  mainWindow = new BrowserWindow({
    width: COMPACT_SIZE.width,
    height: COMPACT_SIZE.height,
    minWidth: 380,
    minHeight: 390,
    show: false,
    frame: false,
    transparent: false,
    backgroundColor: "#F4F7FB",
    alwaysOnTop: true,
    resizable: false,
    maximizable: false,
    fullscreenable: false,
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      spellcheck: true,
    },
  });

  mainWindow.removeMenu();
  mainWindow.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
  mainWindow.webContents.on("will-navigate", (event, targetUrl) => {
    const currentUrl = mainWindow?.webContents.getURL();
    if (!currentUrl || targetUrl !== currentUrl) event.preventDefault();
  });
  const rendererSession = mainWindow.webContents.session;
  const trustedRenderer = (webContents) => Boolean(
    mainWindow
    && !mainWindow.isDestroyed()
    && webContents === mainWindow.webContents
    && webContents.getURL().startsWith("file://"),
  );
  rendererSession.setPermissionCheckHandler((webContents, permission, _origin, details) => {
    if (!trustedRenderer(webContents) || permission !== "media") return false;
    const requested = Array.isArray(details?.mediaTypes) ? details.mediaTypes : [];
    return requested.length === 0 || requested.every((type) => type === "audio");
  });
  rendererSession.setPermissionRequestHandler((webContents, permission, callback, details) => {
    const requested = Array.isArray(details?.mediaTypes) ? details.mediaTypes : [];
    callback(
      trustedRenderer(webContents)
      && permission === "media"
      && requested.length > 0
      && requested.every((type) => type === "audio"),
    );
  });
  mainWindow.loadFile(path.join(__dirname, "renderer", "index.html"));
  mainWindow.webContents.on("did-finish-load", emitPttHotkeyCapability);
  mainWindow.once("ready-to-show", () => {
    placeCompactWindow();
    mainWindow.show();
  });
  mainWindow.on("closed", () => {
    mainWindow = null;
  });
}

function placeCompactWindow() {
  if (!mainWindow) return;
  const display = screen.getDisplayNearestPoint(screen.getCursorScreenPoint());
  const area = display.workArea;
  mainWindow.setBounds({
    x: area.x + area.width - COMPACT_SIZE.width - 18,
    y: area.y + area.height - COMPACT_SIZE.height - 18,
    ...COMPACT_SIZE,
  });
}

function setWindowMode(mode) {
  if (!mainWindow || !["compact", "full"].includes(mode) || mode === currentMode) return;
  currentMode = mode;
  if (mode === "compact") {
    mainWindow.setMinimumSize(380, 390);
    mainWindow.setResizable(false);
    mainWindow.setAlwaysOnTop(true, "floating");
    placeCompactWindow();
  } else {
    mainWindow.setAlwaysOnTop(false);
    mainWindow.setResizable(true);
    mainWindow.setMinimumSize(860, 600);
    const display = screen.getDisplayMatching(mainWindow.getBounds());
    const area = display.workArea;
    const width = Math.min(FULL_SIZE.width, area.width - 40);
    const height = Math.min(FULL_SIZE.height, area.height - 40);
    mainWindow.setBounds({
      x: area.x + Math.max(20, Math.round((area.width - width) / 2)),
      y: area.y + Math.max(20, Math.round((area.height - height) / 2)),
      width,
      height,
    });
  }
  mainWindow.webContents.send("window:mode", currentMode);
}

function startBackend() {
  const spec = backendLaunchSpec();
  try {
    backendProcess = spawn(spec.command, spec.args, {
      cwd: projectDirectory(),
      windowsHide: true,
      stdio: ["pipe", "pipe", "pipe"],
      shell: false,
      env: backendEnvironment(),
    });
  } catch (error) {
    emitBackendEvent({ type: "fatal", message: `Не удалось запустить core (${error.name})` });
    return;
  }

  const lines = readline.createInterface({ input: backendProcess.stdout, crlfDelay: Infinity });
  lines.on("line", (line) => {
    try {
      const event = JSON.parse(line);
      if (!event || typeof event !== "object" || Array.isArray(event)) return;
      if (event.type === "ready") {
        backendReady = true;
        const queued = pendingCommands;
        pendingCommands = [];
        queued.forEach(writeBackendCommand);
      }
      emitBackendEvent({ ...event, bridge_kind: spec.kind });
    } catch (_error) {
      // Stdout is a protocol channel. Non-JSON diagnostic lines are ignored.
    }
  });

  // Consume stderr to prevent a blocked child. Provider diagnostics and local
  // paths are deliberately not copied into renderer-visible events.
  backendProcess.stderr.on("data", () => {});
  backendProcess.on("error", (error) => {
    emitBackendEvent({ type: "fatal", message: `Core недоступен (${error.name})` });
  });
  backendProcess.on("exit", (code) => {
    backendReady = false;
    backendPttAvailable = false;
    stopPttHotkey("F8 не перехватывается: core локальной диктовки остановлен");
    if (code !== 0 && !app.isQuitting) {
      emitBackendEvent({ type: "fatal", message: "Core RnD Workbench завершился с ошибкой" });
    }
  });
}

function emitPttInsertionResult(requestId, success, detail, target) {
  pttTargets.delete(requestId);
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.webContents.send("ptt:insertion", {
      requestId,
      success: Boolean(success),
      detail,
      target,
    });
  }
}

function insertPttTextIntoSystemField(requestId, text, expectedForegroundHwnd) {
  if (process.platform !== "win32") {
    emitPttInsertionResult(
      requestId,
      false,
      "Системная вставка доступна только в Windows",
      "system",
    );
    return;
  }
  if (!Number.isSafeInteger(expectedForegroundHwnd) || expectedForegroundHwnd <= 0) {
    emitPttInsertionResult(
      requestId,
      false,
      "Не удалось зафиксировать активное окно; текст не вставлен",
      "system",
    );
    return;
  }
  const helper = pttHelperPath("ptt-insert.ps1");
  const powershell = windowsPowerShell();
  if (!fs.existsSync(helper) || !fs.existsSync(powershell)) {
    emitPttInsertionResult(
      requestId,
      false,
      "Не найден локальный helper вставки",
      "system",
    );
    return;
  }
  let processToInsert;
  try {
    processToInsert = spawn(
      powershell,
      [
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        helper,
        "-ExpectedForegroundHwnd",
        String(expectedForegroundHwnd),
      ],
      { windowsHide: true, shell: false, stdio: ["pipe", "ignore", "ignore"] },
    );
  } catch (error) {
    emitPttInsertionResult(
      requestId,
      false,
      `Не удалось запустить локальную вставку (${error.name})`,
      "system",
    );
    return;
  }
  let settled = false;
  const finish = (success, detail) => {
    if (settled) return;
    settled = true;
    clearTimeout(timeout);
    emitPttInsertionResult(requestId, success, detail, "system");
  };
  const timeout = setTimeout(() => {
    if (processToInsert.exitCode === null) processToInsert.kill();
    finish(false, "Системная вставка не завершилась за 5 секунд");
  }, 5000);
  processToInsert.on("error", (error) => {
    finish(false, `Системная вставка недоступна (${error.name})`);
  });
  processToInsert.on("exit", (code) => {
    const failureDetail = code === 6
      ? "В защищённые поля диктовка не вставляется"
      : code === 9
        ? "Фокус изменился; текст не вставлен"
      : code === 5 || code === 7
        ? "Нет активного текстового поля"
        : `Активное поле отклонило вставку (код ${code})`;
    finish(
      code === 0,
      code === 0 ? "Диктовка вставлена в активное поле" : failureDetail,
    );
  });
  processToInsert.stdin.end(text, "utf8");
}

function handlePttDictationResult(event) {
  const requestId = typeof event.request_id === "string" ? event.request_id : "";
  const text = typeof event.text === "string" ? event.text.trim() : "";
  const targetInfo = pttTargets.get(requestId);
  const target = targetInfo?.target;
  if (!requestId || !text || text.length > 20000 || !targetInfo) {
    if (requestId) {
      emitPttInsertionResult(
        requestId,
        false,
        "Результат диктовки не связан с активным нажатием F8",
        target || "unknown",
      );
    }
    return;
  }
  if (target === "renderer") {
    if (!mainWindow || mainWindow.isDestroyed() || !mainWindow.isFocused()) {
      emitPttInsertionResult(
        requestId,
        false,
        "Фокус изменился; текст не вставлен",
        "renderer",
      );
      return;
    }
    mainWindow?.webContents.send("ptt:insert", { requestId, text });
    return;
  }
  insertPttTextIntoSystemField(requestId, text, targetInfo.releaseHwnd);
}

function emitBackendEvent(event) {
  if (event?.type === "capability" && event.id === "windows_push_to_talk") {
    backendPttAvailable = event.available === true;
    if (backendPttAvailable) {
      startPttHotkey();
    } else {
      stopPttHotkey("F8 не перехватывается: локальный Faster-Whisper не готов");
    }
  }
  if (
    event?.type === "dictation_state"
    && ["ignored", "error", "unavailable", "cancelled", "busy"].includes(event.state)
    && typeof event.request_id === "string"
  ) {
    pttTargets.delete(event.request_id);
  }
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.webContents.send("backend:event", event);
  }
  // Deliver the transcript to renderer state before asking either insertion
  // path to act. If focus changed, the same text can then be retained safely
  // in the RnD Workbench composer instead of being lost.
  if (event?.type === "dictation_result") handlePttDictationResult(event);
}

function writeBackendCommand(payload) {
  if (!backendProcess || backendProcess.killed || !backendProcess.stdin.writable) {
    emitBackendEvent({ type: "fatal", message: "Нет соединения с core" });
    return;
  }
  let serialized;
  try {
    serialized = JSON.stringify(payload);
  } catch (_error) {
    emitBackendEvent({ type: "error", message: "Команда не может быть сериализована" });
    return;
  }
  if (Buffer.byteLength(serialized, "utf8") > 2 * 1024 * 1024) {
    emitBackendEvent({ type: "error", message: "Команда превышает допустимый размер" });
    return;
  }
  backendProcess.stdin.write(`${serialized}\n`, "utf8");
}

function sendBackendCommand(payload) {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) return;
  if (typeof payload.command !== "string" || payload.command.length > 80) return;
  if (
    (payload.command === "voice_audio_chunk" || payload.command === "ptt_audio_chunk")
    && (typeof payload.data !== "string" || payload.data.length > 96 * 1024)
  ) {
    emitBackendEvent({ type: "error", message: "Некорректный аудиоблок" });
    return;
  }
  if (payload.command === "ptt_dictation_cancel" && typeof payload.request_id === "string") {
    const targetInfo = pttTargets.get(payload.request_id);
    if (targetInfo) {
      emitPttInsertionResult(
        payload.request_id,
        false,
        "Диктовка F8 отменена до вставки",
        targetInfo.target,
      );
    }
  }
  if (!backendReady && payload.command !== "quit") {
    if (pendingCommands.length >= 100) pendingCommands.shift();
    pendingCommands.push(payload);
    return;
  }
  writeBackendCommand(payload);
}

function stopBackend() {
  if (!backendProcess || backendProcess.killed) return;
  try {
    writeBackendCommand({ command: "quit" });
    backendProcess.stdin.end();
  } catch (_error) {
    backendProcess.kill();
  }
  const processToStop = backendProcess;
  setTimeout(() => {
    if (processToStop.exitCode === null) processToStop.kill();
  }, 1500).unref();
}

function isTrustedRendererEvent(event) {
  return Boolean(
    mainWindow &&
      !mainWindow.isDestroyed() &&
      event.sender === mainWindow.webContents &&
      event.senderFrame === mainWindow.webContents.mainFrame,
  );
}

ipcMain.on("window:set-mode", (event, mode) => {
  if (isTrustedRendererEvent(event)) setWindowMode(mode);
});
ipcMain.on("window:minimize", (event) => {
  if (isTrustedRendererEvent(event)) mainWindow?.minimize();
});
ipcMain.on("window:close", (event) => {
  if (isTrustedRendererEvent(event)) mainWindow?.close();
});
ipcMain.on("backend:command", (event, payload) => {
  if (isTrustedRendererEvent(event)) sendBackendCommand(payload);
});
ipcMain.handle("synapse:choose-package", async (event) => {
  if (!isTrustedRendererEvent(event) || !mainWindow) return null;
  const selection = await dialog.showOpenDialog(mainWindow, {
    title: "Импортировать ZIP встречи из eXpress (Синапс)",
    buttonLabel: "Импортировать",
    properties: ["openFile"],
    filters: [
      { name: "ZIP-пакет встречи", extensions: ["zip"] },
    ],
  });
  if (selection.canceled || selection.filePaths.length !== 1) return null;
  return selection.filePaths[0];
});
ipcMain.handle("meeting:choose-transcript", async (event) => {
  if (!isTrustedRendererEvent(event) || !mainWindow) return null;
  const selection = await dialog.showOpenDialog(mainWindow, {
    title: "Импортировать готовый транскрипт eXpress",
    buttonLabel: "Импортировать",
    properties: ["openFile"],
    filters: [
      { name: "Транскрипт встречи", extensions: ["txt", "md", "markdown", "csv", "tsv", "json", "log", "xml", "docx", "pdf"] },
    ],
  });
  if (selection.canceled || selection.filePaths.length !== 1) return null;
  return selection.filePaths[0];
});
ipcMain.handle("meeting:choose-audio", async (event) => {
  if (!isTrustedRendererEvent(event) || !mainWindow) return null;
  const selection = await dialog.showOpenDialog(mainWindow, {
    title: "Добавить аудиозапись встречи eXpress",
    buttonLabel: "Добавить",
    properties: ["openFile"],
    filters: [
      { name: "Аудиозапись встречи", extensions: ["aac", "flac", "m4a", "mp3", "mp4", "ogg", "opus", "wav", "webm"] },
    ],
  });
  if (selection.canceled || selection.filePaths.length !== 1) return null;
  return selection.filePaths[0];
});
ipcMain.handle("pilot:export-metrics", async (event) => {
  if (!isTrustedRendererEvent(event) || !mainWindow) return null;
  const selection = await dialog.showSaveDialog(mainWindow, {
    title: "Экспортировать обезличенную сводку качества",
    buttonLabel: "Сохранить",
    defaultPath: path.join(app.getPath("documents"), "RnD-Workbench-pilot-metrics.json"),
    filters: [{ name: "JSON", extensions: ["json"] }],
  });
  if (selection.canceled || !selection.filePath) return null;
  return selection.filePath;
});
ipcMain.on("ptt:renderer-insertion-result", (event, payload) => {
  if (!isTrustedRendererEvent(event) || !payload || typeof payload !== "object") return;
  const requestId = typeof payload.requestId === "string" ? payload.requestId : "";
  if (!requestId || pttTargets.get(requestId)?.target !== "renderer") return;
  const success = payload.success === true;
  const detail = typeof payload.detail === "string"
    ? payload.detail.slice(0, 300)
    : success ? "Диктовка вставлена в активное поле" : "Нет активного текстового поля";
  emitPttInsertionResult(requestId, success, detail, "renderer");
});

const lock = app.requestSingleInstanceLock();
if (!lock) {
  app.quit();
} else {
  app.on("second-instance", () => {
    if (!mainWindow) return;
    if (mainWindow.isMinimized()) mainWindow.restore();
    mainWindow.show();
    mainWindow.focus();
  });
  app.whenReady().then(() => {
    createMainWindow();
    startBackend();
  });
  app.on("activate", () => {
    if (!mainWindow) createMainWindow();
  });
}

app.on("before-quit", () => {
  app.isQuitting = true;
  stopPttHotkey();
  stopBackend();
});

app.on("window-all-closed", () => app.quit());
