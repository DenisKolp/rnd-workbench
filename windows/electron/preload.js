"use strict";

const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("rndWorkbench", {
  sendCommand(command) {
    ipcRenderer.send("backend:command", command);
  },
  setWindowMode(mode) {
    ipcRenderer.send("window:set-mode", mode);
  },
  minimize() {
    ipcRenderer.send("window:minimize");
  },
  close() {
    ipcRenderer.send("window:close");
  },
  chooseSynapsePackage() {
    return ipcRenderer.invoke("synapse:choose-package");
  },
  chooseMeetingTranscript() {
    return ipcRenderer.invoke("meeting:choose-transcript");
  },
  chooseMeetingAudio() {
    return ipcRenderer.invoke("meeting:choose-audio");
  },
  choosePilotMetricsExport() {
    return ipcRenderer.invoke("pilot:export-metrics");
  },
  onBackendEvent(callback) {
    const listener = (_event, payload) => callback(payload);
    ipcRenderer.on("backend:event", listener);
    return () => ipcRenderer.removeListener("backend:event", listener);
  },
  onWindowMode(callback) {
    const listener = (_event, mode) => callback(mode);
    ipcRenderer.on("window:mode", listener);
    return () => ipcRenderer.removeListener("window:mode", listener);
  },
  onPttKey(callback) {
    const listener = (_event, payload) => callback(payload);
    ipcRenderer.on("ptt:key", listener);
    return () => ipcRenderer.removeListener("ptt:key", listener);
  },
  onPttCapability(callback) {
    const listener = (_event, payload) => callback(payload);
    ipcRenderer.on("ptt:capability", listener);
    return () => ipcRenderer.removeListener("ptt:capability", listener);
  },
  onPttInsert(callback) {
    const listener = (_event, payload) => callback(payload);
    ipcRenderer.on("ptt:insert", listener);
    return () => ipcRenderer.removeListener("ptt:insert", listener);
  },
  onPttInsertion(callback) {
    const listener = (_event, payload) => callback(payload);
    ipcRenderer.on("ptt:insertion", listener);
    return () => ipcRenderer.removeListener("ptt:insertion", listener);
  },
  reportPttInsertion(payload) {
    ipcRenderer.send("ptt:renderer-insertion-result", payload);
  },
});
