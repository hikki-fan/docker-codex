#!/usr/bin/env node
/*
 * Observe Codex's shared app-server and feed only turn lifecycle state to the
 * account supervisor.  This is deliberately a read-only app-server client:
 * it never starts a turn, reads prompts, or handles approvals.
 *
 * The monitor is conservative by construction.  It writes UNKNOWN whenever
 * the socket or a thread resync fails; the supervisor then refuses a quota
 * cutover until a fresh active/idle snapshot is available.
 */

import os from "node:os";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { pathToFileURL } from "node:url";

const relayRoot = process.env.CODEX_RELAY_ROOT || "/usr/local/lib/node_modules/codex-relay";
const wsModule = await import(pathToFileURL(
  process.env.CODEX_RELAY_WS_MODULE || path.join(relayRoot, "node_modules/ws/index.js"),
).href);
const WebSocket = wsModule.default || wsModule.WebSocket || wsModule;

const home = process.env.HOME || os.homedir();
const socketPath = process.env.CODEX_SUPERVISOR_APP_SERVER_SOCKET ||
  path.join(home, ".codex", "app-server-control", "app-server-control.sock");
const source = process.env.CODEX_SUPERVISOR_SOURCE || "/workspace/codex-account-supervisor";
const config = process.env.CODEX_SUPERVISOR_CONFIG || "/home/codex/.codex-supervisor/config.toml";
const python = process.env.CODEX_SUPERVISOR_PYTHON || "python3";
const pollMs = Number(process.env.CODEX_SUPERVISOR_MONITOR_POLL_MS || 5000);
const resyncMs = Number(process.env.CODEX_SUPERVISOR_MONITOR_RESYNC_MS || 30000);
const requestTimeoutMs = Number(process.env.CODEX_SUPERVISOR_MONITOR_REQUEST_TIMEOUT_MS || 5000);
const heartbeatMs = Number(process.env.CODEX_SUPERVISOR_MONITOR_HEARTBEAT_MS || 5000);
const endpoint = `ws+unix://${socketPath}:/`;

let socket;
let nextId = 1;
let pending = new Map();
let activeTurns = new Map();
let knownThreads = new Set();
let reconnectTimer;
let pollTimer;
let resyncTimer;
let heartbeatTimer;
let shuttingDown = false;
let reconnectDelay = 1000;
let resyncInFlight = false;

function log(message) {
  process.stderr.write(`[codex-supervisor-turn-monitor] ${message}\n`);
}

function runSignal(action, active = []) {
  const args = ["-m", "codex_account_supervisor", "turn-signal", action, "--config", config];
  if (action === "sync") {
    for (const [turnId, threadId] of active) {
      args.push("--active-turn", `${turnId}\t${threadId || ""}`);
    }
  }
  const result = spawnSync(python, args, {
    env: { ...process.env, PYTHONPATH: source },
    stdio: ["ignore", "ignore", "pipe"],
    timeout: 5000,
  });
  if (result.error || result.status !== 0) {
    log(`signal_failed action=${action}`);
    return false;
  }
  return true;
}

function setUnknown() {
  runSignal("unknown");
}

function syncActive() {
  return runSignal(
    "sync",
    [...activeTurns.entries()].map(([turnId, value]) => [turnId, value.threadId]),
  );
}

function isActiveTurn(turn) {
  if (!turn || typeof turn !== "object") return false;
  if (turn.completedAt !== null && turn.completedAt !== undefined) return false;
  const status = typeof turn.status === "string"
    ? turn.status
    : turn.status && typeof turn.status === "object" && "type" in turn.status
      ? String(turn.status.type)
      : "";
  return ["active", "inprogress", "running"].includes(status.toLowerCase().replace(/[^a-z0-9]/g, ""));
}

function isRunningStatus(status) {
  const value = typeof status === "string"
    ? status
    : status && typeof status === "object" && "type" in status
      ? String(status.type)
      : "";
  return ["active", "running", "inprogress"].includes(value.toLowerCase().replace(/[^a-z0-9]/g, ""));
}

function isNonRunningStatus(status) {
  const value = typeof status === "string"
    ? status
    : status && typeof status === "object" && "type" in status
      ? String(status.type)
      : "";
  return ["idle", "notloaded"].includes(value.toLowerCase().replace(/[^a-z0-9]/g, ""));
}

function isActiveWriterError(error) {
  const message = String(error?.message || error || "").toLowerCase();
  return message.includes("already has an active writer") || message.includes("active writer");
}

