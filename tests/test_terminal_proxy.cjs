'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const os = require('node:os');
const path = require('node:path');
const {spawn} = require('node:child_process');
const {once} = require('node:events');
const {test} = require('node:test');
const {createRequire} = require('node:module');
const WebSocket = createRequire('/usr/local/lib/node_modules/codex-relay/package.json')('ws');
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));

async function eventually(predicate) {
  for (let attempt = 0; attempt < 200; attempt++) {
    if (predicate()) return;
    await delay(10);
  }
  assert.fail('Condition did not become true');
}

async function fixture(t, handshakeDelay = 0, wrapParent = false) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'terminal-proxy-'));
  const upstreamPath = path.join(directory, 'upstream.sock');
  const socketPath = path.join(directory, 'client.sock');
  const statePath = path.join(directory, 'state.json');
  const server = http.createServer();
  const wss = new WebSocket.WebSocketServer({noServer: true});
  server.on('upgrade', (req, socket, head) => {
    setTimeout(() => wss.handleUpgrade(req, socket, head,
      ws => wss.emit('connection', ws)), handshakeDelay);
  });
  server.listen(upstreamPath);
  await once(server, 'listening');
  const clients = new Set();
  wss.on('connection', ws => { clients.add(ws); ws.on('error', () => {}); });
  const args = [path.join(__dirname, '../docker/codex-terminal-proxy.cjs'),
    socketPath, statePath, upstreamPath];
  const wrapper = `
    const child = require('node:child_process').spawn(process.execPath, process.argv.slice(1),
      {stdio: ['ignore', 'ignore', 'inherit']});
    process.on('SIGTERM', () => child.kill('SIGTERM'));
    child.on('exit', code => process.exit(code || 0));
  `;
  const proxy = spawn(process.execPath, wrapParent ? ['-e', wrapper, '--', ...args] : args,
    {stdio: ['ignore', 'ignore', 'pipe']});
  let stderr = '';
  proxy.stderr.on('data', data => { stderr += data; });
  const readState = () => JSON.parse(fs.readFileSync(statePath, 'utf8'));
  t.after(async () => {
    for (const client of clients) client.terminate();
    if (proxy.exitCode === null && proxy.signalCode === null) {
      const exited = once(proxy, 'exit');
      proxy.kill('SIGTERM');
      await exited;
    }
    if (wrapParent) await eventually(() => !fs.existsSync(socketPath));
    wss.close();
    await new Promise(resolve => server.close(resolve));
    fs.rmSync(directory, {recursive: true, force: true});
    assert.equal(stderr, '');
  });

  await eventually(() => fs.existsSync(statePath));
  const connect = async () => {
    const accepted = once(wss, 'connection');
    const client = new WebSocket(`ws+unix://${socketPath}:/`);
    clients.add(client);
    client.on('error', () => {});
    await once(client, 'open');
    return {client, accepted};
  };
  return {readState, connect, proxy, socketPath, statePath, upstreamPath, clients};
}

test('proxy exits and invalidates state after its parent is killed', {timeout: 10000}, async t => {
  const f = await fixture(t, 0, true);
  const {client, accepted} = await f.connect();
  const [upstream] = await accepted;
  await exchange(client, upstream, '{"id":1,"method":"thread/start","params":{}}',
    '{"id":1,"result":{"thread":{"id":"owned"}}}');
  const exited = once(f.proxy, 'exit');
  const disconnected = once(client, 'close');
  f.proxy.kill('SIGKILL');
  await exited;
  await disconnected;
  assert.deepEqual(f.readState(), {ready: false, known: false, threadIds: ['owned']});
  assert.equal(fs.existsSync(f.socketPath), false);
  assert.equal(fs.existsSync(f.upstreamPath), true);
});

async function exchange(client, upstream, request, response) {
  const incoming = once(upstream, 'message');
  client.send(request);
  const [forwarded, binary] = await incoming;
  assert.equal(forwarded.toString(), request);
  assert.equal(binary, false);
  const reply = once(client, 'message');
  upstream.send(response);
  const [received, receivedBinary] = await reply;
  assert.equal(received.toString(), response);
  assert.equal(receivedBinary, false);
}

