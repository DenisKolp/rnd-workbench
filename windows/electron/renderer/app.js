"use strict";

const VOICE_SAMPLE_RATE = 16000;

const state = {
  mode: "compact",
  compactView: "voice",
  snapshot: {},
  runtime: {},
  platform: { voice_available: false },
  streamingText: "",
  listening: false,
  speaking: false,
  voicePhase: "idle",
  responsePending: false,
  metric: "Готов к работе",
  toastTimer: null,
  meetingImporting: false,
  meetingImportKind: "",
  voiceQualificationRunning: false,
  javaFallbackNotified: false,
  artifactHistory: null,
  composerSuggestionIndex: 0,
  ptt: {
    sttAvailable: false,
    sttDetail: "Faster-Whisper не настроен",
    hotkeyAvailable: false,
    hotkeyDetail: "Системная F8 ещё не проверена",
    permission: "unknown",
    captureVerified: false,
    phase: "idle",
    pendingText: "",
  },
};

const byId = (id) => document.getElementById(id);

function sendCommand(command, payload = {}) {
  window.rndWorkbench.sendCommand({ command, ...payload });
}

function setMeetingImporting(active, kind = state.meetingImportKind) {
  state.meetingImporting = Boolean(active);
  const activeKind = String(kind || "package");
  if (state.meetingImporting) {
    state.meetingImportKind = activeKind;
    const addMenu = byId("meetingAddMenu");
    if (addMenu) addMenu.open = false;
  }
  const audioButton = byId("meetingAudioImportButton");
  const transcriptButton = byId("meetingTranscriptImportButton");
  const packageButton = byId("synapseImportButton");
  const expressButton = byId("expressSyncButton");
  if (audioButton) {
    audioButton.disabled = state.meetingImporting || !state.ptt.sttAvailable;
    audioButton.textContent = state.meetingImporting && activeKind === "audio"
      ? "Распознаю аудио…"
      : "Аудиозапись";
    audioButton.title = state.ptt.sttAvailable
      ? "Распознать запись локальным Faster-Whisper"
      : state.ptt.sttDetail;
  }
  if (transcriptButton) {
    transcriptButton.disabled = state.meetingImporting;
    transcriptButton.textContent = state.meetingImporting && activeKind === "transcript"
      ? "Импортирую транскрипт…"
      : "Транскрипт";
  }
  if (packageButton) {
    packageButton.disabled = state.meetingImporting;
    packageButton.textContent = state.meetingImporting && activeKind === "package"
      ? "Проверяю пакет…"
      : "ZIP с контекстом";
  }
  if (expressButton) {
    expressButton.disabled = state.meetingImporting;
    expressButton.textContent = state.meetingImporting && activeKind === "express"
      ? "Получаю встречи…"
      : "Получить из eXpress";
  }
  if (!state.meetingImporting) state.meetingImportKind = "";
}

function setMode(mode) {
  if (!new Set(["compact", "full"]).has(mode)) return;
  state.mode = mode;
  document.body.dataset.mode = mode;
  const modeButton = byId("modeButton");
  modeButton.textContent = mode === "compact" ? "Развернуть" : "Виджет";
  modeButton.setAttribute(
    "aria-label",
    mode === "compact" ? "Развернуть в полное окно" : "Перейти в компактный виджет",
  );
  updateComposerPresentation();
  renderRuntime();
}

function requestModeToggle() {
  const next = state.mode === "compact" ? "full" : "compact";
  setMode(next);
  window.rndWorkbench.setWindowMode(next);
}

function setCompactView(view) {
  if (!new Set(["voice", "chat"]).has(view)) return;
  if (view === "chat") {
    // "Чат" is a text-only compact state: a microphone must never remain
    // active behind the hidden voice panel.
    if (voiceCapture.active || voiceCapture.starting) void voiceCapture.stop();
    if (pttDictation.recording || pttDictation.starting) {
      void pttDictation.cancel("switch_to_chat");
    }
  }
  state.compactView = view;
  document.body.dataset.compactView = view;
  byId("voiceTab").classList.toggle("active", view === "voice");
  byId("chatTab").classList.toggle("active", view === "chat");
  updateComposerPresentation();
}

function updateComposerPresentation() {
  const composer = byId("composerInput");
  if (composer) {
    composer.placeholder = state.mode === "compact"
      ? "Сообщение…"
      : "Поставьте задачу или задайте вопрос…";
  }
}

function setResponsePending(active) {
  state.responsePending = Boolean(active);
  byId("stopButton").hidden = !state.responsePending;
  byId("sendButton").hidden = state.responsePending;
}

function setVoicePhase(phase, detail = "") {
  state.voicePhase = phase;
  document.body.dataset.voiceState = phase;
  if (detail) byId("voiceStatus").textContent = detail;
}

function textNode(tag, className, value) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  node.textContent = value;
  return node;
}

const CLASSIFICATION_LABELS = Object.freeze({
  public: "Публичные",
  internal: "Внутренние",
  confidential: "Конфиденциальные",
  restricted: "Ограниченные",
});

function normalizedClassification(value) {
  const normalized = String(value || "internal").toLowerCase();
  return Object.hasOwn(CLASSIFICATION_LABELS, normalized) ? normalized : "internal";
}

function classificationBadge(value) {
  const classification = normalizedClassification(value);
  return textNode(
    "span",
    `classification-badge ${classification}`,
    CLASSIFICATION_LABELS[classification],
  );
}

function decodedMetadata(value) {
  if (value && typeof value === "object" && !Array.isArray(value)) return value;
  if (typeof value !== "string" || !value.trim()) return {};
  try {
    const parsed = JSON.parse(value);
    return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed : {};
  } catch (_error) {
    return {};
  }
}

function requestSourceFragment(source) {
  if (!source || typeof source !== "object" || !source.id) return;
  const payload = { source_id: String(source.id) };
  if (Number.isInteger(source.char_start) && Number.isInteger(source.char_end)) {
    payload.char_start = source.char_start;
    payload.char_end = source.char_end;
  }
  if (typeof source.chunk_id === "string" && source.chunk_id) {
    payload.chunk_id = source.chunk_id;
  }
  sendCommand("source_fragment", payload);
}

function floatToPcm16(samples) {
  const output = new Int16Array(samples.length);
  for (let index = 0; index < samples.length; index += 1) {
    const sample = Math.max(-1, Math.min(1, samples[index]));
    output[index] = sample < 0 ? Math.round(sample * 32768) : Math.round(sample * 32767);
  }
  return output;
}

function resampleMono(samples, inputRate, outputRate) {
  if (inputRate === outputRate) return samples.slice();
  const outputLength = Math.max(1, Math.round(samples.length * outputRate / inputRate));
  const output = new Float32Array(outputLength);
  const ratio = inputRate / outputRate;
  for (let index = 0; index < outputLength; index += 1) {
    const position = index * ratio;
    const left = Math.min(samples.length - 1, Math.floor(position));
    const right = Math.min(samples.length - 1, left + 1);
    const fraction = position - left;
    output[index] = samples[left] + (samples[right] - samples[left]) * fraction;
  }
  return output;
}

function pcmToBase64(samples) {
  const bytes = new Uint8Array(samples.buffer, samples.byteOffset, samples.byteLength);
  let binary = "";
  for (let index = 0; index < bytes.length; index += 1) {
    binary += String.fromCharCode(bytes[index]);
  }
  return window.btoa(binary);
}

function decodePcm16(encoded) {
  const binary = window.atob(encoded);
  if (!binary.length || binary.length % 2 !== 0 || binary.length > 64 * 1024) {
    throw new Error("Invalid PCM16 block");
  }
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) {
    bytes[index] = binary.charCodeAt(index);
  }
  return new Int16Array(bytes.buffer);
}

class PcmAudioPlayer {
  constructor() {
    this.context = null;
    this.masterGain = null;
    this.limiter = null;
    this.sources = new Set();
    this.sampleRate = 24000;
    this.expectedSequence = 0;
    this.nextStartTime = 0;
    this.ended = true;
    this.peak = 0;
    this.clippedSamples = 0;
    this.totalSamples = 0;
    this.voiceTimingOriginMs = null;
    this.firstAudioReported = false;
  }

  setVoiceTimingOrigin(originMs) {
    this.voiceTimingOriginMs = Number.isFinite(originMs) ? originMs : null;
    this.firstAudioReported = false;
  }

  attachContext(context) {
    this.context = context;
    this.masterGain = context.createGain();
    this.masterGain.gain.value = 0.92;
    this.limiter = context.createDynamicsCompressor();
    this.limiter.threshold.value = -3;
    this.limiter.knee.value = 2;
    this.limiter.ratio.value = 16;
    this.limiter.attack.value = 0.003;
    this.limiter.release.value = 0.06;
    this.masterGain.connect(this.limiter);
    this.limiter.connect(context.destination);
  }

  start(event) {
    const voiceTimingOriginMs = this.voiceTimingOriginMs;
    this.stop("", true);
    this.voiceTimingOriginMs = voiceTimingOriginMs;
    this.firstAudioReported = false;
    const sampleRate = Number(event.sample_rate);
    if (!Number.isInteger(sampleRate) || sampleRate < 8000 || sampleRate > 48000) {
      throw new Error("Unsupported TTS sample rate");
    }
    this.sampleRate = sampleRate;
    this.expectedSequence = 0;
    this.peak = 0;
    this.clippedSamples = 0;
    this.totalSamples = 0;
    this.nextStartTime = this.context ? this.context.currentTime + 0.035 : 0;
    this.ended = false;
    if (this.context && this.masterGain) {
      const now = this.context.currentTime;
      this.masterGain.gain.cancelScheduledValues(now);
      this.masterGain.gain.setValueAtTime(Math.max(0, this.masterGain.gain.value), now);
      this.masterGain.gain.linearRampToValueAtTime(0.92, now + 0.008);
    }
    state.speaking = true;
    setVoicePhase("speaking", "Отвечаю… Скажите что-нибудь, чтобы перебить.");
  }

  push(event) {
    if (!this.context || this.ended) return;
    const sequence = Number(event.sequence);
    if (sequence !== this.expectedSequence) {
      this.stop();
      throw new Error("TTS audio sequence mismatch");
    }
    this.expectedSequence += 1;
    const pcm = decodePcm16(String(event.data || ""));
    const samples = new Float32Array(pcm.length);
    for (let index = 0; index < pcm.length; index += 1) {
      samples[index] = pcm[index] / 32768;
      const magnitude = Math.abs(samples[index]);
      this.peak = Math.max(this.peak, magnitude);
      if (Math.abs(pcm[index]) >= 32767) this.clippedSamples += 1;
    }
    this.totalSamples += pcm.length;
    const buffer = this.context.createBuffer(1, samples.length, this.sampleRate);
    buffer.copyToChannel(samples, 0);
    const source = this.context.createBufferSource();
    source.buffer = buffer;
    source.connect(this.masterGain);
    const startTime = Math.max(this.context.currentTime + 0.02, this.nextStartTime);
    this.nextStartTime = startTime + buffer.duration;
    if (!this.firstAudioReported && this.voiceTimingOriginMs !== null) {
      const scheduledAtMs = performance.now()
        + Math.max(0, startTime - this.context.currentTime) * 1000;
      const seconds = Math.max(0, (scheduledAtMs - this.voiceTimingOriginMs) / 1000);
      sendCommand("voice_diagnostic", {
        kind: "playback_first_audio",
        seconds: Number(seconds.toFixed(3)),
        hardware_measured: true,
      });
      this.firstAudioReported = true;
    }
    this.sources.add(source);
    source.onended = () => {
      this.sources.delete(source);
      this.finishIfDrained();
    };
    source.start(startTime);
  }

