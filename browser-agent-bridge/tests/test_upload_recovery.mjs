import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createLocatorHandlers } from '../extension/sw/locator.js';

function fixture({ missingBeforeSet = false, replacedAfterSet = false, uncertainSet = false } = {}) {
  let marks = 0, sets = 0, searches = 0;
  const handlers = createLocatorHandlers({
    assertTabId: value => value, assertTabAllowed: async () => {}, assertString: value => value,
    recordAction: async () => {}, attachDebugger: async () => {},
    resolveFrameTarget: async tabId => ({ target: { tabId }, frame: { frameId: 0 }, frameSelector: null }),
    chromeApi: { scripting: { executeScript: async options => {
      const action = options.args?.[0]?.action;
      if (action === 'markFileInput') { marks++; return [{ result: { multiple: true } }]; }
      if (action === 'summarizeMarkedFileInput' && replacedAfterSet) throw new Error('Marked file input not found after setting files');
      return [{ result: { element: { tagName: 'input' } } }];
    } } },
    cdp: async (_tab, method) => {
      if (method === 'DOM.performSearch') return { searchId: 'search', resultCount: missingBeforeSet && searches++ === 0 ? 0 : 1 };
      if (method === 'DOM.getSearchResults') return { nodeIds: [42] };
      if (method === 'DOM.setFileInputFiles') {
        sets++;
        if (uncertainSet) throw new Error('Connection lost after dispatch');
      }
      return {};
    },
  });
  return { run: () => handlers.locatorSetInputFiles({ tabId: 1, locator: { selector: 'input[type=file]' }, files: ['/fixture/report.txt'] }),
    counts: () => ({ marks, sets }) };
}

test('upload relocates a replaced input before setting files, with one bounded retry', async () => {
  const f = fixture({ missingBeforeSet: true });
  const result = await f.run();
  assert.equal(result.ok, true);
  assert.deepEqual(f.counts(), { marks: 2, sets: 1 });
});

test('input replacement after acceptance requires verification and never repeats upload', async () => {
  const f = fixture({ replacedAfterSet: true });
  const result = await f.run();
  assert.equal(result.inputReplaced, true);
  assert.equal(result.verificationRequired, true);
  assert.equal(f.counts().sets, 1);
});

test('uncertain setFileInputFiles outcome is not retried', async () => {
  const f = fixture({ uncertainSet: true });
  await assert.rejects(f.run(), /Connection lost/);
  assert.deepEqual(f.counts(), { marks: 1, sets: 1 });
});
