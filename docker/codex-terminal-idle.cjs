// Read-only probe: never resume, subscribe, interrupt, or approve a thread.
const {createRequire} = require('node:module');
const {join} = require('node:path');
const {homedir} = require('node:os');
const {readFileSync} = require('node:fs');
const localRequire = createRequire('/usr/local/lib/node_modules/codex-relay/package.json');
const WebSocket = localRequire('ws');
const socketPath = join(process.env.CODEX_HOME || join(homedir(), '.codex'),
  'app-server-control', 'app-server-control.sock');
const ws = new WebSocket(`ws+unix://${socketPath}:/`);
let sequence = 0;
const pending = new Map();
const timer = setTimeout(() => process.exit(2), 10000);
ws.on('error', () => process.exit(2));
ws.on('message', raw => {
  try {
    const msg = JSON.parse(raw);
    const pair = pending.get(msg.id);
    if (pair) {
      pending.delete(msg.id);
      msg.error ? pair[1](new Error('RPC failed')) : pair[0](msg.result);
    }
  } catch { process.exit(2); }
});
function rpc(method, params) {
  const id = ++sequence;
  return new Promise((resolve, reject) => {
    pending.set(id, [resolve, reject]);
    ws.send(JSON.stringify({id, method, params}));
  });
}
ws.on('open', async () => {
  let result = 2;
  try {
    await rpc('initialize', {clientInfo: {name: 'terminal-idle-probe', version: '1.0'},
      capabilities: {experimentalApi: true}});
    ws.send(JSON.stringify({method: 'initialized'}));
    let ids;
    if (process.argv[2]) {
      try {
        const state = JSON.parse(readFileSync(process.argv[2], 'utf8'));
        if (state.ready === true && state.known === true &&
            Array.isArray(state.threadIds) && state.threadIds.length > 0 &&
            state.threadIds.every(id => typeof id === 'string' && id.length > 0)) {
          ids = state.threadIds;
        }
      } catch { /* Unknown ownership falls back to all loaded threads. */ }
    }
    if (!ids) {
      const loaded = await rpc('thread/loaded/list', {});
      if (!Array.isArray(loaded.data)) throw new Error('Unknown response');
      ids = loaded.data;
    }
    result = 0;
    for (const threadId of ids) {
      const {thread} = await rpc('thread/read', {threadId, includeTurns: false});
      if (!['idle', 'notLoaded'].includes(thread?.status?.type)) {
        result = 1;
        break;
      }
    }
  } catch { result = 2; }
  clearTimeout(timer);
  ws.close();
  // We have no subscriptions; closing this probe must never unload a TUI thread.
  setTimeout(() => process.exit(result), 50);
});