  finish() {
    this.ended = true;
    sendCommand("voice_diagnostic", {
      kind: "playback_signal",
      peak: Number(this.peak.toFixed(6)),
      clipped_samples: this.clippedSamples,
      total_samples: this.totalSamples,
      hardware_measured: false,
    });
    this.finishIfDrained();
  }

  finishIfDrained() {
    if (this.ended && this.sources.size === 0) {
      state.speaking = false;
      this.voiceTimingOriginMs = null;
      this.firstAudioReported = false;
      if (state.listening) setVoicePhase("listening", "Слушаю…");
    }
  }

  stop(reason = "", preserveTiming = false) {
    const wasSpeaking = state.speaking || this.sources.size > 0;
    const stopAt = this.context ? this.context.currentTime + 0.014 : 0;
    if (this.context && this.masterGain) {
      const now = this.context.currentTime;
      this.masterGain.gain.cancelScheduledValues(now);
      this.masterGain.gain.setValueAtTime(this.masterGain.gain.value, now);
      // A 12 ms fade avoids a discontinuity click on barge-in while keeping
      // acoustic cancellation far below the 250 ms product budget.
      this.masterGain.gain.linearRampToValueAtTime(0, now + 0.012);
    }
    for (const source of this.sources) {
      try { source.stop(stopAt); } catch (_error) { /* already stopped */ }
    }
    this.sources.clear();
    this.ended = true;
    state.speaking = false;
    if (reason && wasSpeaking) {
      sendCommand("voice_diagnostic", {
        kind: "playback_cancel_scheduled",
        fade_ms: 12,
        reason,
        hardware_measured: false,
      });
    }
    if (!preserveTiming) {
      this.voiceTimingOriginMs = null;
      this.firstAudioReported = false;
    }
    if (state.listening) setVoicePhase("listening", "Слушаю…");
  }
}

class VoiceCaptureController {
  constructor(player) {
    this.player = player;
    this.stream = null;
    this.context = null;
    this.source = null;
    this.processor = null;
    this.muteGain = null;
    this.active = false;
    this.starting = false;
    this.startGeneration = 0;
    this.utteranceActive = false;
    this.sequence = 0;
    this.preRoll = [];
    this.preRollMs = 0;
    this.noiseFloor = 0.004;
    this.calibrationUntil = 0;
    this.candidateMs = 0;
    this.silenceMs = 0;
    this.utteranceMs = 0;
    this.capturePeak = 0;
    this.captureClippedSamples = 0;
    this.captureTotalSamples = 0;
  }

  async start() {
    if (this.active || this.starting) return;
    if (!navigator.mediaDevices?.getUserMedia) {
      throw new Error("Захват микрофона недоступен в этом окружении Electron");
    }
    const AudioContextClass = window.AudioContext || window.webkitAudioContext;
    if (!AudioContextClass) throw new Error("Web Audio недоступен");
    const generation = ++this.startGeneration;
    const requestedAt = performance.now();
    this.starting = true;
    let stream = null;
    let context = null;
    try {
      stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          channelCount: { ideal: 1 },
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: false,
        },
        video: false,
      });
      if (generation !== this.startGeneration) {
        for (const track of stream.getTracks()) track.stop();
        return;
      }
      context = new AudioContextClass({ latencyHint: "interactive" });
      await context.resume();
      if (generation !== this.startGeneration) {
        for (const track of stream.getTracks()) track.stop();
        if (context.state !== "closed") await context.close();
        return;
      }
      const source = context.createMediaStreamSource(stream);
      const processor = context.createScriptProcessor(2048, 1, 1);
      const muteGain = context.createGain();
      muteGain.gain.value = 0;
      processor.onaudioprocess = (event) => this.process(event.inputBuffer.getChannelData(0));
      source.connect(processor);
      processor.connect(muteGain);
      muteGain.connect(context.destination);

      this.stream = stream;
      this.context = context;
      this.source = source;
      this.processor = processor;
      this.muteGain = muteGain;
      this.active = true;
      this.calibrationUntil = performance.now() + 600;
      this.player.attachContext(context);
      state.listening = true;
      setVoicePhase("calibrating", "Проверяю уровень шума…");
      sendCommand("voice_session_start", {
        sample_rate: VOICE_SAMPLE_RATE,
        encoding: "pcm_s16le",
        channels: 1,
        capture: {
          browser_sample_rate: context.sampleRate,
          echo_cancellation_requested: true,
          noise_suppression_requested: true,
        },
      });
      sendCommand("voice_diagnostic", {
        kind: "capture_ready",
        browser_sample_rate: context.sampleRate,
        target_sample_rate: VOICE_SAMPLE_RATE,
        hardware_measured: true,
      });
      sendCommand("voice_diagnostic", {
        kind: "listen_ready",
        seconds: Number(((performance.now() - requestedAt) / 1000).toFixed(3)),
        hardware_measured: true,
      });
      renderVoiceCapability();
    } catch (error) {
      for (const track of stream?.getTracks() || []) track.stop();
      if (context && context.state !== "closed") await context.close();
      if (generation === this.startGeneration) {
        this.stream = null;
        this.context = null;
        this.source = null;
        this.processor = null;
        this.muteGain = null;
        this.active = false;
        state.listening = false;
      }
      throw error;
    } finally {
      if (generation === this.startGeneration) this.starting = false;
    }
  }

  async stop({ notifyBackend = true } = {}) {
    this.startGeneration += 1;
    this.starting = false;
    if (!this.active) {
      state.listening = false;
      renderVoiceCapability();
      return;
    }
    this.active = false;
    this.utteranceActive = false;
    this.preRoll = [];
    this.preRollMs = 0;
    this.player.stop();
    if (this.processor) this.processor.onaudioprocess = null;
    try { this.source?.disconnect(); } catch (_error) { /* already detached */ }
    try { this.processor?.disconnect(); } catch (_error) { /* already detached */ }
    try { this.muteGain?.disconnect(); } catch (_error) { /* already detached */ }
    for (const track of this.stream?.getTracks() || []) track.stop();
    if (this.context && this.context.state !== "closed") await this.context.close();
    this.stream = null;
    this.context = null;
    this.source = null;
    this.processor = null;
    this.muteGain = null;
    state.listening = false;
    setVoicePhase("idle", "Готов к голосовому разговору");
    if (notifyBackend) sendCommand("voice_session_stop");
    renderVoiceCapability();
  }

  process(input) {
    if (!this.active || !this.context) return;
    let energy = 0;
    for (let index = 0; index < input.length; index += 1) {
      energy += input[index] * input[index];
      const magnitude = Math.abs(input[index]);
      this.capturePeak = Math.max(this.capturePeak, magnitude);
      if (magnitude >= 0.999) this.captureClippedSamples += 1;
    }
    this.captureTotalSamples += input.length;
    const rms = Math.sqrt(energy / Math.max(1, input.length));
    const durationMs = input.length * 1000 / this.context.sampleRate;
    const resampled = resampleMono(input, this.context.sampleRate, VOICE_SAMPLE_RATE);
    const pcm = floatToPcm16(resampled);
    const block = { pcm, durationMs };

    if (!this.utteranceActive) {
      this.pushPreRoll(block);
      const calibrating = performance.now() < this.calibrationUntil && !state.speaking;
      // Learn only plausible room noise. Immediate near speech must not poison
      // the baseline, and playback/barge-in is never blocked by calibration.
      if (!state.speaking && rms < Math.max(0.018, this.noiseFloor * 4)) {
        this.noiseFloor = this.noiseFloor * 0.96 + rms * 0.04;
      }
      if (calibrating) {
        const earlySpeechThreshold = Math.max(0.025, this.noiseFloor * 5);
        this.candidateMs = rms >= earlySpeechThreshold
          ? this.candidateMs + durationMs
          : Math.max(0, this.candidateMs - durationMs * 2);
        if (this.candidateMs >= 120) this.beginUtterance(false);
        return;
      }
      const normalThreshold = Math.max(0.009, this.noiseFloor * 3.0);
      const bargeThreshold = Math.max(0.025, this.noiseFloor * 5.0);
      const threshold = state.speaking ? bargeThreshold : normalThreshold;
      this.candidateMs = rms >= threshold
        ? this.candidateMs + durationMs
        : Math.max(0, this.candidateMs - durationMs * 2);
      const requiredMs = state.speaking ? 220 : 90;
      if (this.candidateMs >= requiredMs) this.beginUtterance(state.speaking);
      return;
    }

    this.sendBlock(pcm);
    this.utteranceMs += durationMs;
    const speechThreshold = Math.max(0.008, this.noiseFloor * 2.6);
    this.silenceMs = rms >= speechThreshold ? 0 : this.silenceMs + durationMs;
    if ((this.silenceMs >= 650 && this.utteranceMs >= 350) || this.utteranceMs >= 20000) {
      this.endUtterance();
    }
  }

  pushPreRoll(block) {
    this.preRoll.push(block);
    this.preRollMs += block.durationMs;
    while (this.preRoll.length > 1 && this.preRollMs > 320) {
      const removed = this.preRoll.shift();
      this.preRollMs -= removed.durationMs;
    }
  }

  beginUtterance(isBargeIn) {
    if (isBargeIn) {
      this.player.stop("barge_in");
      sendCommand("voice_cancel", { reason: "barge_in" });
    }
    sendCommand("voice_utterance_start", {
      sample_rate: VOICE_SAMPLE_RATE,
      encoding: "pcm_s16le",
      channels: 1,
      barge_in: isBargeIn,
    });
    this.utteranceActive = true;
    this.sequence = 0;
    this.capturePeak = 0;
    this.captureClippedSamples = 0;
    this.captureTotalSamples = 0;
    this.utteranceMs = this.preRoll.reduce((total, item) => total + item.durationMs, 0);
    this.silenceMs = 0;
    for (const item of this.preRoll) this.sendBlock(item.pcm);
    this.preRoll = [];
    this.preRollMs = 0;
    this.candidateMs = 0;
    setVoicePhase("capturing", isBargeIn ? "Перебиваю и слушаю…" : "Слышу вас…");
  }

  sendBlock(pcm) {
    sendCommand("voice_audio_chunk", { sequence: this.sequence, data: pcmToBase64(pcm) });
    this.sequence += 1;
  }

  endUtterance() {
    if (!this.utteranceActive) return;
    const speechTailMs = Math.max(0, Math.min(2000, this.silenceMs));
    this.player.setVoiceTimingOrigin(performance.now() - speechTailMs);
    sendCommand("voice_utterance_end", {
      duration_ms: Math.round(this.utteranceMs),
      speech_tail_ms: Math.round(speechTailMs),
    });
    sendCommand("voice_diagnostic", {
      kind: "capture_signal",
      peak: Number(this.capturePeak.toFixed(6)),
      clipped_samples: this.captureClippedSamples,
      total_samples: this.captureTotalSamples,
      hardware_measured: true,
    });
    this.utteranceActive = false;
    this.sequence = 0;
    this.silenceMs = 0;
    this.utteranceMs = 0;
    this.candidateMs = 0;
    this.capturePeak = 0;
    this.captureClippedSamples = 0;
    this.captureTotalSamples = 0;
    setVoicePhase("transcribing", "Распознаю речь…");
  }
}

