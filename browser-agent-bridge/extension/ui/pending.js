// A lost HTTP response does not mean the instruction was not accepted.
// Keep the original envelope until the server confirms its disposition.
import { sameAttachments } from "./attachments/composer.js";
export async function reconcilePending(state, api) {
  const pending = state.pending;
  if (!pending) return null;
  const result = await api.request(
    `/api/requests/${encodeURIComponent(pending.body.client_request_id)}`,
  );
  if (state.pending !== pending) return null;
  if (result.status === "accepted") {
    const message = result.message;
    if (
      message?.client_request_id !== pending.body.client_request_id ||
      message.conversation_id !== pending.conversationId ||
      message.text !== pending.body.text ||
      message.intent !== pending.body.intent ||
      !sameAttachments(message.attachments, pending.body.attachments)
    ) throw new Error("The saved instruction does not match its acknowledgement.");
  } else if (result.status !== "retired") return null;
  state.pending = null;
  return { ...result, pending };
}
