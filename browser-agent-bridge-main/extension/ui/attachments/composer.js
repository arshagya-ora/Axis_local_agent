import { el, button } from "../dom.js";

export function attachmentRefs(items = []) {
  return items.map(({ id, role }) => ({ attachment_id: id, role: role || "auto" }));
}

export function sameAttachments(left = [], right = []) {
  const canonical = items => JSON.stringify(items.map(x => [x.attachment_id, x.role]).sort());
  return canonical(left) === canonical(right);
}

export function clearSentAttachments(state, conversationId, refs = []) {
  const ids = new Set(refs.map(x => x.attachment_id));
  state.attachmentDrafts[conversationId] = (state.attachmentDrafts[conversationId] || []).filter(x => !ids.has(x.id));
}

export function mountAttachments(root, api, state, { save, error, changed }) {
  let uploading = false, timer = null, disabled = false;
  const picker = el("input");
  picker.type = "file";
  picker.multiple = true;
  picker.accept = ".pdf,.docx,.pptx,.xlsx,.md,.txt,.csv,.doc,.ppt,.xls";
  picker.hidden = true;
  const add = button("Attach files", () => picker.click(), "attachment", "icon-button attachment-add");
  add.title = "Attach files";
  const list = el("div", "attachment-list");
  const hint = el("p", "metadata", "Up to 8 files per task · 25 MiB each. Selected excerpts are sent to your configured model.");
  (document.getElementById("attachment-action") || root).append(add);
  root.append(picker, list, hint);

  function render(locked = disabled) {
    disabled = locked;
    const drafts = state.attachmentDrafts[state.selected] || [];
    hint.hidden = drafts.length === 0;
    add.disabled = disabled || uploading || drafts.length >= 8;
    add.setAttribute("aria-label", uploading ? "Uploading…" : "Attach files");
    add.title = uploading ? "Uploading…" : "Attach files";
    list.replaceChildren();
    for (const file of drafts) {
      const tile = el("div", "attachment-tile");
      const title = el("span", "attachment-name", file.filename);
      title.title = file.filename;
      const remove = button(`Remove ${file.filename}`, async () => {
        const cid = state.selected;
        try {
          await api.request(`/api/attachments/${file.id}`, { method: "DELETE" });
        } catch (cause) {
          if (!["attachment_in_use", "not_found"].includes(cause.code)) { error(cause.message); return; }
        }
        state.attachmentDrafts[cid] = (state.attachmentDrafts[cid] || []).filter(x => x.id !== file.id);
        render(); save();
      });
      remove.textContent = "Remove";
      remove.disabled = disabled;
      tile.append(title, remove);
      if (file.role === "upload") tile.append(el("span", "metadata", "Existing restriction: upload only"));
      const status = file.status === "processing" ? "Reading file…" : file.status === "unreadable" ? "Available for website upload; contents not readable" :
        file.search_status === "hybrid" ? "Ready · hybrid search" : file.search_status === "indexing" ? "Ready · preparing semantic search" : "Ready · keyword search";
      tile.append(el("span", "metadata", status));
      if (file.warnings?.length) {
        const details = el("details", "attachment-warnings");
        details.append(el("summary", "", "Reading notes"), el("p", "", file.warnings.join("\n")));
        tile.append(details);
      }
      list.append(tile);
    }
    clearTimeout(timer);
    if (drafts.some(x => x.status === "processing" || x.search_status === "indexing")) timer = setTimeout(refresh, 1500);
  }

  async function refresh() {
    const cid = state.selected;
    const drafts = state.attachmentDrafts[cid] || [];
    try {
      const result = await api.request(`/api/conversations/${cid}/attachments`);
      for (const file of drafts) {
        const fresh = result.items.find(x => x.id === file.id);
        if (fresh) Object.assign(file, fresh);
      }
      save();
    } catch { /* Connection feedback is handled by the workspace. */ }
    render();
  }

  async function upload(files) {
    if (disabled || uploading) return;
    const cid = state.selected;
    const drafts = state.attachmentDrafts[cid] ||= [];
    if (files.length + drafts.length > 8) { error("Attach at most eight files per task."); return; }
    if (files.some(x => x.size > 25 * 1024 * 1024)) { error("Each attachment must be 25 MiB or smaller."); return; }
    uploading = true; render(); changed();
    try {
      await api.request("/api/conversations", { method: "POST", body: { id: cid } });
      for (const file of files) {
        const result = await api.request(`/api/conversations/${cid}/attachments?filename=${encodeURIComponent(file.name)}`,
          { method: "POST", body: file, binary: true });
        drafts.push({ ...result, role: "auto" });
        save(); render();
      }
      error("");
    } catch (cause) { error(cause.message); }
    finally { uploading = false; picker.value = ""; render(); changed(); }
  }
  picker.addEventListener("change", () => upload([...picker.files]));
  root.parentElement.addEventListener("dragover", event => {
    if (event.dataTransfer?.types.includes("Files")) event.preventDefault();
  });
  root.parentElement.addEventListener("drop", event => {
    if (event.dataTransfer?.files.length) { event.preventDefault(); upload([...event.dataTransfer.files]); }
  });
  return { render, get uploading() { return uploading; } };
}
