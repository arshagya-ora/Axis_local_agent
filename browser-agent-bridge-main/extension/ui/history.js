import { el, icon, button, feedback } from "./dom.js";
import { STATUS } from "./state.js";

export function mountHistory({
  dialog,
  list,
  search,
  api,
  state,
  onSelect,
  onNew,
  onDelete,
}) {
  let generation = 0,
    items = [],
    offset = 0,
    hasMore = false;
  const close = () => dialog.close();
  dialog.addEventListener(
    "keydown",
    (event) => {
      if (event.key === "Escape") {
        event.preventDefault();
        close();
      }
    },
    { capture: true },
  );
  document.querySelector("#close-history").addEventListener("click", close);
  document.querySelector("#history-new").addEventListener("click", () => {
    close();
    onNew();
  });
  dialog.addEventListener("click", (event) => {
    if (event.target === dialog) {
      const rect = dialog.getBoundingClientRect();
      if (event.clientX > rect.right || event.clientY > rect.bottom) close();
    }
  });
  dialog.addEventListener("close", () =>
    document.querySelector("#history-button").focus(),
  );
  let timer;
  search.addEventListener("input", () => {
    clearTimeout(timer);
    timer = setTimeout(() => load(), 180);
  });
  async function load(more = false) {
    const current = ++generation;
    if (!more) {
      offset = 0;
      list.replaceChildren(el("p", "list-state", "Loading conversations…"));
    }
    try {
      const result = await api.request(
        `/api/conversations?search=${encodeURIComponent(search.value)}&offset=${more ? offset : 0}`,
      );
      if (current !== generation) return;
      items = more ? [...items, ...result.items] : result.items;
      items = items.slice(-100);
      offset += result.items.length;
      hasMore = result.has_more;
      render();
    } catch (error) {
      if (current !== generation) return;
      const node = el("div", "list-state");
      node.append(
        el("p", "", error.message),
        button("Try again", () => load()),
      );
      list.replaceChildren(node);
    }
  }
  function render() {
    list.replaceChildren();
    if (!items.length) {
      list.append(
        el(
          "p",
          "list-state",
          search.value
            ? "No matching conversations."
            : "No conversations yet. Your browser tasks will be saved here.",
        ),
      );
      return;
    }
    let group = "";
    for (const conversation of items) {
      const date = new Date(conversation.updated_at * 1000),
        today = new Date(),
        yesterday = new Date();
      yesterday.setDate(today.getDate() - 1);
      const label =
        date.toDateString() === today.toDateString()
          ? "Today"
          : date.toDateString() === yesterday.toDateString()
            ? "Yesterday"
            : date.toLocaleDateString(undefined, {
                month: "short",
                day: "numeric",
                year: "numeric",
              });
      if (label !== group) {
        list.append(el("h2", "date-group", label));
        group = label;
      }
      const row = el(
          "article",
          `history-item${conversation.id === state.selected ? " selected" : ""}`,
        ),
        select = button(
          "",
          () => {
            close();
            onSelect(conversation.id);
          },
          null,
          "history-select",
        ),
        copy = el("span", "history-copy");
      select.setAttribute("aria-label", `Open ${conversation.title}`);
      if (conversation.id === state.selected)
        select.setAttribute("aria-current", "true");
      copy.append(
        el("strong", "", conversation.title),
        el("span", "preview", conversation.preview || "No messages yet"),
      );
      select.append(icon("file"), copy);
      row.append(select);
      if (state.active?.conversation_id === conversation.id)
        copy.append(
          el(
            "span",
            `badge ${state.active.status}`,
            STATUS[state.active.status],
          ),
        );
      const actions = el("div", "history-actions"),
        time = el(
          "time",
          "",
          date.toLocaleTimeString(undefined, {
            hour: "numeric",
            minute: "2-digit",
          }),
        );
      time.dateTime = date.toISOString();
      actions.append(
        time,
        button(
          `Rename ${conversation.title}`,
          () => rename(row, conversation),
          "edit",
          "icon-button",
        ),
        button(
          `Delete ${conversation.title}`,
          () => remove(row, conversation),
          "trash",
          "icon-button",
        ),
      );
      row.append(actions);
      list.append(row);
    }
    if (hasMore)
      list.append(
        button("Load more conversations", () => load(true), null, "full-width"),
      );
    if (offset > 100)
      list.append(
        button(
          "Back to recent conversations",
          () => load(),
          null,
          "full-width",
        ),
      );
  }
  function rename(row, conversation) {
    if (row.querySelector("form")) return;
    const form = el("form", "rename-form"),
      input = el("input");
    input.value = conversation.title;
    input.maxLength = 120;
    input.required = true;
    input.setAttribute("aria-label", "Conversation title");
    const save = el("button", "", "Save");
    save.type = "submit";
    form.append(
      input,
      save,
      button("Cancel", () => form.remove()),
    );
    row.append(form);
    input.focus();
    input.select();
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      save.disabled = true;
      try {
        await api.request(`/api/conversations/${conversation.id}`, {
          method: "PATCH",
          body: { title: input.value },
        });
        if (state.conversation?.id === conversation.id) {
          state.conversation.title = input.value;
          document.querySelector("#conversation-title").textContent =
            input.value;
        }
        await load();
      } catch (error) {
        form.append(el("p", "feedback error", error.message));
        save.disabled = false;
      }
    });
  }
  function remove(row, conversation) {
    if (row.querySelector(".delete-confirm")) return;
    const box = el("div", "delete-confirm"),
      message = el("p", "feedback");
    box.append(
      el("p", "metadata", "Delete this conversation and its saved history?"),
      message,
    );
    const confirm = button("Delete conversation", async () => {
      confirm.disabled = true;
      try {
        await api.request(`/api/conversations/${conversation.id}`, {
          method: "DELETE",
        });
        onDelete(conversation.id);
        await load();
      } catch (error) {
        feedback(message, error.message, true);
        confirm.disabled = false;
      }
    });
    box.append(
      confirm,
      button("Keep", () => box.remove()),
    );
    row.append(box);
    confirm.focus();
  }
  return {
    open() {
      dialog.showModal();
      search.focus();
      load();
    },
    refresh: load,
  };
}
