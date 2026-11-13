// Transparent local transport; observe replies without generating any RPCs.
'use strict';
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const {createRequire} = require('node:module');
const WebSocket = createRequire('/usr/local/lib/node_modules/codex-relay/package.json')('ws');
const [socketPath, statePath, upstreamPath] = process.argv.slice(2);
if (!socketPath || !statePath || !upstreamPath ||
    new Set([socketPath, statePath, upstreamPath].map(p => path.resolve(p))).size !== 3) {
  console.error('Usage: node codex-terminal-proxy.cjs SOCKET_PATH STATE_PATH UPSTREAM_SOCKET');
  process.exit(1);
}

process.umask(0o077);
const MAX_PENDING = 1024;
const MAX_THREADS = 1024;
const MAX_QUEUE_MESSAGES = 128;
const MAX_QUEUE_BYTES = 8 * 1024 * 1024;
const MAX_BUFFER_BYTES = 16 * 1024 * 1024;
const MAX_TRACKING_BYTES = 1024 * 1024;
const threadMethods = new Set(['thread/start', 'thread/resume', 'thread/fork']);
const threadIds = new Set();
const server = http.createServer((_req, res) => { res.writeHead(404); res.end(); });
const wss = new WebSocket.WebSocketServer({noServer: true, maxPayload: 0});
let active = null;
let ownsSocket = false;
let stopping = false;
let parentTimer;

function state(ready, known) {
  const temporary = `${statePath}.${process.pid}.tmp`;
  try {
    fs.writeFileSync(temporary, JSON.stringify({ready, known, threadIds: [...threadIds]}),
      {mode: 0o600, flag: 'wx'});
    fs.renameSync(temporary, statePath);
  } catch (error) {
    try { fs.unlinkSync(temporary); } catch {}
    throw error;
  }
}

function shutdown(code) {
  if (stopping) return;
  stopping = true;
  clearInterval(parentTimer);
  try { state(false, false); } catch { code = 1; }
  if (active) endConnection(active, false);
  wss.close();
  server.close();
  if (ownsSocket) {
    try { fs.unlinkSync(socketPath); } catch (error) {
      if (error.code !== 'ENOENT') code = 1;
    }
  }
  process.exit(code);
}

function fatal() {
  // Never log transport errors: they can contain RPC payloads or private paths.
  console.error('Terminal proxy failed.');
  shutdown(1);
}

function endConnection(connection, writeState = true) {
  if (connection.ended) return;
  connection.ended = true;
  connection.pending.clear();
  connection.queue.length = 0;
  if (active === connection) {
    active = null;
    if (writeState) {
      try { state(false, false); } catch { fatal(); }
    }
  }
  connection.downstream.terminate();
  connection.upstream?.terminate();
}

function parse(connection, raw) {
  // Resume histories can be hundreds of MiB. Forward them without another
  // decoded string/object copy; unknown scope makes the guard use its global
  // read-only idle check until a later small association reply restores it.
  if (raw.length > MAX_TRACKING_BYTES) {
    connection.pending.clear();
    connection.confirmed = false;
    connection.associationFailed = true;
    try { state(connection.upstream.readyState === WebSocket.OPEN, false); }
    catch { fatal(); }
    return null;
  }
  try {
    const value = JSON.parse(raw.toString());
    return value && !Array.isArray(value) && typeof value === 'object' ? value : null;
  } catch { return null; }
}

function validId(id) {
  return (typeof id === 'string' && id.length <= 4096) ||
    (typeof id === 'number' && Number.isFinite(id));
}

function send(connection, target, raw, binary) {
  let budget = connection.sendBudgets.get(target);
  if (!budget) {
    budget = {bytes: 0, messages: 0, largeBytes: 0};
    connection.sendBudgets.set(target, budget);
  }
  const largeBytes = raw.length > MAX_BUFFER_BYTES ? raw.length : 0;
  // Exempt one large message itself, not the backlog behind it. Otherwise a
  // normal notification immediately following a large history would disconnect.
  if (target.readyState !== WebSocket.OPEN || budget.messages >= MAX_QUEUE_MESSAGES ||
      (largeBytes && budget.largeBytes) ||
      budget.bytes + raw.length - budget.largeBytes - largeBytes > MAX_BUFFER_BYTES) {
    endConnection(connection);
    return false;
  }
  budget.bytes += raw.length;
  budget.messages++;
  budget.largeBytes += largeBytes;
  target.send(raw, {binary}, error => {
    budget.bytes -= raw.length;
    budget.messages--;
    budget.largeBytes -= largeBytes;
    if (error) endConnection(connection);
  });
  return true;
}

