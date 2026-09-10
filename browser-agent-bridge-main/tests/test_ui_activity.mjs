import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";
import { activityPhase, groupActivity } from "../extension/ui/activity.js";

test("activity phases support explicit metadata, legacy history and unknown events", () => {
  const items = [
    { type: "status", payload: { text: "Planning the next step" } },
    { type: "browser_action", payload: { text: "Clicked" } },
    { type: "browser_action", payload: { text: "Captured", phase: "Evidence" } },
    { type: "status", payload: { text: "Read browser context" } },
    { type: "final", payload: { text: "Done" } },
    { type: "status", payload: { text: "Paused", phase: "untrusted" } },
    { type: "message", payload: {} },
  ];
  assert.deepEqual(groupActivity(items).map(g => [g.phase, g.items.length]), [
    ["Planning", 1], ["Browsing", 1], ["Evidence", 2], ["Result", 1], ["Activity", 1],
  ]);
  assert.equal(activityPhase(items[5]), "Activity");
  assert.equal(groupActivity(items)[2].items[0], items[2]);
  assert.deepEqual(groupActivity([]), []);
});

test("packaged UI files are valid UTF-8 without mojibake", () => {
  const root = new URL("../extension/", import.meta.url);
  const decoder = new TextDecoder("utf-8", { fatal: true });
  for (const name of readdirSync(root, { recursive: true })) {
    if (!/\.(html|js|css)$/.test(name)) continue;
    const source = decoder.decode(readFileSync(new URL(name.replaceAll("\\", "/"), root)));
    assert.doesNotMatch(source, /\u00e2\u20ac|\u00e2\u2020|\u00c2\u00b7|\ufffd/, name);
  }
});