test('forwards unchanged RPCs, tracks actual start/resume/fork IDs, and preserves binary/server requests',
  {timeout: 10000}, async t => {
    const f = await fixture(t, 40);
    assert.deepEqual(f.readState(), {ready: true, known: false, threadIds: []});
    assert.equal(fs.statSync(f.socketPath).mode & 0o777, 0o600);
    assert.equal(fs.statSync(f.statePath).mode & 0o777, 0o600);
    const {client, accepted} = await f.connect();
    // Send before the upstream handshake to exercise the bounded queue.
    client.send('{"id":1,"method":"initialize","params":{}}');
    const [upstream] = await accepted;
    const [initial] = await once(upstream, 'message');
    assert.equal(initial.toString(), '{"id":1,"method":"initialize","params":{}}');
    const initReply = once(client, 'message');
    upstream.send('{"id":1,"result":{}}');
    await initReply;
    for (const [id, method, threadId] of [[2, 'thread/start', 'created'],
      ['resume-id', 'thread/resume', 'actual-resumed'], [4, 'thread/fork', 'forked']]) {
      await exchange(client, upstream, JSON.stringify({id, method, params: {threadId: 'requested'}}),
        JSON.stringify({id, result: {thread: {id: threadId}}}));
    }
    assert.deepEqual(f.readState(), {ready: true, known: true,
      threadIds: ['created', 'actual-resumed', 'forked']});
    const pending = once(upstream, 'message');
    client.send('{"id":5,"method":"thread/resume","params":{}}');
    await pending;
    const request = '{"id":5,"method":"item/commandExecution/requestApproval","params":{}}';
    const downstreamRequest = once(client, 'message');
    upstream.send(request);
    assert.equal((await downstreamRequest)[0].toString(), request);
    assert.equal(f.readState().known, false);
    const approval = once(upstream, 'message');
    client.send('{"id":5,"result":{"decision":"decline"}}');
    assert.equal((await approval)[0].toString(), '{"id":5,"result":{"decision":"decline"}}');
    const actualReply = once(client, 'message');
    upstream.send('{"id":5,"result":{"thread":{"id":"another"}}}');
    await actualReply;
    assert.deepEqual(f.readState().threadIds, ['created', 'actual-resumed', 'forked', 'another']);
    for (const [source, destination] of [[client, upstream], [upstream, client]]) {
      const received = once(destination, 'message');
      const bytes = Buffer.from([0, 255, 12, 5]);
      source.send(bytes, {binary: true});
      const [forwarded, binary] = await received;
      assert.deepEqual(forwarded, bytes);
      assert.equal(binary, true);
    }
  });

test('errors and unsolicited results never establish a known thread', {timeout: 10000}, async t => {
  const f = await fixture(t);
  const {client, accepted} = await f.connect();
  const [upstream] = await accepted;
  await exchange(client, upstream, '{"id":1,"method":"thread/resume","params":{"threadId":"wrong"}}',
    '{"id":1,"error":{"code":-1,"message":"failed"}}');
  await exchange(client, upstream, '{"id":2,"method":"thread/fork","params":{}}',
    '{"id":2,"result":{"thread":{}}}');
  const received = once(client, 'message');
  upstream.send('{"id":999,"result":{"thread":{"id":"unsolicited"}}}');
  await received;
  assert.deepEqual(f.readState(), {ready: true, known: false, threadIds: []});
});

test('disconnects fail closed, sequential reconnects retain IDs, second live client is rejected, SIGTERM cleans up',
  {timeout: 10000}, async t => {
    const f = await fixture(t);
    let {client, accepted} = await f.connect();
    let [upstream] = await accepted;
    await exchange(client, upstream, '{"id":1,"method":"thread/start","params":{}}',
      '{"id":1,"result":{"thread":{"id":"first"}}}');
    const second = new WebSocket(`ws+unix://${f.socketPath}:/`);
    f.clients.add(second);
    const rejected = once(second, 'error');
    await rejected;
    assert.equal(f.readState().known, true);
    client.terminate();
    await eventually(() => !f.readState().ready);
    assert.deepEqual(f.readState(), {ready: false, known: false, threadIds: ['first']});
    ({client, accepted} = await f.connect());
    [upstream] = await accepted;
    await eventually(() => f.readState().ready);
    assert.equal(f.readState().known, false);
    await exchange(client, upstream, '{"id":1,"method":"thread/resume","params":{}}',
      '{"id":1,"result":{"thread":{"id":"second"}}}');
    assert.deepEqual(f.readState().threadIds, ['first', 'second']);
    upstream.terminate();
    await eventually(() => !f.readState().ready);
    assert.equal(f.readState().known, false);
    const exited = once(f.proxy, 'exit');
    f.proxy.kill('SIGTERM');
    assert.deepEqual(await exited, [0, null]);
    assert.equal(fs.existsSync(f.socketPath), false);
    assert.equal(fs.existsSync(f.upstreamPath), true);
  });