server.on('upgrade', (request, socket, head) => {
  if (active || stopping) {
    socket.end('HTTP/1.1 409 Conflict\r\nConnection: close\r\nContent-Length: 0\r\n\r\n');
    return;
  }
  wss.handleUpgrade(request, socket, head, downstream => wss.emit('connection', downstream));
});

wss.on('connection', downstream => {
  const connection = {downstream, upstream: null, pending: new Map(), queue: [],
    queueBytes: 0, queueLargeBytes: 0, sendBudgets: new Map(),
    confirmed: false, associationFailed: false, ended: false};
  active = connection;
  try { state(false, false); } catch { fatal(); return; }
  downstream.on('error', () => endConnection(connection));
  downstream.on('close', () => endConnection(connection));
  downstream.on('message', (raw, binary) => {
    if (connection.ended) return;
    const message = parse(connection, raw);
    if (message && typeof message.method === 'string' && Object.hasOwn(message, 'id') &&
        !validId(message.id)) {
      endConnection(connection);
      return;
    }
    if (message && typeof message.method === 'string' && validId(message.id)) {
      if (connection.pending.size >= MAX_PENDING || connection.pending.has(message.id)) {
        endConnection(connection);
        return;
      }
      if (threadMethods.has(message.method)) {
        const alreadyAwaiting = [...connection.pending.values()].some(value => threadMethods.has(value));
        if (!alreadyAwaiting) connection.associationFailed = false;
        connection.confirmed = false;
        try { state(connection.upstream.readyState === WebSocket.OPEN, false); }
        catch { fatal(); return; }
      }
      connection.pending.set(message.id, threadMethods.has(message.method) ? message.method : null);
    }
    if (connection.upstream.readyState === WebSocket.CONNECTING) {
      // Permit a single large message, but bound the backlog behind it.
      const largeBytes = raw.length > MAX_QUEUE_BYTES ? raw.length : 0;
      if (connection.queue.length >= MAX_QUEUE_MESSAGES ||
          (largeBytes && connection.queueLargeBytes) ||
          connection.queueBytes + raw.length - connection.queueLargeBytes - largeBytes > MAX_QUEUE_BYTES) {
        endConnection(connection);
        return;
      }
      connection.queueBytes += raw.length;
      connection.queueLargeBytes += largeBytes;
      connection.queue.push([raw, binary]);
    } else {
      send(connection, connection.upstream, raw, binary);
    }
  });

  const upstream = connection.upstream = new WebSocket(`ws+unix://${upstreamPath}:/`,
    {handshakeTimeout: 10000, maxPayload: 0});
  upstream.on('error', () => endConnection(connection));
  upstream.on('close', () => endConnection(connection));
  upstream.on('open', () => {
    if (connection.ended) return;
    try { state(true, false); } catch { fatal(); return; }
    for (const [raw, binary] of connection.queue) {
      if (!send(connection, upstream, raw, binary)) break;
    }
    connection.queue.length = 0;
    connection.queueBytes = 0;
    connection.queueLargeBytes = 0;
  });
  upstream.on('message', (raw, binary) => {
    if (connection.ended) return;
    const message = parse(connection, raw);
    // Server requests have their own ID namespace and must pass through untouched.
    if (message && !Object.hasOwn(message, 'method') && validId(message.id) &&
        connection.pending.has(message.id)) {
      const method = connection.pending.get(message.id);
      connection.pending.delete(message.id);
      if (threadMethods.has(method)) {
        const id = message.result?.thread?.id;
        if (!Object.hasOwn(message, 'error') && typeof id === 'string' && id.length > 0 && id.length <= 4096) {
          if (!threadIds.has(id) && threadIds.size >= MAX_THREADS) {
            endConnection(connection);
            return;
          }
          threadIds.add(id);
          connection.confirmed = true;
        } else {
          connection.associationFailed = true;
        }
        const awaitingThread = [...connection.pending.values()].some(value => threadMethods.has(value));
        try { state(true, connection.confirmed && !awaitingThread && !connection.associationFailed); }
        catch { fatal(); return; }
      }
    }
    send(connection, downstream, raw, binary);
  });
});

server.on('error', fatal);
wss.on('error', fatal);
process.on('uncaughtException', fatal);
process.on('unhandledRejection', fatal);
process.on('SIGTERM', () => shutdown(0));
process.on('SIGINT', () => shutdown(0));
const initialParentPid = process.ppid;
parentTimer = setInterval(() => {
  if (process.ppid !== initialParentPid) shutdown(0);
}, 1000);
parentTimer.unref();
server.listen(socketPath, () => {
  ownsSocket = true;
  try {
    fs.chmodSync(socketPath, 0o600);
    state(true, false);
  } catch { fatal(); }
});
