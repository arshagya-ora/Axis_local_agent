import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { test } from 'node:test';
import vm from 'node:vm';

const source = await readFile(new URL('../extension/sw/page.js', import.meta.url), 'utf8');
const { createPageHandlers } = await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);

function element(tagName, children = [], style = {}) {
  const node = { nodeType: 1, tagName, childNodes: [], style };
  node.childNodes = children.map(child => {
    if (typeof child === 'string') return { nodeType: 3, textContent: child, parentElement: node };
    child.parentElement = node;
    return child;
  });
  return node;
}

async function readPage(body) {
  const handlers = createPageHandlers({
    assertTabId: id => id, assertTabAllowed: async () => {},
    resolveFrameTarget: async tabId => ({ target: { tabId }, frame: { frameId: 0 } }),
    chromeApi: { scripting: { executeScript: async ({ func }) => [{ result: vm.runInNewContext(`(${func})()`, {
      document: { body, title: 'Fixture' }, location: { href: 'https://fixture.test/' },
      Node: { ELEMENT_NODE: 1, TEXT_NODE: 3, DOCUMENT_NODE: 9, DOCUMENT_FRAGMENT_NODE: 11 },
      getComputedStyle: node => ({ display: 'block', visibility: 'visible', ...node.style }),
      getSelection: () => '',
    }) }] } },
  });
  return (await handlers.pageReadText({ tabId: 1 })).text;
}

test('page text excludes executable source and non-rendered confirmation strings', async () => {
  const body = element('BODY', [
    element('H1', ['Preferences']), element('BUTTON', ['Settings']),
    element('SCRIPT', ["show('Compact mode: enabled')"]),
    element('STYLE', ['.result { display: block; }']),
    element('TEMPLATE', ['Payment successful']),
    element('NOSCRIPT', ['JavaScript disabled']),
    element('DIV', ['Workflow complete'], { display: 'none' }),
    element('DIV', ['Secret status'], { visibility: 'hidden' }),
    element('DIV', ['Deferred content'], { contentVisibility: 'hidden' }),
  ]);
  assert.equal(await readPage(body), 'Preferences Settings');
});

test('page text retains visible shadow content and a visible child inside a hidden-visibility parent', async () => {
  const host = element('CUSTOM-ELEMENT');
  host.shadowRoot = { nodeType: 11, childNodes: [element('SPAN', ['Shadow status'])] };
  const body = element('BODY', [host,
    element('DIV', ['Hidden parent text', element('SPAN', ['Visible child'])], { visibility: 'hidden' }),
  ]);
  assert.equal(await readPage(body), 'Shadow status Visible child');
});
