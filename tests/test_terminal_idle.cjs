const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const http = require('node:http');
const {spawn} = require('node:child_process');
const {createRequire} = require('node:module');
const {WebSocketServer} = createRequire('/usr/local/lib/node_modules/codex-relay/package.json')('ws');
const probe = path.resolve(__dirname, '../docker/codex-terminal-idle.cjs');

async function check(state, statuses, expected, expectedReads) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'idle-test-'));
  fs.mkdirSync(path.join(root, 'app-server-control'));
  const statePath = path.join(root, 'state.json');
  fs.writeFileSync(statePath, JSON.stringify(state));
  const server = http.createServer();
  const wss = new WebSocketServer({server});
  const reads = [];
  wss.on('connection', ws => ws.on('message', raw => {
    const msg = JSON.parse(raw);
    if (!('id' in msg)) return;
    let result = {};
    if (msg.method === 'thread/loaded/list') result = {data: Object.keys(statuses)};
    else if (msg.method === 'thread/read') {
      reads.push(msg.params.threadId);
      result = {thread: {status: {type: statuses[msg.params.threadId]}}};
    } else assert.equal(msg.method, 'initialize', 'probe must be read-only');
    ws.send(JSON.stringify({id: msg.id, result}));
  }));
  await new Promise(r => server.listen(path.join(root, 'app-server-control/app-server-control.sock'), r));
  try {
    const proc = spawn(process.execPath, [probe, statePath], {env:{...process.env, CODEX_HOME:root}, stdio:'ignore'});
    const code = await new Promise(r => proc.once('exit', r));
    assert.equal(code, expected);
    assert.deepEqual(reads, expectedReads);
  } finally {
    for (const ws of wss.clients) ws.terminate();
    await new Promise(r => wss.close(r));
    await new Promise(r => server.close(r));
    fs.rmSync(root, {recursive:true, force:true});
  }
}
test('owned idle thread is reclaimable even when unrelated thread is active', () =>
  check({ready:true, known:true, threadIds:['mine']}, {mine:'idle', other:'active'}, 0, ['mine']));
test('owned active thread is retained', () =>
  check({ready:true, known:true, threadIds:['mine']}, {mine:'active'}, 1, ['mine']));
test('unknown ownership falls back to all loaded threads', () =>
  check({ready:true, known:false, threadIds:['mine']}, {mine:'idle', other:'active'}, 1, ['mine','other']));
test('unknown thread status is not treated as idle', () =>
  check({ready:true, known:true, threadIds:['mine']}, {mine:'unknown'}, 1, ['mine']));
