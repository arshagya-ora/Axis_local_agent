import { test } from "node:test";
import assert from "node:assert/strict";
import { attachmentRefs, sameAttachments, clearSentAttachments } from "../extension/ui/attachments/composer.js";
import { reconcilePending } from "../extension/ui/pending.js";
import { createState, persistentState } from "../extension/ui/state.js";

test("attachment drafts persist metadata but never original file bytes", () => {
  const state = createState();
  state.attachmentDrafts.c = [{ id: "file", filename: "mapd.xlsx", role: "instructions" }];
  assert.deepEqual(persistentState(state).attachmentDrafts.c, state.attachmentDrafts.c);
  assert.deepEqual(attachmentRefs(state.attachmentDrafts.c), [{ attachment_id: "file", role: "instructions" }]);
});

test("acknowledgement clears sent attachments and preserves a newer draft", () => {
  const state = createState();
  state.attachmentDrafts.c = [{ id: "old" }, { id: "new" }];
  clearSentAttachments(state, "c", [{ attachment_id: "old", role: "reference" }]);
  assert.deepEqual(state.attachmentDrafts.c, [{ id: "new" }]);
  assert.equal(sameAttachments([{ attachment_id: "a", role: "upload" }], [{ attachment_id: "a", role: "reference" }]), false);
});

test("lost-response recovery rejects acknowledgement for a different file", async () => {
  const state = { pending: { conversationId: "c", body: { text: "Configure", intent: "new_task", client_request_id: "r", attachments: [{ attachment_id: "a", role: "reference" }] } } };
  await assert.rejects(reconcilePending(state, { request: async () => ({ status: "accepted", message: { ...state.pending.body, conversation_id: "c", attachments: [{ attachment_id: "b", role: "reference" }] } }) }));
  assert.ok(state.pending);
});
