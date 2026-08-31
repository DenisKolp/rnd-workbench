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
  metric: "Готов к работе",
  toastTimer: null,
  meetingImporting: false,
  meetingImportKind: "",
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
  if (state.meetingImporting) state.meetingImportKind = activeKind;
  const audioButton = byId("meetingAudioImportButton");
  const transcriptButton = byId("meetingTranscriptImportButton");
  const packageButton = byId("synapseImportButton");
  if (audioButton) {
    audioButton.disabled = state.meetingImporting || !state.ptt.sttAvailable;
    audioButton.textContent = state.meetingImporting && activeKind === "audio"
      ? "Распознаю аудио…"
      : "Аудиозапись · Whisper";
    audioButton.title = state.ptt.sttAvailable
      ? "Распознать запись локальным Faster-Whisper"
      : state.ptt.sttDetail;
  }
  if (transcriptButton) {
    transcriptButton.disabled = state.meetingImporting;
    transcriptButton.textContent = state.meetingImporting && activeKind === "transcript"
      ? "Импортирую транскрипт…"
      : "Готовый транскрипт";
  }
  if (packageButton) {
    packageButton.disabled = state.meetingImporting;
    packageButton.textContent = state.meetingImporting && activeKind === "package"
      ? "Проверяю пакет…"
      : "Пакет с контекстом";
  }
  if (!state.meetingImporting) state.meetingImportKind = "";
}

function setMode(mode) {
  if (!new Set(["compact", "full"]).has(mode)) return;
  state.mode = mode;
  document.body.dataset.mode = mode;
  byId("modeButton").textContent = mode === "compact" ? "Полное окно" : "Компактно";
}

function requestModeToggle() {
  const next = state.mode === "compact" ? "full" : "compact";
  window.rndWorkbench.setWindowMode(next);
  setMode(next);
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
    this.stop();
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
      if (state.listening) setVoicePhase("listening", "Слушаю…");
    }
  }

  stop(reason = "") {
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
        hardware_measured: false,
      });
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
    this.starting = true;
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
    } finally {
      this.starting = false;
    }
    if (generation !== this.startGeneration) {
      for (const track of stream.getTracks()) track.stop();
      return;
    }
    const context = new AudioContextClass({ latencyHint: "interactive" });
    try {
      await context.resume();
    } catch (error) {
      for (const track of stream.getTracks()) track.stop();
      this.requestId = null;
      this.heldRequestId = null;
      state.ptt.permission = "error";
      state.ptt.phase = "error";
      sendCommand("ptt_dictation_cancel", { request_id: requestId, reason: "audio_context_error" });
      toast(`Диктовка F8: аудиоустройство недоступно (${error?.name || "Error"})`);
      renderVoiceCapability();
      return;
    }
    if (generation !== this.generation || this.heldRequestId !== requestId) {
      for (const track of stream.getTracks()) track.stop();
      await context.close();
      this.requestId = null;
      sendCommand("ptt_dictation_cancel", { request_id: requestId, reason: "released_during_audio_start" });
      state.ptt.phase = "idle";
      document.body.dataset.pttState = state.ptt.phase;
      renderVoiceCapability();
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
    renderVoiceCapability();
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
    sendCommand("voice_utterance_end", { duration_ms: Math.round(this.utteranceMs) });
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
    const open = textNode("button", "task-open", String(task.title || "Новая задача"));
    open.type = "button";
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
}

function renderRuntime() {
  const ready = Boolean(state.runtime.ready);
  const provider = state.runtime.provider_type;
  const route = ready && provider === "local" ? "local" : ready && provider === "corporate" ? "corporate" : "unconfigured";
  document.body.dataset.route = route;
  byId("routeLabel").textContent = route === "local"
    ? "локальная модель · данные на устройстве"
    : route === "corporate"
      ? "корпоративная модель · защищённый API"
      : state.runtime.base_url ? "модель требует настройки" : "модель не настроена";
  byId("sidebarStatus").textContent = route === "local" ? "Локальный контур" : route === "corporate" ? "Корпоративный контур" : "Нужна настройка";
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
  byId("voiceNote").textContent = available
    ? `Полный ответ остаётся в чате, голосом звучит короткая реплика. Говорите во время ответа, чтобы перебить. ${pttDiagnosticText()}`
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

function renderSnapshot() {
  renderMessages();
  renderTasks();
  renderRuntime();
  renderVoiceCapability();
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
        renderSnapshot();
        break;
      case "assistant_delta":
        state.streamingText += String(event.text || "");
        renderMessages();
        break;
      case "assistant_end":
        state.streamingText = "";
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
        toast(String(event.message || "Передача контекста заблокирована политикой данных"));
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
  input.value = "";
  input.style.height = "auto";
  state.streamingText = "";
  sendCommand("text", { text, speak: false });
  if (state.mode === "compact") setCompactView("chat");
}

function openSettings() {
  const settings = state.snapshot.settings || {};
  byId("providerInput").value = state.runtime.provider_type === "local" ? "local" : "corporate";
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
byId("voiceTab").addEventListener("click", () => setCompactView("voice"));
byId("chatTab").addEventListener("click", () => setCompactView("chat"));
byId("newTaskButton").addEventListener("click", () => sendCommand("new_task", { title: "Новая задача" }));
byId("meetingAudioImportButton").addEventListener("click", () => void chooseMeetingAudio());
byId("meetingTranscriptImportButton").addEventListener("click", () => void chooseMeetingTranscript());
byId("synapseImportButton").addEventListener("click", () => void chooseSynapsePackage());
byId("sendButton").addEventListener("click", sendText);
byId("stopButton").addEventListener("click", () => {
  audioPlayer.stop("user_stop");
  sendCommand("stop");
});
byId("micButton").addEventListener("click", () => void toggleVoiceSession());
byId("composerInput").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    sendText();
  }
});
byId("composerInput").addEventListener("input", (event) => {
  event.currentTarget.style.height = "auto";
  event.currentTarget.style.height = `${Math.min(96, event.currentTarget.scrollHeight)}px`;
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
setVoicePhase("idle", "Проверяю голосовой runtime…");
void probePttMicrophonePermission();