test('overlapping thread requests remain unknown until every association succeeds', {timeout: 10000}, async t => {
  const f = await fixture(t);
  const {client, accepted} = await f.connect();
  const [upstream] = await accepted;
  for (const errorFirst of [false, true]) {
    for (const id of [1, 2]) {
      const request = once(upstream, 'message');
      client.send(JSON.stringify({id, method: 'thread/resume', params: {}}));
      await request;
    }
    assert.equal(f.readState().known, false);
    const responses = [{id: 1, result: {thread: {id: 'observed'}}}, {id: 2, error: {code: -1}}];
    if (errorFirst) responses.reverse();
    for (const response of responses) {
      const reply = once(client, 'message');
      upstream.send(JSON.stringify(response));
      await reply;
      assert.equal(f.readState().known, false);
    }
  }
  await exchange(client, upstream, '{"id":3,"method":"thread/resume","params":{}}',
    '{"id":3,"result":{"thread":{"id":"confirmed"}}}');
  assert.deepEqual(f.readState(), {ready: true, known: true, threadIds: ['observed', 'confirmed']});
});

test('pending RPC bound disconnects fail closed', {timeout: 10000}, async t => {
  const f = await fixture(t);
  const {client, accepted} = await f.connect();
  await accepted;
  const closed = once(client, 'close');
  for (let id = 0; id <= 1024; id++) {
    client.send(JSON.stringify({id, method: 'thread/read', params: {}}));
  }
  await closed;
  assert.deepEqual(f.readState(), {ready: false, known: false, threadIds: []});
});

test('large histories pass through without tracking or retaining payloads; a later association recovers',
  {timeout: 15000}, async t => {
    const f = await fixture(t);
    const {client, accepted} = await f.connect();
    const [upstream] = await accepted;
    await exchange(client, upstream, '{"id":1,"method":"thread/start","params":{}}',
      '{"id":1,"result":{"thread":{"id":"first"}}}');
    const request = once(upstream, 'message');
    client.send('{"id":2,"method":"thread/resume","params":{}}');
    await request;
    const history = Buffer.from('{"id":2,"result":{"thread":{"id":"oversized","history":"' +
      'private-history-'.repeat(1200000) + '"}}}');
    assert.ok(history.length > 16 * 1024 * 1024);
    const received = once(client, 'message');
    const notification = new Promise(resolve => {
      function onMessage(raw) {
        if (raw.toString() === '{"method":"history/complete"}') {
          client.off('message', onMessage);
          resolve();
        }
      }
      client.on('message', onMessage);
    });
    upstream.send(history, {binary: false});
    upstream.send('{"method":"history/complete"}');
    const [forwarded, binary] = await received;
    assert.deepEqual(forwarded, history);
    assert.equal(binary, false);
    await notification;
    assert.deepEqual(f.readState(), {ready: true, known: false, threadIds: ['first']});
    assert.equal(fs.readFileSync(f.statePath, 'utf8').includes('private-history'), false);
    // Oversized messages clear old pending IDs: a late small reply cannot
    // accidentally establish confidence without a fresh association request.
    const staleReply = once(client, 'message');
    upstream.send('{"id":2,"result":{"thread":{"id":"stale"}}}');
    await staleReply;
    assert.equal(f.readState().known, false);
    await exchange(client, upstream, '{"id":3,"method":"thread/resume","params":{}}',
      '{"id":3,"result":{"thread":{"id":"recovered"}}}');
    assert.deepEqual(f.readState(), {ready: true, known: true, threadIds: ['first', 'recovered']});
    const largeUpload = once(upstream, 'message');
    client.send(history, {binary: true});
    const [upload, uploadBinary] = await largeUpload;
    assert.deepEqual(upload, history);
    assert.equal(uploadBinary, true);
    assert.deepEqual(f.readState(), {ready: true, known: false, threadIds: ['first', 'recovered']});
  });