class PushToTalkDictationController {
  constructor() {
    this.stream = null;
    this.context = null;
    this.source = null;
    this.processor = null;
    this.muteGain = null;
    this.requestId = null;
    this.heldRequestId = null;
    this.sequence = 0;
    this.recording = false;
    this.starting = false;
    this.sentStart = false;
    this.generation = 0;
    this.maxTimer = null;
  }

  async handleKey(event) {
    if (!event || typeof event.requestId !== "string") return;
    if (event.phase === "down") {
      if (!state.ptt.hotkeyAvailable || !state.ptt.sttAvailable) {
        sendCommand("ptt_dictation_cancel", {
          request_id: event.requestId,
          reason: "capability_unavailable",
        });
        toast(state.ptt.sttDetail || state.ptt.hotkeyDetail || "Диктовка F8 недоступна");
        return;
      }
      if (this.recording || this.starting || state.ptt.phase === "transcribing") {
        sendCommand("ptt_dictation_cancel", {
          request_id: event.requestId,
          reason: "dictation_busy",
        });
        toast("Предыдущая диктовка ещё не завершена");
        return;
      }
      this.heldRequestId = event.requestId;
      await this.start(event.requestId);
      return;
    }
    if (event.phase === "up") {
      if (this.heldRequestId === event.requestId) this.heldRequestId = null;
      if (this.recording && this.requestId === event.requestId) await this.finish();
      return;
    }
    if (event.phase === "cancel" && this.requestId === event.requestId) {
      await this.cancel("hotkey_helper_stopped");
    }
  }

  async start(requestId) {
    const AudioContextClass = window.AudioContext || window.webkitAudioContext;
    if (!navigator.mediaDevices?.getUserMedia || !AudioContextClass) {
      state.ptt.permission = "unavailable";
      renderVoiceCapability();
      sendCommand("ptt_dictation_cancel", { request_id: requestId, reason: "capture_unavailable" });
      return;
    }
    if (voiceCapture.active || voiceCapture.starting) await voiceCapture.stop();
    const generation = ++this.generation;
    this.requestId = requestId;
    state.ptt.pendingText = "";
    this.starting = true;
    state.ptt.phase = "requesting_permission";
    document.body.dataset.pttState = state.ptt.phase;
    setVoicePhase("listening", "F8: запрашиваю доступ к микрофону…");
    let stream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          channelCount: { ideal: 1 },
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: false,
        },
        video: false,
      });
      state.ptt.permission = "granted";
      state.ptt.captureVerified = true;
    } catch (error) {
      state.ptt.permission = error?.name === "NotAllowedError" ? "denied" : "error";
      state.ptt.captureVerified = false;
      state.ptt.phase = "error";
      document.body.dataset.pttState = state.ptt.phase;
      this.starting = false;
      this.requestId = null;
      this.heldRequestId = null;
      renderVoiceCapability();
      sendCommand("ptt_dictation_cancel", {
        request_id: requestId,
        reason: state.ptt.permission === "denied" ? "microphone_denied" : "microphone_error",
      });
      toast(`Диктовка F8: микрофон недоступен (${error?.name || "Error"})`);
      return;
    }
    if (generation !== this.generation || this.heldRequestId !== requestId) {
      for (const track of stream.getTracks()) track.stop();
      this.starting = false;
      this.requestId = null;
      sendCommand("ptt_dictation_cancel", { request_id: requestId, reason: "released_before_capture" });
      state.ptt.phase = "idle";
      document.body.dataset.pttState = state.ptt.phase;
      renderVoiceCapability();
      return;
    }
    let context;
    try {
      context = new AudioContextClass({ latencyHint: "interactive" });
      await context.resume();
    } catch (error) {
      for (const track of stream.getTracks()) track.stop();
      if (context && context.state !== "closed") await context.close();
      this.starting = false;
      this.requestId = null;
      this.heldRequestId = null;
      state.ptt.phase = "error";
      document.body.dataset.pttState = state.ptt.phase;
      sendCommand("ptt_dictation_cancel", { request_id: requestId, reason: "audio_context_error" });
      renderVoiceCapability();
      toast(`Диктовка F8: аудиотракт недоступен (${error?.name || "Error"})`);
      return;
    }
    if (generation !== this.generation || this.heldRequestId !== requestId) {
      for (const track of stream.getTracks()) track.stop();
      if (context.state !== "closed") await context.close();
      this.starting = false;
      this.requestId = null;
      sendCommand("ptt_dictation_cancel", {
        request_id: requestId,
        reason: "released_during_audio_start",
      });
      state.ptt.phase = "idle";
      document.body.dataset.pttState = state.ptt.phase;
      renderVoiceCapability();
      return;
    }
    this.starting = false;
    const source = context.createMediaStreamSource(stream);
    const processor = context.createScriptProcessor(2048, 1, 1);
    const muteGain = context.createGain();
    muteGain.gain.value = 0;
    processor.onaudioprocess = (audioEvent) => {
      if (!this.recording || this.requestId !== requestId) return;
      const mono = audioEvent.inputBuffer.getChannelData(0);
      const resampled = resampleMono(mono, context.sampleRate, VOICE_SAMPLE_RATE);
      const pcm = floatToPcm16(resampled);
      sendCommand("ptt_audio_chunk", {
        request_id: requestId,
        sequence: this.sequence,
        data: pcmToBase64(pcm),
      });
      this.sequence += 1;
    };
    source.connect(processor);
    processor.connect(muteGain);
    muteGain.connect(context.destination);
    this.stream = stream;
    this.context = context;
    this.source = source;
    this.processor = processor;
    this.muteGain = muteGain;
    this.sequence = 0;
    this.recording = true;
    this.sentStart = true;
    state.ptt.phase = "recording";
    document.body.dataset.pttState = state.ptt.phase;
    sendCommand("ptt_dictation_start", {
      request_id: requestId,
      sample_rate: VOICE_SAMPLE_RATE,
      encoding: "pcm_s16le",
      channels: 1,
    });
    setVoicePhase("listening", "Диктовка F8: говорите, пока удерживаете клавишу…");
    renderVoiceCapability();
    this.maxTimer = window.setTimeout(() => {
      if (this.recording && this.requestId === requestId) void this.finish();
    }, 25000);
  }

  async finish() {
    if (!this.recording || !this.requestId) return;
    const requestId = this.requestId;
    this.recording = false;
    this.heldRequestId = null;
    this.stopResources();
    state.ptt.phase = "transcribing";
    document.body.dataset.pttState = state.ptt.phase;
    setVoicePhase("transcribing", "Локально распознаю диктовку F8…");
    sendCommand("ptt_dictation_end", { request_id: requestId });
    renderVoiceCapability();
  }

  async cancel(reason = "cancel") {
    const requestId = this.requestId || this.heldRequestId;
    this.generation += 1;
    this.recording = false;
    this.starting = false;
    this.heldRequestId = null;
    this.stopResources();
    this.requestId = null;
    this.sentStart = false;
    state.ptt.pendingText = "";
    state.ptt.phase = "idle";
    document.body.dataset.pttState = state.ptt.phase;
    if (requestId) sendCommand("ptt_dictation_cancel", { request_id: requestId, reason });
    if (state.listening) setVoicePhase("listening", "Слушаю…");
    else setVoicePhase("idle", "Готов к работе");
    renderVoiceCapability();
  }

  stopResources() {
    if (this.maxTimer !== null) window.clearTimeout(this.maxTimer);
    this.maxTimer = null;
    if (this.processor) this.processor.onaudioprocess = null;
    try { this.source?.disconnect(); } catch (_error) { /* already detached */ }
    try { this.processor?.disconnect(); } catch (_error) { /* already detached */ }
    try { this.muteGain?.disconnect(); } catch (_error) { /* already detached */ }
    for (const track of this.stream?.getTracks() || []) track.stop();
    if (this.context && this.context.state !== "closed") void this.context.close();
    this.stream = null;
    this.context = null;
    this.source = null;
    this.processor = null;
    this.muteGain = null;
  }

  resetAfterResult() {
    this.generation += 1;
    this.recording = false;
    this.starting = false;
    this.heldRequestId = null;
    this.stopResources();
    this.requestId = null;
    this.sentStart = false;
    state.ptt.phase = "idle";
    document.body.dataset.pttState = state.ptt.phase;
    if (state.listening) setVoicePhase("listening", "Слушаю…");
    else setVoicePhase("idle", "Готов к работе");
    renderVoiceCapability();
  }
}

const audioPlayer = new PcmAudioPlayer();
const voiceCapture = new VoiceCaptureController(audioPlayer);
const pttDictation = new PushToTalkDictationController();

