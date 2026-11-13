// Opt-in integration check. Own isolated CODEX_HOME/server; no model inference.
const {spawn} = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {createRequire} = require('node:module');
const WebSocket = createRequire('/usr/local/lib/node_modules/codex-relay/package.json')('ws');
const root = fs.mkdtempSync(path.join(os.tmpdir(), 'codex-disconnect-'));
const sock = path.join(root, 'server.sock');
const marker = path.join(root, 'completed');
const server = spawn('/usr/local/bin/codex-real', ['app-server', '--listen', `unix://${sock}`],
  {env: {...process.env, CODEX_HOME: root}, stdio: 'ignore'});
let ws;
let proxy;
let seq = 0;
const pending = new Map();
const delay = ms => new Promise(r => setTimeout(r, ms));
function rpc(method, params) {
  const id = ++seq;
  return new Promise((resolve, reject) => {
    pending.set(id, [resolve, reject]);
    ws.send(JSON.stringify({id, method, params}));
  });
}
const deadline = setTimeout(() => { server.kill('SIGKILL'); process.exit(2); }, 25000);
(async () => {
  try {
    for (let n = 0; !fs.existsSync(sock) && n < 100; n++) await delay(100);
    let clientSocket = sock;
    if (process.argv.includes('--proxy')) {
      clientSocket = path.join(root, 'proxy.sock');
      proxy = spawn(process.execPath, [path.resolve(__dirname, '../docker/codex-terminal-proxy.cjs'),
        clientSocket, path.join(root, 'proxy-state.json'), sock], {stdio:'ignore'});
      for (let n = 0; !fs.existsSync(clientSocket) && n < 50; n++) await delay(100);
    }
    ws = new WebSocket(`ws+unix://${clientSocket}:/`);
    await new Promise((resolve, reject) => {ws.once('open', resolve); ws.once('error', reject);});
    ws.on('message', raw => {
      const msg = JSON.parse(raw);
      const item = pending.get(msg.id);
      if (item) {
        pending.delete(msg.id);
        msg.error ? item[1](new Error(JSON.stringify(msg.error))) : item[0](msg.result);
      }
    });
    await rpc('initialize', {clientInfo: {name:'disconnect-test', version:'1'}, capabilities:{experimentalApi:true}});
    ws.send(JSON.stringify({method:'initialized'}));
    const {thread} = await rpc('thread/start', {cwd:root, approvalPolicy:'never', sandbox:'read-only'});
    if (proxy) {
      const tracked = JSON.parse(fs.readFileSync(path.join(root, 'proxy-state.json'), 'utf8'));
      if (!tracked.known || !tracked.threadIds.includes(thread.id)) throw new Error('actual thread not tracked');
    }
    await rpc('thread/shellCommand', {threadId:thread.id,
      command:`sleep 3; printf completed > '${marker}'`, timeoutMs:10000});
    let active = false;
    for (let n = 0; n < 20; n++) {
      const before = await rpc('thread/read', {threadId:thread.id, includeTurns:false});
      if (before.thread.status.type === 'active') { active = true; break; }
      await delay(50);
    }
    if (!active) throw new Error('test turn was not active');
    ws.terminate();
    for (let n = 0; !fs.existsSync(marker) && n < 100; n++) await delay(100);
    if (fs.readFileSync(marker, 'utf8') !== 'completed') throw new Error('task did not complete');
    console.log('PASS: isolated app-server shell turn completed after sole client disconnected (no inference).');
  } catch (e) {
    console.error(e.message); process.exitCode = 1;
  } finally {
    ws?.terminate();
    if (proxy) {
      proxy.kill('SIGTERM');
      await Promise.race([new Promise(r => proxy.once('exit', r)), delay(2000)]);
      if (proxy.exitCode === null && proxy.signalCode === null) proxy.kill('SIGKILL');
    }
    server.kill('SIGTERM');
    await Promise.race([new Promise(r => server.once('exit', r)), delay(2000)]);
    if (server.exitCode === null && server.signalCode === null) server.kill('SIGKILL');
    clearTimeout(deadline);
    // Keep the tiny isolated directory as verification evidence; never touch real sessions.
    console.log('Evidence directory:', root);
  }
})();
