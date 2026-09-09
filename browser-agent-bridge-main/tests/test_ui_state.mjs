import { test } from "node:test";
import assert from "node:assert/strict";
import {
  createState,
  applyEvent,
  applySnapshot,
  persistentState,
  ownsRuntime,
  mergeTask,
} from "../extension/ui/state.js";

test("a late command response cannot resurrect a completed task", () => {
  const state = createState();
  state.selected = "c";
  const task = {
    id: "task",
    conversation_id: "c",
    status: "completed",
    owns_runtime: false,
    updated_at: 20,
  };
  mergeTask(state, task);
  assert.equal(
    mergeTask(state, {
      ...task,
      status: "active",
      owns_runtime: true,
      updated_at: 10,
    }),
    false,
  );
  assert.equal(state.active, null);
  assert.equal(state.tasks.task.status, "completed");
});

test("replayed events are deduplicated and activity remains bounded", () => {
  const state = createState();
  state.selected = "conversation";
  state.messages = [{ id: "message", task_id: "task" }];
  const task = {
    id: "task",
    conversation_id: "conversation",
    status: "active",
    owns_runtime: true,
  };
  for (let sequence = 1; sequence <= 2000; sequence++) {
    const event = {
      sequence,
      conversation_id: "conversation",
      task_id: "task",
      type: "status",
      payload: { task, text: `Action ${sequence}` },
    };
    assert.equal(applyEvent(state, event), true);
    assert.equal(applyEvent(state, event), false);
  }
  assert.equal(state.activity.task.items.length, 30);
  assert.equal(state.activity.task.has_more, true);
  assert.equal(state.cursor, 2000);
  assert.equal(state.expanded.task, true);
  state.expanded.task = false;
  applyEvent(state, {
    sequence: 2001,
    conversation_id: "conversation",
    task_id: "task",
    type: "final",
    payload: { task: { ...task, status: "completed", owns_runtime: false } },
  });
  assert.equal(state.expanded.task, false);
  assert.equal(state.active, null);
});

test("history selection and older activity do not lose runtime owner", () => {
  const state = createState();
  state.selected = "older";
  const task = {
    id: "current",
    conversation_id: "running",
    status: "limit_reached",
    owns_runtime: true,
  };
  applyEvent(state, {
    sequence: 1,
    conversation_id: "running",
    task_id: "current",
    payload: { task },
  });
  assert.equal(state.selected, "older");
  assert.equal(state.active.id, "current");
  assert.equal(ownsRuntime(task), true);
  applySnapshot(state, {
    conversation: { id: "older", title: "Saved" },
    messages: [],
    tasks: [],
    activity: {},
    has_more: false,
  });
  assert.equal(state.active.id, "current");
  state.readingEarlier = true;
  applyEvent(state, {
    sequence: 2,
    conversation_id: "older",
    payload: { message: { id: "new", task_id: "other" } },
  });
  assert.equal(state.messages.length, 0);
});

test("local view persistence excludes pairing credentials and caps retained views", () => {
  const state = createState();
  state.token = "secret";
  for (let index = 0; index < 150; index++) {
    state.drafts[index] = "draft";
    state.expanded[index] = true;
  }
  const stored = persistentState(state);
  assert.equal(Object.keys(stored.drafts).length, 20);
  assert.equal(Object.keys(stored.expanded).length, 100);
  assert.equal(JSON.stringify(stored).includes("secret"), false);
});
