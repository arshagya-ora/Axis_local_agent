import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { test } from 'node:test';

async function importModule(name) {
  const source = await readFile(new URL(`../extension/sw/${name}.js`, import.meta.url), 'utf8');
  return import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);
}

function state(tabId = 7) {
  return {
    tabId, url: 'https://example.test/canvas',
    viewport: { width: 1200, height: 800, deviceScaleFactor: 2, scale: 1, offsetX: 0, offsetY: 0 },
    scroll: { x: 0, y: 120 }
  };
}

async function setup({ imageWidth = 2400, imageHeight = 1600 } = {}) {
  const { createVisualHandlers } = await importModule('visual');
  let live = state();
  let id = 0;
  const calls = [];
  const deps = {
    assertTabAllowed: async (tabId, method) => calls.push({ type: 'allowed', tabId, method }),
    attachDebugger: async tabId => calls.push({ type: 'attach', tabId }),
    cdp: async (tabId, method, params) => {
      calls.push({ type: 'cdp', tabId, method, params });
      return { data: 'cGl4ZWxz' };
    },
    chromeApi: { scripting: { executeScript: async ({ target }) => {
      calls.push({ type: 'probe', tabId: target.tabId });
      const { tabId, ...result } = live;
      return [{ result: structuredClone(result) }];
    } } },
    cryptoApi: { randomUUID: () => `shot-${++id}` },
    decodeImage: async () => ({ width: imageWidth, height: imageHeight, close: () => calls.push({ type: 'close' }) }),
    createCanvas: (width, height) => ({
      getContext: () => ({ drawImage: () => calls.push({ type: 'resize', width, height }) }),
      convertToBlob: async () => new Blob(['small pixels'])
    })
  };
  return { h: createVisualHandlers(deps), deps, calls, setState: value => { live = value; } };
}

test('captures exact tab, retains CSS/HiDPI metadata, resizes longest edge to 1600', async () => {
  const { h, calls } = await setup();
  const result = await h.captureVisualScreenshot(7);
  assert.deepEqual(result.image, { width: 1600, height: 1067 });
  assert.deepEqual(result.viewport, state().viewport);
  assert.deepEqual(result.scroll, { x: 0, y: 120 });
  assert.equal(result.screenshotId, 'shot-1');
  assert.match(result.dataUrl, /^data:image\/png;base64,/);
  const capture = calls.find(call => call.method === 'Page.captureScreenshot');
  assert.equal(capture.tabId, 7);
  assert.equal(capture.params.captureBeyondViewport, false);
  assert.equal(calls.filter(call => call.type === 'probe').every(call => call.tabId === 7), true);
  assert.equal(calls.some(call => call.type === 'close'), true);
});

test('small screenshot is not upscaled and JPEG format is preserved', async () => {
  const { h, calls } = await setup({ imageWidth: 800, imageHeight: 600 });
  const result = await h.captureVisualScreenshot(7, { format: 'jpeg', quality: 80 });
  assert.deepEqual(result.image, { width: 800, height: 600 });
  assert.equal(result.dataUrl, 'data:image/jpeg;base64,cGl4ZWxz');
  assert.equal(calls.some(call => call.type === 'resize'), false);
  assert.equal(calls.find(call => call.method === 'Page.captureScreenshot').params.quality, 80);
});

test('readVisualState reads only targeted metadata, with no image capture or mutation', async () => {
  const { h, calls } = await setup();
  assert.deepEqual(await h.readVisualState(7), state());
  assert.equal(calls.some(call => call.type === 'cdp'), false);
});

test('fresh screenshot is single use, including when a subsequent input fails', async () => {
  const { h } = await setup();
  const capture = await h.captureVisualScreenshot(7);
  const params = { screenshotId: capture.screenshotId, expectedVisualState: capture };
  await h.validateVisualAction(7, params);
  await assert.rejects(() => h.validateVisualAction(7, params), { code: 'STALE_VISUAL_STATE' });
});

test('newer capture, mismatched tab, forged metadata and missing state are rejected', async () => {
  const { h } = await setup();
  const old = await h.captureVisualScreenshot(7);
  const capture = await h.captureVisualScreenshot(7);
  for (const [tabId, params] of [
    [7, { screenshotId: old.screenshotId, expectedVisualState: old }],
    [8, { screenshotId: capture.screenshotId, expectedVisualState: capture }],
    [7, { screenshotId: capture.screenshotId, expectedVisualState: { ...capture, tabId: 8 } }],
    [7, { screenshotId: capture.screenshotId }]
  ]) await assert.rejects(() => h.validateVisualAction(tabId, params), { code: 'STALE_VISUAL_STATE' });
});