function availableComposerSuggestions() {
  const composer = byId("composerInput");
  const value = String(composer?.value || "");
  const leadingTrimmed = value.trimStart();
  if (/^\/[^\s]*$/.test(leadingTrimmed)) {
    const query = leadingTrimmed.slice(1).toLocaleLowerCase("ru");
    const skills = Array.isArray(state.snapshot.skills) ? state.snapshot.skills : [];
    return skills
      .filter((skill) => {
        const command = String(skill.command || "");
        const name = String(skill.name || "");
        return command.startsWith("/") && (
          !query
          || command.slice(1).toLocaleLowerCase("ru").startsWith(query)
          || name.toLocaleLowerCase("ru").includes(query)
        );
      })
      .sort((left, right) => String(left.command).localeCompare(String(right.command), "ru"))
      .slice(0, 6)
      .map((skill) => ({
        id: `skill:${String(skill.id || skill.command)}`,
        kind: "skill",
        title: String(skill.command),
        subtitle: String(skill.name || "Скилл"),
        insertion: String(skill.command),
      }));
  }

  const at = value.lastIndexOf("@");
  if (at < 0) return [];
  const queryText = value.slice(at + 1);
  if (/[\s\[\]"]/.test(queryText)) return [];
  const query = queryText.toLocaleLowerCase("ru");
  const sources = Array.isArray(state.snapshot.sources) ? state.snapshot.sources : [];
  return sources
    .filter((source) => {
      const title = String(source.title || "");
      const kind = String(source.kind || "");
      return !query
        || title.toLocaleLowerCase("ru").includes(query)
        || kind.toLocaleLowerCase("ru").includes(query);
    })
    .sort((left, right) => String(left.title).localeCompare(String(right.title), "ru"))
    .slice(0, 6)
    .map((source) => ({
      id: `source:${String(source.id || source.title)}`,
      kind: "source",
      title: String(source.title || "Источник"),
      subtitle: String(source.kind) === "meeting" ? "Встреча" : "Источник",
      insertion: `@[${String(source.title || "Источник").replaceAll("]", ")")}]`,
    }));
}

function hideComposerSuggestions() {
  const suggestions = byId("composerSuggestions");
  const composer = byId("composerInput");
  suggestions.hidden = true;
  suggestions.replaceChildren();
  composer.setAttribute("aria-expanded", "false");
  state.composerSuggestionIndex = 0;
}

function applyComposerSuggestion(suggestion) {
  const composer = byId("composerInput");
  if (!suggestion || !(composer instanceof HTMLTextAreaElement)) return false;
  if (suggestion.kind === "skill") {
    composer.value = `${suggestion.insertion} `;
  } else {
    const at = composer.value.lastIndexOf("@");
    if (at < 0) return false;
    composer.value = `${composer.value.slice(0, at)}${suggestion.insertion} `;
  }
  hideComposerSuggestions();
  composer.dispatchEvent(new Event("input", { bubbles: true }));
  composer.focus();
  return true;
}

function renderComposerSuggestions() {
  const container = byId("composerSuggestions");
  const composer = byId("composerInput");
  const suggestions = availableComposerSuggestions();
  container.replaceChildren();
  if (!suggestions.length) {
    hideComposerSuggestions();
    return;
  }
  state.composerSuggestionIndex = Math.min(
    Math.max(state.composerSuggestionIndex, 0),
    suggestions.length - 1,
  );
  suggestions.forEach((suggestion, index) => {
    const item = textNode(
      "button",
      `composer-suggestion${index === state.composerSuggestionIndex ? " active" : ""}`,
      "",
    );
    item.type = "button";
    item.setAttribute("role", "option");
    item.setAttribute("aria-selected", index === state.composerSuggestionIndex ? "true" : "false");
    item.append(
      textNode("strong", "", suggestion.title),
      textNode("span", "", suggestion.subtitle),
    );
    item.addEventListener("mousedown", (event) => event.preventDefault());
    item.addEventListener("click", () => applyComposerSuggestion(suggestion));
    container.append(item);
  });
  container.hidden = false;
  composer.setAttribute("aria-expanded", "true");
}

function renderMessages() {
  const container = byId("messages");
  container.replaceChildren();
  const messages = Array.isArray(state.snapshot.messages) ? state.snapshot.messages : [];
  if (!messages.length && !state.streamingText) {
    const empty = textNode("div", "empty-state", "");
    empty.append(
      textNode("strong", "", "Рабочий контекст готов"),
      textNode("p", "", "Подключите локальную или корпоративную модель и задайте первый вопрос."),
    );
    container.append(empty);
    return;
  }
  for (const message of messages) {
    if (!message || !["user", "assistant"].includes(message.role)) continue;
    const row = textNode("article", `message ${message.role}`, "");
    row.append(
      textNode("div", "author", message.role === "user" ? "Вы" : "RnD Workbench"),
      textNode("div", "bubble", String(message.content || "")),
    );
    const metadata = decodedMetadata(message.metadata);
    const sources = Array.isArray(metadata.sources) ? metadata.sources : [];
    if (message.role === "assistant" && sources.length) {
      const sourceLinks = textNode("div", "message-sources", "");
      for (const source of sources.slice(0, 5)) {
        if (!source || typeof source !== "object" || !source.id) continue;
        const link = textNode("button", "source-chip", String(source.title || "Источник"));
        link.type = "button";
        link.title = "Открыть использованный фрагмент";
        link.append(classificationBadge(source.classification));
        link.addEventListener("click", () => requestSourceFragment(source));
        sourceLinks.append(link);
      }
      if (sourceLinks.childElementCount) row.append(sourceLinks);
    }
    container.append(row);
  }
  if (state.streamingText) {
    const row = textNode("article", "message assistant", "");
    row.append(textNode("div", "author", "RnD Workbench"));
    const bubble = textNode("div", "bubble", state.streamingText);
    bubble.append(textNode("span", "stream-caret", " ▌"));
    row.append(bubble);
    container.append(row);
  }
  container.scrollTop = container.scrollHeight;
}

function renderTasks() {
  const container = byId("taskList");
  container.replaceChildren();
  const tasks = Array.isArray(state.snapshot.tasks) ? state.snapshot.tasks : [];
  for (const task of tasks) {
    const row = textNode("div", `task-row${task.id === state.snapshot.current_task_id ? " active" : ""}`, "");
    const open = textNode("button", "task-open", "");
    open.type = "button";
    open.append(
      textNode("span", "task-open-title", String(task.title || "Новая задача")),
      classificationBadge(task.classification),
    );
    open.addEventListener("click", () => sendCommand("select_task", { id: task.id }));
    row.append(open);
    const remove = textNode("button", "delete-task", "×");
    remove.type = "button";
    remove.title = "Удалить задачу";
    remove.addEventListener("click", (event) => {
      event.stopPropagation();
      if (window.confirm(`Удалить задачу «${String(task.title || "Новая задача")}»?`)) {
        sendCommand("delete_task", { task_id: task.id });
      }
    });
    row.append(remove);
    container.append(row);
  }
  const current = tasks.find((task) => task.id === state.snapshot.current_task_id);
  byId("taskTitle").textContent = String(current?.title || "Новая задача");
  const taskClassification = byId("taskClassification");
  const classification = normalizedClassification(current?.classification);
  taskClassification.className = `classification-badge ${classification}`;
  taskClassification.textContent = CLASSIFICATION_LABELS[classification];
}

function renderContextLibrary() {
  const sources = Array.isArray(state.snapshot.sources) ? state.snapshot.sources : [];
  const artifacts = Array.isArray(state.snapshot.artifacts) ? state.snapshot.artifacts : [];
  const tasks = Array.isArray(state.snapshot.tasks) ? state.snapshot.tasks : [];
  const currentTask = tasks.find((task) => task.id === state.snapshot.current_task_id);
  const plan = Array.isArray(currentTask?.plan) ? currentTask.plan : [];
  byId("contextLibraryCount").textContent = String(plan.length + sources.length + artifacts.length);
  byId("taskPlanCount").textContent = `${plan.length} ${plan.length === 1 ? "шаг" : plan.length >= 2 && plan.length <= 4 ? "шага" : "шагов"}`;

  const planList = byId("taskPlanList");
  planList.replaceChildren();
  for (const step of plan) {
    planList.append(textNode("li", "", String(step)));
  }
  if (!plan.length) {
    planList.append(textNode("li", "task-plan-empty", "План ещё не сформирован"));
  }

  const sourceList = byId("sourceList");
  sourceList.replaceChildren();
  for (const source of sources.slice(0, 6)) {
    const row = textNode("div", "context-entity-row", "");
    const open = textNode("button", "context-entity-open", String(source.title || "Источник"));
    open.type = "button";
    open.title = "Показать источник";
    open.addEventListener("click", () => requestSourceFragment(source));
    const remove = textNode("button", "context-entity-delete", "×");
    remove.type = "button";
    remove.title = "Удалить источник";
    remove.setAttribute("aria-label", `Удалить источник ${String(source.title || "")}`);
    remove.addEventListener("click", () => {
      if (window.confirm(`Удалить источник «${String(source.title || "Источник")}»?`)) {
        sendCommand("delete_source", { source_id: source.id });
      }
    });
    row.append(open, classificationBadge(source.classification), remove);
    sourceList.append(row);
  }
  if (!sourceList.childElementCount) {
    sourceList.append(textNode("span", "context-entity-empty", "Источников пока нет"));
  }

  const artifactList = byId("artifactList");
  artifactList.replaceChildren();
  for (const artifact of artifacts.slice(0, 6)) {
    const row = textNode("div", "context-entity-row", "");
    const open = textNode("button", "context-entity-open", String(artifact.title || "Материал"));
    open.type = "button";
    open.title = "Открыть историю материала";
    open.addEventListener("click", () => {
      sendCommand("artifact_versions", { artifact_id: artifact.id });
    });
    const remove = textNode("button", "context-entity-delete", "×");
    remove.type = "button";
    remove.title = "Удалить материал";
    remove.setAttribute("aria-label", `Удалить материал ${String(artifact.title || "")}`);
    remove.addEventListener("click", () => {
      if (window.confirm(`Удалить материал «${String(artifact.title || "Материал")}» и его версии?`)) {
        sendCommand("delete_artifact", { artifact_id: artifact.id });
      }
    });
    row.append(open, classificationBadge(artifact.classification), remove);
    artifactList.append(row);
  }
  if (!artifactList.childElementCount) {
    artifactList.append(textNode("span", "context-entity-empty", "Материалов пока нет"));
  }
}

function openSourceFragment(source) {
  if (!source || typeof source !== "object") return;
  byId("sourceFragmentTitle").textContent = String(source.title || "Источник");
  const start = Number(source.char_start || 0);
  const end = Number(source.char_end || 0);
  const prefix = source.exact === true ? "Точный фрагмент" : "Начало источника";
  byId("sourceFragmentMeta").replaceChildren(
    classificationBadge(source.classification),
    textNode("span", "", `${prefix} · символы ${start}–${end}`),
  );
  byId("sourceFragmentText").textContent = String(source.excerpt || "");
  const dialog = byId("sourceFragmentDialog");
  if (!dialog.open) dialog.showModal();
}

function entityTitle(collection, id, fallback) {
  const rows = Array.isArray(collection) ? collection : [];
  return String(rows.find((item) => String(item.id) === String(id))?.title || fallback);
}

function renderArtifactHistory(payload) {
  if (!payload || typeof payload !== "object" || !payload.artifact) return;
  state.artifactHistory = payload;
  const artifact = payload.artifact;
  const versions = Array.isArray(payload.versions) ? payload.versions : [];
  const relations = Array.isArray(payload.relations) ? payload.relations : [];
  byId("artifactHistoryTitle").textContent = String(artifact.title || "Материал");
  byId("artifactHistorySummary").replaceChildren(
    classificationBadge(artifact.classification),
    textNode("span", "", `Текущая версия: ${Number(artifact.current_version || 1)} · всего ${versions.length}`),
  );

  const versionList = byId("artifactVersionList");
  versionList.replaceChildren();
  for (const version of [...versions].reverse()) {
    const row = textNode("div", `artifact-version-row${version.is_current ? " current" : ""}`, "");
    const copy = textNode("div", "artifact-version-copy", "");
    copy.append(
      textNode("strong", "", `Версия ${Number(version.version || 0)}`),
      textNode("span", "", version.is_current ? "Текущая" : String(version.created_at || "")),
    );
    row.append(copy, classificationBadge(version.classification));
    if (!version.is_current) {
      const restore = textNode("button", "secondary-button artifact-restore", "Восстановить");
      restore.type = "button";
      restore.addEventListener("click", () => {
        if (window.confirm(`Создать новую версию из версии ${Number(version.version)}?`)) {
          sendCommand("restore_artifact", {
            artifact_id: artifact.id,
            version: Number(version.version),
          });
        }
      });
      row.append(restore);
    }
    versionList.append(row);
  }

  const relationLabels = {
    produced_by_task: "Создано задачей",
    derived_from_source: "Получено из источника",
    derived_from_artifact: "Создано из материала",
    restored_from: "Восстановлено из версии",
    revision_of: "Продолжает версию",
  };
  const provenanceList = byId("artifactProvenanceList");
  provenanceList.replaceChildren();
  for (const relation of relations.slice(-20).reverse()) {
    const metadata = decodedMetadata(relation.metadata);
    let target = "";
    if (relation.source_id) {
      target = entityTitle(state.snapshot.sources, relation.source_id, "Источник");
    } else if (relation.task_id) {
      target = entityTitle(state.snapshot.tasks, relation.task_id, "Задача");
    } else if (relation.related_artifact_id) {
      target = entityTitle(state.snapshot.artifacts, relation.related_artifact_id, "Материал");
    }
    const coordinates = Number.isInteger(metadata.char_start) && Number.isInteger(metadata.char_end)
      ? ` · символы ${metadata.char_start}–${metadata.char_end}`
      : "";
    provenanceList.append(textNode(
      "div",
      "artifact-provenance-row",
      `v${Number(relation.artifact_version || 0)} · ${relationLabels[relation.relation_type] || "Связано"}${target ? `: ${target}` : ""}${coordinates}`,
    ));
  }
  if (!provenanceList.childElementCount) {
    provenanceList.append(textNode("span", "context-entity-empty", "Связи происхождения не зафиксированы"));
  }
  const dialog = byId("artifactHistoryDialog");
  if (!dialog.open) dialog.showModal();
}

function renderApprovals() {
  const container = byId("approvalList");
  if (!container) return;
  container.replaceChildren();
  const approvals = Array.isArray(state.snapshot.approvals)
    ? state.snapshot.approvals.filter((item) => ["pending", "error"].includes(String(item.status || "")))
    : [];
  byId("approvalCount").textContent = String(approvals.length);
  if (!approvals.length) {
    container.append(textNode("span", "approval-empty", "Нет действий, требующих внимания"));
    return;
  }
  for (const approval of approvals.slice(0, 6)) {
    const status = String(approval.status || "pending");
    const item = textNode("div", `approval-item${status === "error" ? " error" : ""}`, "");
    item.append(textNode("div", "approval-item-title", String(approval.title || approval.action_type || "Внешнее действие")));
    item.append(textNode(
      "div",
      "approval-item-meta",
      status === "pending" ? `Риск: ${String(approval.risk || "medium")}` : "Нужна проверка результата",
    ));
    if (status === "pending") {
      const actions = textNode("div", "approval-actions", "");
      const reject = textNode("button", "secondary-button", "Отклонить");
      reject.type = "button";
      reject.addEventListener("click", () => sendCommand("resolve_approval", { id: approval.id, status: "rejected" }));
      const approve = textNode("button", "send-button", "Подтвердить");
      approve.type = "button";
      approve.addEventListener("click", () => {
        if (window.confirm(`Выполнить действие «${String(approval.title || approval.action_type)}»?`)) {
          sendCommand("resolve_approval", { id: approval.id, status: "approved" });
        }
      });
      actions.append(reject, approve);
      item.append(actions);
    }
    container.append(item);
  }
}

function renderMeetings() {
  const card = byId("meetingContextCard");
  if (!card) return;
  const meetings = Array.isArray(state.snapshot.meetings) ? state.snapshot.meetings : [];
  card.hidden = meetings.length === 0;
  if (!meetings.length) return;

  const currentId = String(state.snapshot.current_meeting_id || meetings[0]?.id || "");
  const selected = meetings.find((meeting) => String(meeting.id) === currentId) || meetings[0];
  const select = byId("meetingSelect");
  select.replaceChildren();
  for (const meeting of meetings) {
    const option = document.createElement("option");
    option.value = String(meeting.id || "");
    const date = String(meeting.occurred_at || meeting.created_at || "").slice(0, 10);
    option.textContent = `${date ? `${date} · ` : ""}${String(meeting.title || "Встреча")}`;
    select.append(option);
  }
  select.value = String(selected.id || "");

  const counts = selected.item_counts && typeof selected.item_counts === "object"
    ? selected.item_counts
    : {};
  const openAttention = Number(selected.open_attention || 0);
  byId("meetingContextMeta").textContent = openAttention > 0
    ? `Требует внимания: ${openAttention}`
    : "Анализ готов";
  byId("meetingSummary").textContent = String(
    selected.summary || "Решения, поручения, риски и вопросы извлечены из транскрипта.",
  );
  const countLabels = [
    ["decision", "Решения"],
    ["action", "Поручения"],
    ["risk", "Риски"],
    ["question", "Вопросы"],
  ];
  const countContainer = byId("meetingCounts");
  countContainer.replaceChildren();
  for (const [kind, label] of countLabels) {
    const count = Number(counts[kind] || 0);
    if (count > 0) countContainer.append(textNode("span", "", `${label}: ${count}`));
  }

  const kindLabels = {
    decision: "Решение",
    action: "Поручение",
    commitment: "Обязательство",
    risk: "Риск",
    question: "Вопрос",
    topic: "Тема",
  };
  const itemList = byId("meetingItemList");
  itemList.replaceChildren();
  const items = Array.isArray(state.snapshot.meeting_items)
    ? state.snapshot.meeting_items.slice(0, 5)
    : [];
  for (const item of items) {
    const row = document.createElement("li");
    row.append(
      textNode("span", "", kindLabels[String(item.kind)] || "Пункт"),
      textNode("span", "", String(item.text || "")),
    );
    itemList.append(row);
  }
  if (!items.length) {
    const row = document.createElement("li");
    row.append(textNode("span", "", "Пункт"), textNode("span", "", "Данные ещё не извлечены."));
    itemList.append(row);
  }

  const briefing = String(state.snapshot.meeting_briefing || "").trim();
  byId("meetingBriefingText").textContent = briefing || "Брифинг ещё не подготовлен.";
  byId("prepareMeetingBriefingButton").textContent = briefing
    ? "Обновить брифинг"
    : "Подготовить брифинг";
}

function renderRuntime() {
  const ready = Boolean(state.runtime.ready);
  const provider = state.runtime.provider_type;
  const route = ready && provider === "local" ? "local" : ready && provider === "corporate" ? "corporate" : "unconfigured";
  document.body.dataset.route = route;
  const routeLabel = byId("routeLabel");
  const compact = state.mode === "compact";
  routeLabel.textContent = route === "local"
    ? compact ? "полностью локально" : "локальная модель · данные на устройстве"
    : route === "corporate"
      ? compact ? "корпоративный контур" : "корпоративная модель · защищённый API"
      : state.runtime.base_url ? "модель требует настройки" : "модель не настроена";
  byId("sidebarStatus").textContent = route === "local" ? "Локальный контур" : route === "corporate" ? "Корпоративный контур" : "Нужна настройка";
  const javaPolicy = state.platform.java_core_policy || {};
  const routePolicyTitle = javaPolicy.ready
    ? "Маршрутизация проверяется Java 21 core"
    : javaPolicy.configured
      ? "Java 21 core временно недоступен; действует резервная Python-политика"
      : "Java 21 core не настроен в development-режиме";
  const actionJournal = state.platform.java_action_journal || {};
  const recoveryAttention = Number(actionJournal.recovery?.requires_attention || 0);
  const autonomyPolicyReady = Boolean(actionJournal.autonomy_policy_ready);
  const actionJournalTitle = recoveryAttention > 0
    ? `Внешние действия требуют сверки: ${recoveryAttention}`
    : actionJournal.ready && autonomyPolicyReady
      ? "Java 21 проверяет политику действий и защищает их от повторного выполнения"
      : actionJournal.ready
        ? "Журнал Java 21 активен; политика действий работает в резервном режиме"
      : "Защитный журнал недоступен; внешние действия блокируются";
  routeLabel.title = `${routePolicyTitle}\n${actionJournalTitle}`;
  if (javaPolicy.configured && !javaPolicy.ready && !state.javaFallbackNotified) {
    state.javaFallbackNotified = true;
    toast("Java core недоступен — действует резервная встроенная политика");
  }
}

function voiceDiagnosticText() {
  const diagnostics = state.platform.voice_diagnostics || {};
  return [diagnostics.stt?.detail, diagnostics.tts?.detail].filter(Boolean).join(" · ");
}

function pttPermissionLabel() {
  switch (state.ptt.permission) {
    case "granted": return state.ptt.captureVerified
      ? "микрофон проверен записью"
      : "разрешение Electron есть; устройство проверится при удержании";
    case "denied": return "микрофон запрещён";
    case "prompt": return "разрешение будет запрошено при первом удержании";
    case "unavailable": return "захват микрофона недоступен";
    case "error": return "ошибка доступа к микрофону";
    default: return "разрешение микрофона ещё не проверено";
  }
}

function pttDiagnosticText() {
  if (!state.ptt.sttAvailable) return state.ptt.sttDetail || "Faster-Whisper не готов";
  if (!state.ptt.hotkeyAvailable) return state.ptt.hotkeyDetail || "Системная F8 не готова";
  return `Удерживайте F8 для локальной диктовки в активное поле · ${pttPermissionLabel()}`;
}

function renderVoiceCapability() {
  const runtimeAvailable = Boolean(state.platform.voice_available);
  const captureAvailable = Boolean(navigator.mediaDevices?.getUserMedia);
  const available = runtimeAvailable && captureAvailable;
  const button = byId("micButton");
  button.disabled = !available;
  button.setAttribute("aria-pressed", state.listening ? "true" : "false");
  byId("micLabel").textContent = available
    ? state.listening ? "Завершить разговор" : "Начать разговор"
    : runtimeAvailable ? "Нет доступа к микрофону" : "Настройте голосовой runtime";
  const dictationHint = state.ptt.sttAvailable && state.ptt.hotkeyAvailable
    ? "F8 — диктовка в активное поле."
    : pttDiagnosticText();
  byId("voiceNote").textContent = available
    ? `Полный ответ — в чате. Говорите, чтобы перебить. ${dictationHint}`
    : captureAvailable
      ? `${voiceDiagnosticText() || "Нужны Faster-Whisper и доступный локальный OmniVoice-Fast server."} ${pttDiagnosticText()}`
      : "Electron не предоставляет доступ к микрофону в текущем окружении.";
  const audioImport = byId("meetingAudioImportButton");
  if (audioImport) {
    audioImport.disabled = state.meetingImporting || !state.ptt.sttAvailable;
    audioImport.title = state.ptt.sttAvailable
      ? "Распознать запись локальным Faster-Whisper"
      : state.ptt.sttDetail;
  }
}

function renderPilotMetrics() {
  const summary = state.snapshot.pilot_metrics || {};
  const metrics = summary.metrics || {};
  const count = Number(summary.sample_count || 0);
  byId("pilotMetricsCount").textContent = String(count);
  const parts = [];
  const transcript = metrics.transcript_ready_seconds;
  const firstAudio = metrics.first_audio_seconds;
  const listenReady = metrics.listen_ready_seconds;
  const ttsRtf = metrics.tts_rtf;
  const clipping = metrics.output_clipping_ratio;
  const cleanWer = metrics.stt_clean_wer;
  const corporateWer = metrics.stt_corporate_wer;
  if (listenReady) parts.push(`готовность p95 ${Number(listenReady.p95).toFixed(2)} с`);
  if (transcript) parts.push(`текст p50/p95 ${Number(transcript.p50).toFixed(2)}/${Number(transcript.p95).toFixed(2)} с`);
  if (firstAudio) parts.push(`звук p50/p95 ${Number(firstAudio.p50).toFixed(2)}/${Number(firstAudio.p95).toFixed(2)} с`);
  if (ttsRtf) parts.push(`TTS RTF p95 ${Number(ttsRtf.p95).toFixed(2)}`);
  if (cleanWer) parts.push(`WER обычная ${(Number(cleanWer.p95) * 100).toFixed(1)}%`);
  if (corporateWer) parts.push(`WER корпоративная ${(Number(corporateWer.p95) * 100).toFixed(1)}%`);
  if (clipping) parts.push(`клиппинг ${(Number(clipping.max) * 100).toFixed(3)}%`);
  byId("pilotMetricsSummary").textContent = parts.length
    ? parts.join(" · ")
    : "Сделайте несколько голосовых запросов на этом устройстве.";
  const usage = summary.usage || {};
  const activeDays = Number(usage.active_days || 0);
  const completedTurns = Number(usage.completed_turns || 0);
  const voiceTurns = Number(usage.voice_turns || 0);
  const meetingImports = Number(usage.meeting_imports || 0);
  const meetingBriefings = Number(usage.meeting_briefings || 0);
  const observedExits = Number(usage.observed_session_exits || 0);
  const cleanExits = Number(usage.clean_session_exits || 0);
  const crashFreeRate = usage.crash_free_session_rate == null
    ? null
    : Number(usage.crash_free_session_rate);
  const firstValueSeconds = usage.first_value_seconds == null
    ? null
    : Number(usage.first_value_seconds);
  const usageParts = [
    `активных дней: ${activeDays}`,
    `запросов: ${completedTurns}`,
    `голосом: ${voiceTurns}`,
  ];
  if (meetingImports > 0) usageParts.push(`встреч импортировано: ${meetingImports}`);
  if (meetingBriefings > 0) usageParts.push(`брифингов: ${meetingBriefings}`);
  if (observedExits > 0 && crashFreeRate != null && Number.isFinite(crashFreeRate)) {
    usageParts.push(
      `штатных завершений: ${(crashFreeRate * 100).toFixed(1)}% (${cleanExits}/${observedExits})`,
    );
  } else {
    usageParts.push("надёжность: ожидает завершений");
  }
  if (firstValueSeconds != null && Number.isFinite(firstValueSeconds)) {
    usageParts.push(`первый результат: ${(firstValueSeconds / 60).toFixed(1)} мин`);
  }
  byId("pilotUsageSummary").textContent = usageParts.join(" · ");
  byId("pilotUsefulnessRating").value = String(Number(usage.usefulness_rating || 0));
}

function renderPilotPreflight() {
  const report = state.snapshot.pilot_preflight || {};
  const checks = Array.isArray(report.checks) ? report.checks : [];
  const overall = String(report.overall || "unknown");
  const overallLabels = {
    ready: "Готово",
    limited: "Ограниченно",
    blocked: "Не готово",
    unknown: "Проверяю",
  };
  const counts = report.counts || {};
  const blockers = Number(counts.block || 0);
  const pending = Number(counts.warn || 0) + Number(counts.unverified || 0);
  byId("pilotPreflightOverall").textContent = overallLabels[overall] || "Проверяю";
  byId("pilotPreflightSummary").textContent = checks.length
    ? `Проверок: ${checks.length} · блокирует: ${blockers} · требует внимания: ${pending}`
    : "Определяю готовность этой установки к пилоту.";
  const list = byId("pilotPreflightList");
  list.replaceChildren();
  const styles = {
    pass: { className: "ready", symbol: "✓" },
    warn: { className: "planned", symbol: "!" },
    block: { className: "error", symbol: "×" },
    unverified: { className: "partial", symbol: "?" },
  };
  for (const check of checks) {
    const style = styles[String(check.status)] || styles.unverified;
    const row = textNode("li", style.className, "");
    row.append(textNode("span", "", style.symbol));
    const copy = textNode("div", "", "");
    copy.append(textNode("strong", "", String(check.title || "Проверка")));
    const detail = [check.detail, check.action].filter(Boolean).join(" ");
    copy.append(textNode("small", "", detail));
    row.append(copy);
    list.append(row);
  }
}

function renderPilotOnboarding() {
  const onboarding = state.snapshot.pilot_onboarding || {};
  const progress = onboarding.progress || {};
  const completed = Math.max(0, Number(progress.completed || 0));
  const total = Math.max(1, Number(progress.total || 4));
  byId("pilotOnboardingTitle").textContent = String(onboarding.title || "Быстрый старт");
  byId("pilotOnboardingDetail").textContent = String(
    onboarding.detail || "Определяю следующий полезный шаг.",
  );
  byId("pilotOnboardingProgress").textContent = `${Math.min(completed, total)} из ${total}`;
  const button = byId("pilotOnboardingButton");
  const actionId = String(onboarding.action_id || "");
  button.dataset.actionId = actionId;
  button.textContent = String(onboarding.action_label || "Продолжить");
  button.hidden = !actionId;
  byId("pilotOnboardingCard").dataset.status = String(onboarding.status || "active");
}

function activateWindowMode(mode) {
  if (state.mode === mode) return;
  setMode(mode);
  window.rndWorkbench.setWindowMode(mode);
}

function performPilotOnboardingAction() {
  const actionId = String(byId("pilotOnboardingButton").dataset.actionId || "");
  if (actionId === "review_preflight") {
    sendCommand("pilot_preflight");
    return;
  }
  if (actionId === "start_voice") {
    activateWindowMode("compact");
    setCompactView("voice");
    return;
  }
  if (actionId === "open_chat") {
    activateWindowMode("compact");
    setCompactView("chat");
    byId("composerInput").focus();
    return;
  }
  if (actionId === "show_meeting_import") {
    activateWindowMode("full");
    const diagnostics = byId("pilotOnboardingButton").closest("details");
    if (diagnostics) diagnostics.open = false;
    document.querySelector(".meeting-import-card")?.scrollIntoView({ block: "nearest" });
    byId("meetingTranscriptImportButton").focus();
    return;
  }
  if (actionId === "prepare_briefing") {
    activateWindowMode("compact");
    setCompactView("chat");
    const composer = byId("composerInput");
    composer.value = "/briefing ";
    composer.dispatchEvent(new Event("input", { bubbles: true }));
    composer.focus();
  }
}

function renderSnapshot() {
  renderMessages();
  renderTasks();
  renderApprovals();
  renderMeetings();
  renderContextLibrary();
  renderComposerSuggestions();
  renderRuntime();
  renderVoiceCapability();
  renderPilotMetrics();
  renderPilotPreflight();
  renderPilotOnboarding();
  const express = state.snapshot.express_connector || {};
  const expressButton = byId("expressSyncButton");
  expressButton.hidden = !Boolean(express.configured);
  expressButton.disabled = state.meetingImporting;
  expressButton.title = express.connected
    ? "Корпоративный read-only intake проверен"
    : "Подключение настроено, но ещё не проверено";
  byId("metricsLabel").textContent = state.metric;
}

function toast(message) {
  const node = byId("toast");
  node.textContent = message;
  node.classList.add("visible");
  clearTimeout(state.toastTimer);
  state.toastTimer = setTimeout(() => node.classList.remove("visible"), 4200);
}

function handleBackendEvent(event) {
  if (!event || typeof event !== "object") return;
  try {
    switch (event.type) {
      case "state":
        setVoicePhase(String(event.state || "idle"), String(event.detail || "Готов к работе"));
        if (event.state === "importing_meeting") {
          setMeetingImporting(true, String(event.import_kind || state.meetingImportKind || "package"));
        }
        break;
      case "capability":
        if (event.id === "windows_voice") {
          state.platform.voice_available = Boolean(event.available);
          state.platform.voice_diagnostics = event.diagnostics || {};
          renderVoiceCapability();
        }
        if (event.id === "windows_push_to_talk") {
          state.ptt.sttAvailable = Boolean(event.available);
          state.ptt.sttDetail = String(event.detail || "Faster-Whisper не настроен");
          renderVoiceCapability();
        }
        break;
      case "snapshot":
        if (event.data && typeof event.data === "object") {
          state.snapshot = event.data;
          state.runtime = event.data.llm || {};
          state.platform = event.data.platform || { voice_available: false };
          renderSnapshot();
        }
        break;
      case "voice_ready":
        setVoicePhase("listening", "Слушаю…");
        break;
      case "dictation_ready":
        state.metric = `Речь · ${event.seconds} с`;
        toast(`Распознано: ${String(event.text || "")}`);
        break;
      case "dictation_state":
        if (event.request_id === pttDictation.requestId) {
          state.ptt.phase = String(event.state || state.ptt.phase);
          document.body.dataset.pttState = state.ptt.phase;
          if (["ignored", "error", "unavailable", "cancelled", "busy"].includes(state.ptt.phase)) {
            toast(String(event.detail || "Диктовка F8 не завершена"));
            pttDictation.resetAfterResult();
          } else if (event.detail) {
            setVoicePhase(
              state.ptt.phase === "transcribing" ? "transcribing" : "listening",
              String(event.detail),
            );
          }
        }
        break;
      case "dictation_result":
        if (event.request_id === pttDictation.requestId) {
          const transcript = String(event.text || "").trim();
          state.ptt.pendingText = transcript.length <= 20000 ? transcript : "";
          setVoicePhase("transcribing", "Вставляю диктовку в активное поле…");
        }
        break;
      case "assistant_start":
        state.streamingText = "";
        state.metric = "Формирую ответ…";
        setResponsePending(true);
        renderSnapshot();
        break;
      case "assistant_delta":
        state.streamingText += String(event.text || "");
        renderMessages();
        break;
      case "assistant_end":
        state.streamingText = "";
        setResponsePending(false);
        if (event.interrupted) toast("Ответ остановлен");
        break;
      case "audio_start": audioPlayer.start(event); break;
      case "audio_chunk": audioPlayer.push(event); break;
      case "audio_end": audioPlayer.finish(); break;
      case "audio_cancel": audioPlayer.stop(String(event.reason || "backend_cancel")); break;
      case "metric":
        if (event.name === "stt") state.metric = `Распознавание · ${event.seconds} с`;
        if (event.name === "llm_first_token") state.metric = `Первый токен · ${event.seconds} с`;
        if (event.name === "first_audio") state.metric = `Первый звук · ${event.seconds} с`;
        if (event.name === "response_total") state.metric = `Ответ · ${event.seconds} с`;
        byId("metricsLabel").textContent = state.metric;
        break;
      case "llm_configured":
        state.runtime = event;
        renderRuntime();
        toast(String(event.detail || "Модель настроена"));
        break;
      case "routing_blocked":
        setResponsePending(false);
        toast(String(event.message || "Передача контекста заблокирована политикой данных"));
        break;
      case "plan_updated":
        byId("contextLibrary").open = true;
        state.metric = "План задачи обновлён";
        byId("metricsLabel").textContent = state.metric;
        toast(String(event.result?.message || state.metric));
        break;
      case "source_fragment":
        openSourceFragment(event.source);
        break;
      case "artifact_versions":
        renderArtifactHistory(event);
        break;
      case "artifact_restored":
        renderArtifactHistory(event);
        toast("Версия восстановлена как новая текущая версия");
        break;
      case "entity_deleted":
        if (event.entity === "artifact" && byId("artifactHistoryDialog").open) {
          byId("artifactHistoryDialog").close();
          state.artifactHistory = null;
        }
        toast(event.recovery === "trash" ? "Удалено с возможностью восстановления" : "Удалено");
        break;
      case "approval_resolved":
        toast(event.status === "rejected" ? "Действие отклонено" : "Действие завершено");
        break;
      case "approval_execution_pending":
        toast(String(event.result || "Действие ожидает сверки"));
        break;
      case "approval_execution_failed":
        toast(String(event.result || "Внешнее действие не выполнено"));
        break;
      case "synapse_package_imported": {
        setMeetingImporting(false);
        const result = event.result && typeof event.result === "object" ? event.result : {};
        const repeated = result.status === "already_imported";
        state.metric = repeated ? "Пакет уже в контексте" : "Встреча импортирована";
        byId("metricsLabel").textContent = state.metric;
        toast(repeated
          ? "Этот пакет eXpress (Синапс) уже есть в рабочем контексте"
          : "Встреча импортирована: можно готовить анализ и следующее собрание");
        break;
      }
      case "synapse_package_import_error":
        setMeetingImporting(false);
        state.metric = "Импорт встречи не выполнен";
        byId("metricsLabel").textContent = state.metric;
        toast(String(event.message || "Не удалось импортировать пакет eXpress (Синапс)"));
        break;
      case "express_sync_completed":
        setMeetingImporting(false);
        state.metric = Number(event.added || 0) > 0
          ? `Новых встреч: ${Number(event.added)}`
          : "Новых встреч eXpress нет";
        if (event.has_more) {
          state.metric += " · есть ещё";
        }
        byId("metricsLabel").textContent = state.metric;
        toast(state.metric);
        break;
      case "express_sync_error":
        setMeetingImporting(false);
        state.metric = "Синхронизация eXpress не выполнена";
        byId("metricsLabel").textContent = state.metric;
        toast(String(event.message || state.metric));
        break;
      case "meeting_briefing_ready":
        state.metric = "Брифинг встречи готов";
        byId("metricsLabel").textContent = state.metric;
        break;
      case "meeting_briefing_error":
        toast("Встреча сохранена, но брифинг нужно подготовить повторно");
        break;
      case "meeting_deleted":
        state.metric = "Встреча удалена";
        byId("metricsLabel").textContent = state.metric;
        toast(state.metric);
        break;
      case "meeting_transcript_imported":
        setMeetingImporting(false);
        state.metric = "Транскрипт встречи импортирован";
        byId("metricsLabel").textContent = state.metric;
        toast("Транскрипт eXpress добавлен: анализ встречи готов");
        break;
      case "meeting_transcript_import_error":
        setMeetingImporting(false);
        state.metric = "Импорт транскрипта не выполнен";
        byId("metricsLabel").textContent = state.metric;
        toast(String(event.message || "Не удалось импортировать транскрипт eXpress"));
        break;
      case "meeting_audio_imported":
        setMeetingImporting(false);
        state.metric = "Аудиозапись встречи распознана";
        byId("metricsLabel").textContent = state.metric;
        toast("Аудиозапись eXpress распознана локально, анализ встречи готов");
        break;
      case "meeting_audio_import_cancelled":
        setMeetingImporting(false);
        state.metric = "Импорт аудиозаписи отменён";
        byId("metricsLabel").textContent = state.metric;
        toast("Распознавание аудиозаписи отменено");
        break;
      case "meeting_audio_import_error":
        setMeetingImporting(false);
        state.metric = "Аудиозапись не импортирована";
        byId("metricsLabel").textContent = state.metric;
        toast(String(event.message || "Не удалось распознать аудиозапись eXpress"));
        break;
      case "pilot_metrics_exported":
        toast("Обезличенный отчёт пилота сохранён");
        break;
      case "pilot_feedback_saved":
        toast("Оценка полезности сохранена");
        break;
      case "pilot_preflight":
        if (event.result && typeof event.result === "object") {
          state.snapshot.pilot_preflight = event.result;
          renderPilotPreflight();
        }
        break;
      case "voice_qualification_started":
        state.voiceQualificationRunning = true;
        byId("voiceQualificationButton").textContent = "Остановить проверку";
        byId("voiceQualificationStatus").textContent = `Проверено 0 из ${Number(event.sample_count || 10)}`;
        break;
      case "voice_qualification_progress":
        state.voiceQualificationRunning = true;
        byId("voiceQualificationStatus").textContent = `Проверено ${Number(event.completed || 0)} из ${Number(event.total || 10)}`;
        break;
      case "voice_qualification_completed":
        state.voiceQualificationRunning = false;
        byId("voiceQualificationButton").textContent = "Проверить распознавание";
        byId("voiceQualificationStatus").textContent = "Проверка завершена. Аудио и текст не сохранены.";
        toast("Локальная проверка распознавания завершена");
        break;
      case "voice_qualification_cancelled":
        state.voiceQualificationRunning = false;
        byId("voiceQualificationButton").textContent = "Проверить распознавание";
        byId("voiceQualificationStatus").textContent = "Проверка отменена. Сохранены только уже измеренные числовые значения.";
        break;
      case "voice_qualification_error":
        state.voiceQualificationRunning = false;
        byId("voiceQualificationButton").textContent = "Проверить распознавание";
        byId("voiceQualificationStatus").textContent = "Проверка не выполнена. Убедитесь, что локальные STT и TTS готовы.";
        toast("Не удалось выполнить локальную проверку распознавания");
        break;
      case "capability_unavailable":
        if (event.capability === "push_to_talk") {
          state.ptt.sttAvailable = false;
          state.ptt.sttDetail = String(event.message || "Локальная диктовка недоступна");
        } else if (event.diagnostics) {
          state.platform.voice_diagnostics = event.diagnostics;
        }
        toast(String(event.message || "Функция пока недоступна"));
        renderVoiceCapability();
        break;
      case "speech_ignored":
        setVoicePhase("listening", "Не расслышал. Попробуйте ещё раз.");
        break;
      case "speech_error":
        toast(String(event.message || "Ошибка голосового тракта"));
        if (state.listening) setVoicePhase("listening", "Слушаю…");
        break;
      case "session_stopped":
        if (voiceCapture.active) void voiceCapture.stop({ notifyBackend: false });
        state.listening = false;
        renderVoiceCapability();
        break;
      case "fatal":
      case "error":
        setResponsePending(false);
        if (state.meetingImporting) setMeetingImporting(false);
        toast(String(event.message || "Ошибка RnD Workbench"));
        state.metric = "Нужна проверка";
        renderSnapshot();
        break;
    }
  } catch (_error) {
    audioPlayer.stop();
    toast("Получен некорректный аудиопоток");
  }
}

function sendText() {
  const input = byId("composerInput");
  const text = input.value.trim();
  if (!text) return;
  hideComposerSuggestions();
  input.value = "";
  input.style.height = "auto";
  state.streamingText = "";
  sendCommand("text", { text, speak: false });
  if (state.mode === "compact") setCompactView("chat");
}

function openSettings() {
  const settings = state.snapshot.settings || {};
  const providerType = String(state.runtime.provider_type || "");
  byId("providerInput").value = providerType === "corporate" ? "corporate" : "local";
  byId("endpointInput").value = String(settings.llm_base_url || "");
  byId("modelInput").value = String(settings.llm_model || "");
  byId("apiKeyInput").value = "";
  byId("settingsDialog").showModal();
}

function connectModel(event) {
  event.preventDefault();
  sendCommand("configure_llm", {
    mode: "external",
    provider_type: byId("providerInput").value,
    base_url: byId("endpointInput").value.trim(),
    model: byId("modelInput").value.trim(),
    api_key: byId("apiKeyInput").value,
  });
  byId("apiKeyInput").value = "";
  byId("settingsDialog").close();
}

async function toggleVoiceSession() {
  if (voiceCapture.active) {
    await voiceCapture.stop();
    return;
  }
  if (!state.platform.voice_available) {
    toast(voiceDiagnosticText() || "Голосовой runtime Windows не готов");
    return;
  }
  try {
    await voiceCapture.start();
  } catch (error) {
    state.listening = false;
    setVoicePhase("error", "Не удалось открыть микрофон");
    renderVoiceCapability();
    toast(`Микрофон недоступен (${error?.name || "Error"})`);
  }
}

async function chooseSynapsePackage() {
  if (state.meetingImporting) return;
  try {
    const packagePath = await window.rndWorkbench.chooseSynapsePackage();
    if (typeof packagePath !== "string" || !packagePath) return;
    setMeetingImporting(true, "package");
    state.metric = "Проверяю пакет встречи…";
    byId("metricsLabel").textContent = state.metric;
    sendCommand("import_synapse_package", {
      path: packagePath,
      workspace_id: String(state.snapshot.current_workspace_id || ""),
    });
  } catch (_error) {
    setMeetingImporting(false);
    toast("Не удалось открыть выбор пакета встречи");
  }
}

async function chooseMeetingTranscript() {
  if (state.meetingImporting) return;
  try {
    const transcriptPath = await window.rndWorkbench.chooseMeetingTranscript();
    if (typeof transcriptPath !== "string" || !transcriptPath) return;
    setMeetingImporting(true, "transcript");
    state.metric = "Импортирую транскрипт встречи…";
    byId("metricsLabel").textContent = state.metric;
    sendCommand("import_meeting_transcript", {
      path: transcriptPath,
      workspace_id: String(state.snapshot.current_workspace_id || ""),
    });
  } catch (_error) {
    setMeetingImporting(false);
    toast("Не удалось открыть выбор транскрипта");
  }
}

async function chooseMeetingAudio() {
  if (state.meetingImporting) return;
  if (!state.ptt.sttAvailable) {
    toast(state.ptt.sttDetail || "Сначала настройте локальный Faster-Whisper");
    return;
  }
  try {
    const audioPath = await window.rndWorkbench.chooseMeetingAudio();
    if (typeof audioPath !== "string" || !audioPath) return;
    setMeetingImporting(true, "audio");
    state.metric = "Распознаю аудиозапись локально…";
    byId("metricsLabel").textContent = state.metric;
    sendCommand("import_meeting_audio", {
      path: audioPath,
      workspace_id: String(state.snapshot.current_workspace_id || ""),
    });
  } catch (_error) {
    setMeetingImporting(false);
    toast("Не удалось открыть выбор аудиозаписи");
  }
}

function syncExpressMeetings() {
  if (state.meetingImporting) return;
  const connector = state.snapshot.express_connector || {};
  if (!connector.configured) {
    toast("Корпоративный intake eXpress не настроен администратором");
    return;
  }
  setMeetingImporting(true, "express");
  state.metric = "Получаю новые встречи eXpress…";
  byId("metricsLabel").textContent = state.metric;
  sendCommand("sync_express_meetings");
}

async function exportPilotMetrics() {
  try {
    const reportPath = await window.rndWorkbench.choosePilotMetricsExport();
    if (typeof reportPath !== "string" || !reportPath) return;
    sendCommand("export_pilot_metrics", { path: reportPath });
  } catch (_error) {
    toast("Не удалось выбрать файл для сводки качества");
  }
}

function fieldLooksSecure(element) {
  if (!(element instanceof HTMLElement)) return false;
  const values = [
    element.getAttribute("type"),
    element.getAttribute("autocomplete"),
    element.getAttribute("name"),
    element.getAttribute("id"),
    element.getAttribute("aria-label"),
    element.getAttribute("data-sensitive"),
  ].filter(Boolean).join(" ").toLowerCase();
  return element instanceof HTMLInputElement && element.type === "password"
    || /(password|passwd|парол|secret|token|one-time|otp|\bpin\b)/i.test(values);
}

function insertPttTextIntoActiveField(text) {
  const active = document.activeElement;
  if (!(active instanceof HTMLElement) || fieldLooksSecure(active)) {
    return { success: false, detail: fieldLooksSecure(active) ? "В защищённые поля диктовка не вставляется" : "Нет активного текстового поля" };
  }
  if (active instanceof HTMLTextAreaElement || active instanceof HTMLInputElement) {
    const supportedInputTypes = new Set(["text", "search", "email", "url", "tel"]);
    if (
      active.disabled
      || active.readOnly
      || (active instanceof HTMLInputElement && !supportedInputTypes.has(active.type))
    ) {
      return { success: false, detail: "Активное поле не принимает текст" };
    }
    const start = active.selectionStart ?? active.value.length;
    const end = active.selectionEnd ?? start;
    active.setRangeText(text, start, end, "end");
    active.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertText", data: text }));
    return { success: true, detail: "Диктовка вставлена в активное поле" };
  }
  const editable = active.closest("[contenteditable]");
  if (editable instanceof HTMLElement && editable.isContentEditable && !fieldLooksSecure(editable)) {
    editable.focus();
    const inserted = document.execCommand("insertText", false, text);
    return inserted
      ? { success: true, detail: "Диктовка вставлена в активное поле" }
      : { success: false, detail: "Активное поле отклонило текст" };
  }
  return { success: false, detail: "Нет активного текстового поля" };
}

function keepPttTextInComposer(text) {
  const normalized = String(text || "").trim();
  const composer = byId("composerInput");
  if (!normalized || !(composer instanceof HTMLTextAreaElement)) return false;
  const separator = composer.value && !/\s$/.test(composer.value) ? " " : "";
  composer.value = `${composer.value}${separator}${normalized}`;
  composer.dispatchEvent(new InputEvent("input", {
    bubbles: true,
    inputType: "insertText",
    data: normalized,
  }));
  state.ptt.pendingText = "";
  return true;
}

async function probePttMicrophonePermission() {
  if (!navigator.mediaDevices?.getUserMedia) {
    state.ptt.permission = "unavailable";
    renderVoiceCapability();
    return;
  }
  if (!navigator.permissions?.query) {
    state.ptt.permission = "unknown";
    renderVoiceCapability();
    return;
  }
  try {
    const permission = await navigator.permissions.query({ name: "microphone" });
    const apply = () => {
      state.ptt.permission = ["granted", "denied", "prompt"].includes(permission.state)
        ? permission.state
        : "unknown";
      renderVoiceCapability();
    };
    apply();
    permission.addEventListener("change", apply);
  } catch (_error) {
    state.ptt.permission = "unknown";
    renderVoiceCapability();
  }
}

byId("modeButton").addEventListener("click", requestModeToggle);
byId("minimizeButton").addEventListener("click", () => window.rndWorkbench.minimize());
byId("closeButton").addEventListener("click", () => window.rndWorkbench.close());
byId("settingsButton").addEventListener("click", openSettings);
byId("settingsCloseButton").addEventListener("click", () => byId("settingsDialog").close());
byId("settingsCancelButton").addEventListener("click", () => byId("settingsDialog").close());
byId("sourceFragmentCloseButton").addEventListener("click", () => byId("sourceFragmentDialog").close());
byId("artifactHistoryCloseButton").addEventListener("click", () => byId("artifactHistoryDialog").close());
byId("voiceTab").addEventListener("click", () => setCompactView("voice"));
byId("chatTab").addEventListener("click", () => setCompactView("chat"));
byId("newTaskButton").addEventListener("click", () => sendCommand("new_task", { title: "Новая задача" }));
byId("meetingAudioImportButton").addEventListener("click", () => void chooseMeetingAudio());
byId("meetingTranscriptImportButton").addEventListener("click", () => void chooseMeetingTranscript());
byId("synapseImportButton").addEventListener("click", () => void chooseSynapsePackage());
byId("expressSyncButton").addEventListener("click", syncExpressMeetings);
byId("meetingSelect").addEventListener("change", (event) => {
  sendCommand("select_meeting", { meeting_id: String(event.target.value || "") });
});
byId("prepareMeetingBriefingButton").addEventListener("click", () => {
  const meetingId = String(byId("meetingSelect").value || "");
  if (meetingId) sendCommand("prepare_briefing", { meeting_id: meetingId });
});
byId("deleteMeetingButton").addEventListener("click", () => {
  const meetingId = String(byId("meetingSelect").value || "");
  const option = byId("meetingSelect").selectedOptions[0];
  const title = String(option?.textContent || "эту встречу");
  if (meetingId && window.confirm(`Удалить «${title}» и связанные материалы?`)) {
    sendCommand("delete_meeting", { meeting_id: meetingId });
  }
});
byId("exportPilotMetricsButton").addEventListener("click", () => void exportPilotMetrics());
byId("pilotPreflightButton").addEventListener("click", () => sendCommand("pilot_preflight"));
byId("voiceQualificationButton").addEventListener("click", () => {
  sendCommand(state.voiceQualificationRunning
    ? "voice_qualification_cancel"
    : "voice_qualification");
});
byId("pilotOnboardingButton").addEventListener("click", performPilotOnboardingAction);
byId("pilotUsefulnessRating").addEventListener("change", (event) => {
  const rating = Number(event.currentTarget.value);
  if (Number.isInteger(rating) && rating >= 1 && rating <= 5) {
    sendCommand("set_pilot_feedback", { usefulness_rating: rating });
  }
});
byId("sendButton").addEventListener("click", sendText);
byId("stopButton").addEventListener("click", () => {
  audioPlayer.stop("user_stop");
  setResponsePending(false);
  sendCommand("stop");
});
byId("micButton").addEventListener("click", () => void toggleVoiceSession());
byId("composerInput").addEventListener("keydown", (event) => {
  const suggestions = availableComposerSuggestions();
  if (!event.isComposing && suggestions.length && ["ArrowDown", "ArrowUp"].includes(event.key)) {
    event.preventDefault();
    const delta = event.key === "ArrowDown" ? 1 : -1;
    state.composerSuggestionIndex = (
      state.composerSuggestionIndex + delta + suggestions.length
    ) % suggestions.length;
    renderComposerSuggestions();
    return;
  }
  if (!event.isComposing && suggestions.length && ["Tab", "Enter"].includes(event.key) && !event.shiftKey) {
    event.preventDefault();
    applyComposerSuggestion(suggestions[state.composerSuggestionIndex]);
    return;
  }
  if (event.key === "Escape" && suggestions.length) {
    event.preventDefault();
    hideComposerSuggestions();
    return;
  }
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    sendText();
  }
});
byId("composerInput").addEventListener("input", (event) => {
  state.composerSuggestionIndex = 0;
  event.currentTarget.style.height = "auto";
  event.currentTarget.style.height = `${Math.min(96, event.currentTarget.scrollHeight)}px`;
  renderComposerSuggestions();
});
byId("settingsForm").addEventListener("submit", connectModel);
document.addEventListener("keydown", (event) => {
  if (event.ctrlKey && event.shiftKey && event.key.toLowerCase() === "m") {
    event.preventDefault();
    requestModeToggle();
  }
});

window.addEventListener("beforeunload", () => {
  if (voiceCapture.active) sendCommand("voice_session_stop");
  if (pttDictation.requestId) {
    sendCommand("ptt_dictation_cancel", {
      request_id: pttDictation.requestId,
      reason: "window_close",
    });
    pttDictation.stopResources();
  }
});
window.rndWorkbench.onBackendEvent(handleBackendEvent);
window.rndWorkbench.onWindowMode(setMode);
window.rndWorkbench.onPttCapability((capability) => {
  state.ptt.hotkeyAvailable = capability?.available === true;
  state.ptt.hotkeyDetail = String(capability?.detail || "Системная F8 недоступна");
  renderVoiceCapability();
});
window.rndWorkbench.onPttKey((event) => { void pttDictation.handleKey(event); });
window.rndWorkbench.onPttInsert((payload) => {
  const requestId = typeof payload?.requestId === "string" ? payload.requestId : "";
  const text = typeof payload?.text === "string" ? payload.text : "";
  const result = !requestId || !text
    ? { success: false, detail: "Некорректный результат диктовки" }
    : !document.hasFocus()
      ? { success: false, detail: "Фокус изменился; текст не вставлен" }
      : insertPttTextIntoActiveField(text);
  window.rndWorkbench.reportPttInsertion({ requestId, ...result });
});
window.rndWorkbench.onPttInsertion((result) => {
  if (result?.requestId !== pttDictation.requestId) return;
  let detail = String(
    result?.detail || (result?.success ? "Диктовка вставлена" : "Не удалось вставить диктовку"),
  );
  const canKeepInComposer = result?.success !== true
    && !/(защищён|некорректный результат|отменена)/i.test(detail);
  if (canKeepInComposer && keepPttTextInComposer(state.ptt.pendingText)) {
    detail = "Текст не вставлен и сохранён в поле чата RnD Workbench";
  } else {
    state.ptt.pendingText = "";
  }
  pttDictation.resetAfterResult();
  toast(detail);
});
sendCommand("snapshot");
sendCommand("voice_capabilities");
sendCommand("ptt_capabilities");
setMode("compact");
setCompactView("voice");
setVoicePhase("idle", "Проверяю голос…");
void probePttMicrophonePermission();
