import { test } from "node:test";
import assert from "node:assert/strict";
import { reconcilePending } from "../extension/ui/pending.js";

function fixture() {
  const pending = { conversationId: "old", body: {
    client_request_id: "request", text: "original", intent: "follow_up", task_id: "task",
  } };
  return { pending, selected: "new", drafts: { new: "hi" } };
}
function accepted(pending) {
  return { status: "accepted", message: {
    ...pending.body, conversation_id: pending.conversationId,
  }, task: { id: "task" } };
}
test("reconnect resolves acceptance in another conversation without changing its new draft", async () => {
  const state = fixture(), original = state.pending;
  const result = await reconcilePending(state, { request: async path => {
    assert.equal(path, "/api/requests/request");
    return accepted(original);
  } });
  assert.equal(state.pending, null);
  assert.equal(state.drafts.new, "hi");
  assert.equal(result.pending, original);
});
test("unknown or unreachable acceptance preserves the exact retry envelope", async () => {
  const state = fixture(), original = state.pending;
  assert.equal(await reconcilePending(state, { request: async () => ({ status: "unknown" }) }), null);
  await assert.rejects(reconcilePending(state, { request: async () => { throw Error("offline"); } }));
  assert.equal(state.pending, original);
  assert.equal(state.pending.body.task_id, "task");
});
test("retired request clears the blocker without replaying deleted work", async () => {
  const state = fixture();
  assert.equal((await reconcilePending(state, { request: async () => ({ status: "retired" }) })).status, "retired");
  assert.equal(state.pending, null);
});
test("late acknowledgement cannot clear a newer pending submission", async () => {
  const state = fixture(), original = state.pending;
  const newer = { ...original, body: { ...original.body, client_request_id: "new" } };
  await reconcilePending(state, { request: async () => {
    state.pending = newer;
    return accepted(original);
  } });
  assert.equal(state.pending, newer);
});
test("mismatched acknowledgement retains duplicate protection", async () => {
  const state = fixture(), original = state.pending;
  await assert.rejects(reconcilePending(state, { request: async () => ({
    ...accepted(original), message: { ...accepted(original).message, text: "different" },
  }) }));
  assert.equal(state.pending, original);
});
