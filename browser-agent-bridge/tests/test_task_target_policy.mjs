import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { test } from 'node:test';

const source = await readFile(new URL('../extension/sw/locator.js', import.meta.url), 'utf8');
const { assertTaskTargetAllowed, createLocatorHandlers } = await import(
  'data:text/javascript;base64,' + Buffer.from(source).toString('base64'));

test('resolved download attribute and archive URL are denied regardless of visible label', () => {
  for (const element of [
    {text: 'View', download: true},
    {text: 'View', href: 'https://github.com/org/repo/archive/refs/tags/v1.zip'},
    {text: 'Source code (tar.gz)'},
  ]) {
    assert.throws(() => assertTaskTargetAllowed({forbiddenActions: ['download']}, element),
      error => error.code === 'POLICY_DENIED');
  }
  assert.doesNotThrow(() => assertTaskTargetAllowed({forbiddenActions: ['download']},
    {text: 'Documentation', href: 'https://docs.example.test/'}));
  assert.doesNotThrow(() => assertTaskTargetAllowed({}, {text: 'Download'}));
});

test('ref action checks the resolved DOM target before dispatching input', async () => {
  const dispatched = [];
  const handlers = createLocatorHandlers({
    assertTabId: x => x, assertTabAllowed: async () => {}, assertString: () => {},
    recordAction: async () => {}, attachDebugger: async () => {},
    cdp: async (...args) => dispatched.push(args),
    resolveFrameTarget: async () => ({frameId: 0, target: {tabId: 1}}),
    keyboardDispatcher: {}, sleep: async () => {}, defaultTimeoutMs: 10,
    chromeApi: {tabs: {sendMessage: async () => ({ok: true, target: {
      element: {text: 'View', download: true, clickPoint: {x: 10, y: 10}},
      actionability: {actionable: true},
    }})}},
  });
  await assert.rejects(handlers.locatorClickRef({
    tabId: 1, ref: 'ref_1', forbiddenActions: ['download'],
  }), error => error.code === 'POLICY_DENIED');
  assert.deepEqual(dispatched, []);
});