function request(method, params) {
  if (!socket || socket.readyState !== WebSocket.OPEN) return Promise.reject(new Error("socket unavailable"));
  const id = nextId++;
  return new Promise((resolve, reject) => {
    let timeout;
    const settle = {
      resolve: (value) => {
        clearTimeout(timeout);
        resolve(value);
      },
      reject: (error) => {
        clearTimeout(timeout);
        reject(error);
      },
    };
    pending.set(id, settle);
    timeout = setTimeout(() => {
      const item = pending.get(id);
      if (!item) return;
      pending.delete(id);
      item.reject(new Error(`app-server request timed out: ${method}`));
    }, requestTimeoutMs);
    socket.send(JSON.stringify({ id, method, params }), (error) => {
      if (!error) return;
      const item = pending.get(id);
      if (!item) return;
      pending.delete(id);
      item.reject(error);
    });
  });
}

function resolvePending(error) {
  for (const item of pending.values()) item.reject(error);
  pending.clear();
}

async function resumeThread(threadId, targetTurns = activeTurns, listedThread = null) {
  const response = await request("thread/resume", {
    threadId,
    excludeTurns: true,
    initialTurnsPage: { itemsView: "summary", limit: 4, sortDirection: "desc" },
  });
  const thread = response?.thread || {};
  const turns = Array.isArray(response?.initialTurnsPage?.data)
    ? response.initialTurnsPage.data
    : Array.isArray(thread.turns) ? thread.turns : [];
  const active = [...turns].reverse().find(isActiveTurn);
  const runningKey = `status:${threadId}`;
  const retainedKey = active?.id || (isRunningStatus(thread.status) ? runningKey : null);
  // Reconciliation must remove stale turn IDs even when the latest thread
  // snapshot is terminal. The old implementation only deleted them when a
  // new active turn was found, which could keep DRAIN permanently active after
  // a terminal event was missed during a Relay reconnect.
  for (const [key, value] of [...targetTurns.entries()]) {
    if (value?.threadId === threadId && key !== retainedKey) targetTurns.delete(key);
  }
  if (active?.id) targetTurns.set(active.id, { threadId });
  else if (isRunningStatus(thread.status)) targetTurns.set(runningKey, { threadId });
}

async function resync() {
  if (resyncInFlight) return false;
  resyncInFlight = true;
  const nextTurns = new Map();
  const nextThreads = new Set();
  try {
    const listed = await request("thread/list", {
      archived: false,
      limit: 120,
      sortDirection: "desc",
      sortKey: "recency_at",
      sourceKinds: ["cli", "vscode", "exec", "appServer"],
    });
    const threads = Array.isArray(listed?.data) ? listed.data : [];
    let failures = 0;
    for (const thread of threads) {
      const threadId = typeof thread?.id === "string"
        ? thread.id
        : typeof thread?.threadId === "string" ? thread.threadId : "";
      if (!threadId) continue;
      nextThreads.add(threadId);
      // Idle and notLoaded entries cannot contain a live turn. Avoid
      // needlessly resuming every historical thread on each 30-second scan;
      // this also avoids active-writer locks held by an idle foreground CLI.
      if (isNonRunningStatus(thread.status)) continue;
      try {
        await resumeThread(threadId, nextTurns, thread);
      } catch (error) {
        // An active writer prevents a read-only thread/resume, but it is
        // only a turn blocker when the authoritative list also reports a
        // running status. An idle/notLoaded writer is a client lock, not an
        // in-flight turn, so do not poison the reconciliation with UNKNOWN.
        if (isActiveWriterError(error)) {
          if (isRunningStatus(thread?.status)) {
            nextTurns.set(`status:${threadId}`, { threadId });
          }
        } else {
          failures += 1;
        }
      }
    }
    if (failures > 0) {
      setUnknown();
      log(`resync_incomplete threads=${threads.length} failures=${failures}`);
      return false;
    }
    activeTurns = nextTurns;
    knownThreads = nextThreads;
    const ok = syncActive();
    log(`resync_ok threads=${threads.length} active=${activeTurns.size}`);
    return ok;
  } finally {
    resyncInFlight = false;
  }
}

async function discoverNewThreads() {
  if (!socket || socket.readyState !== WebSocket.OPEN) return;
  const listed = await request("thread/list", {
    archived: false,
    limit: 120,
    sortDirection: "desc",
    sortKey: "recency_at",
    sourceKinds: ["cli", "vscode", "exec", "appServer"],
  });
  const threads = Array.isArray(listed?.data) ? listed.data : [];
  for (const thread of threads) {
    const threadId = typeof thread?.id === "string"
      ? thread.id
      : typeof thread?.threadId === "string" ? thread.threadId : "";
    if (!threadId || knownThreads.has(threadId)) continue;
    knownThreads.add(threadId);
    if (isNonRunningStatus(thread.status)) continue;
    try {
      await resumeThread(threadId, activeTurns, thread);
      syncActive();
    } catch (error) {
      if (isActiveWriterError(error)) {
        if (isRunningStatus(thread?.status)) {
          activeTurns.set(`status:${threadId}`, { threadId });
        }
        syncActive();
      } else {
        setUnknown();
        log("new_thread_resubscribe_failed");
      }
    }
  }
}

