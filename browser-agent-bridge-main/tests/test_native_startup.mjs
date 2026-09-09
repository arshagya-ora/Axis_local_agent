import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { test } from 'node:test';
import vm from 'node:vm';

const source = await readFile(new URL('../extension/service-worker.js', import.meta.url), 'utf8');
const connect = source.slice(source.indexOf('async function connectNative()'), source.indexOf('async function pushSettingsToNative()'));
const alarm = source.slice(source.indexOf('async function ensureNativeHeartbeatAlarm()'), source.indexOf('async function clearNativeHeartbeatAlarm()'));
const pong = source.slice(source.indexOf('function markNativePong()'), source.indexOf('function setNativeStatus('));

function harness({ storageError, settingsError, alarmResult = () => Promise.resolve() } = {}) {
  const messages = [], statuses = [], order = [];
  let messageListener, disconnectListener, disconnected = false, reconnects = 0;
  const port = {
    onMessage: { addListener(fn) { messageListener = fn; } },
    onDisconnect: { addListener(fn) { disconnectListener = fn; } },
    disconnect() { disconnected = true; disconnectListener(); },
  };
  const sandbox = {
    chrome: {
      runtime: {
        id: 'fixture-extension', connectNative: () => port,
        getManifest: () => ({ version: 'fixture' }), sendMessage: () => Promise.resolve(),
      },
      storage: { local: {
        async get() { if (storageError) throw new Error(storageError); return { bridgePort: 9876 }; },
        set: () => Promise.resolve(),
      } },
      alarms: { create() { order.push('alarm'); return alarmResult(); } },
    },
    getBridgeEnabled: async () => true,
    clearTimeout() {}, clearNativeHeartbeatAlarm: async () => {},
    errorMessage: error => error.message,
    setNativeStatus(state, error) { statuses.push({ state, error }); },
    sendNativeNotification(method, params) { order.push(method); messages.push({ method, params }); },
    async pushSettingsToNative() { if (settingsError) throw new Error(settingsError); },
    sendNativePing() { order.push('ping'); },
    scheduleReconnect() { reconnects += 1; },
    rejectPendingNativeRequests() {},
  };
  vm.createContext(sandbox);
  vm.runInContext(`
    let nativePort = null, reconnectTimer = null, lastNativePongAt = null, nativeStatus = {};
    const NATIVE_HOST = 'fixture', NATIVE_HEARTBEAT_ALARM = 'heartbeat', NATIVE_HEARTBEAT_PERIOD_MINUTES = 0.5;
    ${connect}\n${alarm}\n${pong}
    globalThis.start = connectNative;
    globalThis.currentStatus = () => nativeStatus;
  `, sandbox);
  return { sandbox, messages, statuses, order,
    emitPong: () => messageListener({ type: 'pong' }),
    disconnected: () => disconnected, reconnects: () => reconnects };
}

test('connected is reported only after a native pong, and ready precedes heartbeat setup', async () => {
  const h = harness();
  await h.sandbox.start();
  assert.equal(h.statuses.at(-1).state, 'connecting');
  assert.deepEqual(h.order, ['extension.ready', 'alarm', 'ping']);
  assert.equal(h.messages[0].params.port, 9876);
  h.emitPong();
  assert.equal(h.sandbox.currentStatus().state, 'connected');
});

test('a void-returning alarm API cannot abort the native handshake', async () => {
  const h = harness({ alarmResult: () => undefined });
  await h.sandbox.start();
  assert.deepEqual(h.order, ['extension.ready', 'alarm', 'ping']);
  assert.equal(h.disconnected(), false);
});

for (const failure of ['storageError', 'settingsError']) {
  test(`${failure} disconnects the partial startup and reports its actual error`, async () => {
    const h = harness({ [failure]: 'Initialization failed' });
    await h.sandbox.start();
    assert.deepEqual(h.statuses.at(-1), { state: 'disconnected', error: 'Initialization failed' });
    assert.equal(h.disconnected(), true);
    assert.equal(h.reconnects(), 1);
    assert.ok(!h.order.includes('ping'));
    h.emitPong(); // A delayed message from the failed port cannot restore Connected.
    assert.notEqual(h.sandbox.currentStatus().state, 'connected');
  });
}