test('navigation, viewport, browser zoom and scroll invalidate screenshots', async () => {
  for (const mutate of [
    s => { s.url += '#changed'; },
    s => { s.viewport.width += 1; },
    s => { s.viewport.height += 1; },
    s => { s.viewport.deviceScaleFactor = 1.5; },
    s => { s.scroll.y += 1; },
    s => { s.scroll.x += 1; }
  ]) {
    const { h, setState } = await setup();
    const capture = await h.captureVisualScreenshot(7);
    const live = state();
    mutate(live);
    setState(live);
    await assert.rejects(() => h.validateVisualAction(7, { screenshotId: capture.screenshotId, expectedVisualState: capture }), { code: 'STALE_VISUAL_STATE' });
  }
});

test('pinch zoom rejects ambiguous coordinates before image capture', async () => {
  const { h, setState, calls } = await setup();
  const zoomed = state();
  zoomed.viewport.scale = 2;
  setState(zoomed);
  await assert.rejects(() => h.captureVisualScreenshot(7), /Reset pinch zoom/);
  assert.equal(calls.some(call => call.type === 'cdp'), false);
});

test('legacy computer calls skip screenshot guards; policy denial still stops capture', async () => {
  const { h, deps, calls } = await setup();
  await h.validateVisualAction(7, { x: 10, y: 20 });
  assert.equal(calls.length, 0);
  deps.assertTabAllowed = async () => { throw new Error('Access denied'); };
  const { createVisualHandlers } = await importModule('visual');
  await assert.rejects(() => createVisualHandlers(deps).captureVisualScreenshot(7), /Access denied/);
  assert.equal(calls.some(call => call.type === 'cdp'), false);
});

test('all computer interactions validate screenshot before input dispatch', async () => {
  const { createComputerHandlers } = await importModule('computer');
  for (const [name, params] of [
    ['computerClick', { x: 1, y: 2 }], ['computerDrag', { fromX: 0, fromY: 0, toX: 1, toY: 2 }],
    ['computerHover', { x: 1, y: 2 }], ['computerScroll', {}],
    ['computerType', { text: 'hello' }], ['computerKey', { key: 'Enter' }]
  ]) {
    let inputs = 0;
    let guards = 0;
    const h = createComputerHandlers({
      assertTabId: x => x, assertTabAllowed: async () => {}, assertString: x => x, assertNumber: x => x,
      attachDebugger: async () => {}, cdp: async () => { inputs++; }, indicatorSet: async () => {}, recordAction: async () => {},
      keyboardDispatcher: { typeText: async () => { inputs++; }, press: async () => { inputs++; } },
      validateVisualAction: async () => { guards++; throw new Error('stale'); }
    });
    await assert.rejects(() => h[name]({ tabId: 7, ...params }), /stale/);
    assert.equal(guards, 1, name);
    assert.equal(inputs, 0, name);
  }
});

test('page.screenshot keeps ordinary capture contract and opts into visual metadata explicitly', async () => {
  const { createPageHandlers } = await importModule('page');
  const h = createPageHandlers({
    assertTabId: id => id, assertUrlAllowed: async () => {}, assertTabAllowed: async () => {},
    captureTabScreenshot: async () => 'original',
    captureVisualScreenshot: async tabId => ({ dataUrl: 'small', screenshotId: 'shot', tabId }),
    readVisualState: async tabId => state(tabId),
    chromeApi: { tabs: { get: async () => ({ url: state().url }) } }
  });
  assert.deepEqual(await h.pageScreenshot({ tabId: 7 }), { dataUrl: 'original' });
  assert.deepEqual(await h.pageScreenshot({ tabId: 7, modelFacing: true }), { dataUrl: 'small', screenshotId: 'shot', tabId: 7 });
  assert.deepEqual(await h.pageVisualState({ tabId: 7 }), state());
});

test('page changes during capture discard the image before issuing a screenshot id', async () => {
  const { deps } = await setup();
  let probes = 0;
  deps.chromeApi.scripting.executeScript = async () => {
    const current = state();
    if (++probes === 2) current.scroll.y++;
    return [{ result: current }];
  };
  const { createVisualHandlers } = await importModule('visual');
  const h = createVisualHandlers(deps);
  await assert.rejects(() => h.captureVisualScreenshot(7), { code: 'STALE_VISUAL_STATE' });
  await assert.rejects(() => h.validateVisualAction(7, { screenshotId: 'shot-1', expectedVisualState: state() }), { code: 'STALE_VISUAL_STATE' });
});