function onMessage(raw) {
  let message;
  try {
    message = JSON.parse(String(raw));
  } catch {
    return;
  }
  if (typeof message.method === "string" && message.id !== undefined) {
    // The observer is not a task client and must never approve/answer a
    // server request.  Reject it explicitly so the app-server is not left
    // waiting on this read-only connection.
    socket?.send(JSON.stringify({
      id: message.id,
      error: { code: -32601, message: "turn observer does not handle server requests" },
    }));
    return;
  }
  if (typeof message.method === "string") {
    const params = message.params && typeof message.params === "object" ? message.params : {};
    const threadId = typeof params.threadId === "string" ? params.threadId : "";
    const turnId = typeof params.turnId === "string"
      ? params.turnId
      : params.turn && typeof params.turn.id === "string" ? params.turn.id : "";
    if (message.method === "turn/started" && threadId && turnId) {
      activeTurns.set(turnId, { threadId });
      syncActive();
    } else if (["turn/completed", "turn/failed", "turn/aborted", "turn/cancelled"].includes(message.method) && threadId) {
      if (turnId) activeTurns.delete(turnId);
      for (const [key, value] of activeTurns) if (value.threadId === threadId && key.startsWith("status:")) activeTurns.delete(key);
      syncActive();
    } else if (message.method === "thread/status/changed" && threadId) {
      if (isRunningStatus(params.status)) activeTurns.set(`status:${threadId}`, { threadId });
      else activeTurns.delete(`status:${threadId}`);
      syncActive();
    }
    return;
  }
  const item = pending.get(message.id);
  if (!item) return;
  pending.delete(message.id);
  if (message.error) item.reject(new Error(String(message.error.message || "app-server request failed")));
  else item.resolve(message.result);
}

async function connectOnce() {
  await new Promise((resolve, reject) => {
    const candidate = new WebSocket(endpoint, { perMessageDeflate: false });
    socket = candidate;
    const fail = (error) => {
      candidate.removeAllListeners();
      if (socket === candidate) socket = undefined;
      reject(error instanceof Error ? error : new Error("websocket error"));
    };
    candidate.once("open", () => {
      candidate.off("error", fail);
      resolve();
    });
    candidate.once("error", fail);
    candidate.on("message", onMessage);
    candidate.on("close", () => {
      if (socket !== candidate || shuttingDown) return;
      socket = undefined;
      resolvePending(new Error("app-server socket closed"));
      setUnknown();
      scheduleReconnect();
    });
  });
  socket.once("error", () => {});
  await request("initialize", {
    clientInfo: { name: "codex-account-supervisor", title: "turn observer", version: "1.0.0" },
    capabilities: { experimentalApi: true, requestAttestation: false },
  });
  socket.send(JSON.stringify({ method: "initialized" }));
  reconnectDelay = 1000;
  await resync();
  log("connected");
}

function scheduleReconnect() {
  if (shuttingDown || reconnectTimer) return;
  reconnectTimer = setTimeout(async () => {
    reconnectTimer = undefined;
    try {
      await connectOnce();
    } catch {
      setUnknown();
      reconnectDelay = Math.min(reconnectDelay * 2, 10000);
      scheduleReconnect();
    }
  }, reconnectDelay);
}

async function start() {
  log(`starting socket=${socketPath}`);
  try {
    await connectOnce();
  } catch {
    setUnknown();
    scheduleReconnect();
  }
  pollTimer = setInterval(() => discoverNewThreads().catch(() => setUnknown()), pollMs);
  // Events are normally sufficient, but periodic authoritative reconciliation
  // repairs missed terminal events and stale active-turn entries after a
  // Relay/app-server reconnect. It does not mark UNKNOWN during a successful
  // scan, avoiding a transient false drain failure.
  resyncTimer = setInterval(() => resync().catch(() => setUnknown()), resyncMs);
  heartbeatTimer = setInterval(() => {
    if (socket?.readyState === WebSocket.OPEN) runSignal("heartbeat");
    else setUnknown();
  }, heartbeatMs);
}

async function stop() {
  if (shuttingDown) return;
  shuttingDown = true;
  clearInterval(pollTimer);
  clearInterval(resyncTimer);
  clearInterval(heartbeatTimer);
  if (reconnectTimer) clearTimeout(reconnectTimer);
  setUnknown();
  resolvePending(new Error("monitor stopping"));
  socket?.close();
  setTimeout(() => process.exit(0), 100);
}

process.on("SIGTERM", stop);
process.on("SIGINT", stop);
await start();
