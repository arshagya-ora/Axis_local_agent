// Model-facing screenshots are short-lived targeting evidence, not artifacts.
// Existing full-resolution screenshot and computer.* callers remain unchanged.
export function createVisualHandlers({
  assertTabAllowed,
  attachDebugger,
  cdp,
  chromeApi = chrome,
  cryptoApi = crypto,
  decodeImage = async dataUrl => createImageBitmap(await (await fetch(dataUrl)).blob()),
  createCanvas = (width, height) => new OffscreenCanvas(width, height)
}) {
  const captures = new Map();

  async function readVisualState(tabId) {
    await assertTabAllowed(tabId, 'page.visualState');
    const [{ result } = {}] = await chromeApi.scripting.executeScript({
      target: { tabId },
      func: () => ({
        url: location.href,
        viewport: {
          width: window.innerWidth,
          height: window.innerHeight,
          deviceScaleFactor: window.devicePixelRatio,
          scale: window.visualViewport?.scale || 1,
          offsetX: window.visualViewport?.offsetLeft || 0,
          offsetY: window.visualViewport?.offsetTop || 0
        },
        scroll: { x: window.scrollX, y: window.scrollY }
      })
    });
    if (!result || !Number.isFinite(result.viewport?.width) || result.viewport.width <= 0 ||
        !Number.isFinite(result.viewport?.height) || result.viewport.height <= 0) {
      throw staleVisualError('Unable to read the target viewport');
    }
    return { tabId, ...result };
  }

  async function captureVisualScreenshot(tabId, options = {}) {
    await assertTabAllowed(tabId, 'page.screenshot');
    await attachDebugger(tabId);
    captures.delete(tabId);
    const before = await readVisualState(tabId);
    // Pinch zoom changes the mapping between the visible surface and CDP input.
    // Refuse that ambiguous mapping; ordinary browser zoom and HiDPI work normally.
    if (before.viewport.scale !== 1 || before.viewport.offsetX !== 0 || before.viewport.offsetY !== 0) {
      throw staleVisualError('Reset pinch zoom before using visual targeting');
    }
    const format = options.format === 'jpeg' ? 'jpeg' : 'png';
    const captured = await cdp(tabId, 'Page.captureScreenshot', {
      format,
      fromSurface: true,
      captureBeyondViewport: false,
      ...(format === 'jpeg' && Number.isFinite(options.quality) ? { quality: Math.max(0, Math.min(100, Math.round(options.quality))) } : {})
    });
    if (!captured?.data) throw new Error('Screenshot capture returned no image');
    const originalUrl = `data:image/${format};base64,${captured.data}`;
    const bitmap = await decodeImage(originalUrl);
    let dataUrl = originalUrl;
    let width;
    let height;
    try {
      if (!(bitmap.width > 0 && bitmap.height > 0)) throw new Error('Screenshot has no image area');
      const ratio = Math.min(1, 1600 / Math.max(bitmap.width, bitmap.height));
      width = Math.max(1, Math.round(bitmap.width * ratio));
      height = Math.max(1, Math.round(bitmap.height * ratio));
      if (ratio < 1) {
        const canvas = createCanvas(width, height);
        canvas.getContext('2d').drawImage(bitmap, 0, 0, width, height);
        const blob = await canvas.convertToBlob({ type: `image/${format}` });
        dataUrl = `data:image/${format};base64,${await blobBase64(blob)}`;
      }
    } finally {
      bitmap.close?.();
    }
    const after = await readVisualState(tabId);
    if (!sameVisualState(before, after)) throw staleVisualError('Page changed while capturing; capture again');
    const metadata = { ...after, image: { width, height }, screenshotId: cryptoApi.randomUUID() };
    // Only metadata stays in memory. Closed tabs cannot accumulate image bytes.
    captures.set(tabId, metadata);
    while (captures.size > 64) captures.delete(captures.keys().next().value);
    return { dataUrl, ...metadata };
  }

  async function validateVisualAction(tabId, params) {
    if (params.screenshotId === undefined && params.expectedVisualState === undefined) return;
    const capture = captures.get(tabId);
    if (!capture || typeof params.screenshotId !== 'string' || params.screenshotId !== capture.screenshotId) {
      throw staleVisualError('Screenshot is stale, consumed, or belongs to another tab');
    }
    if (!sameVisualState(capture, params.expectedVisualState)) {
      throw staleVisualError('Expected screenshot metadata does not match the captured tab');
    }
    const live = await readVisualState(tabId);
    if (captures.get(tabId) !== capture || !sameVisualState(capture, live)) {
      captures.delete(tabId);
      throw staleVisualError('Tab URL, viewport, zoom, or scroll changed; capture again');
    }
    // Consume before dispatch: an unknown input outcome must never reuse pixels.
    captures.delete(tabId);
  }

  return { readVisualState, captureVisualScreenshot, validateVisualAction };
}

export function sameVisualState(a, b) {
  if (!a || !b || a.tabId !== b.tabId || a.url !== b.url) return false;
  return ['width', 'height', 'deviceScaleFactor', 'scale', 'offsetX', 'offsetY'].every(key =>
    Number.isFinite(a.viewport?.[key]) && a.viewport[key] === b.viewport?.[key]
  ) && ['x', 'y'].every(key => Number.isFinite(a.scroll?.[key]) && a.scroll[key] === b.scroll?.[key]);
}

function staleVisualError(message) {
  const error = new Error(message);
  error.code = 'STALE_VISUAL_STATE';
  return error;
}

async function blobBase64(blob) {
  const bytes = new Uint8Array(await blob.arrayBuffer());
  let binary = '';
  for (let i = 0; i < bytes.length; i += 0x8000) binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  return btoa(binary);
}
