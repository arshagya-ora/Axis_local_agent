export const TERMINAL = new Set([
  "completed",
  "failed",
  "cancelled",
  "limit_reached",
  "interrupted",
]);
export const STATUS = {
  active: "Running",
  pausing: "Pausing",
  stopping: "Stopping",
  paused: "Paused",
  needs_user: "Needs input",
  completed: "Completed",
  failed: "Failed",
  cancelled: "Cancelled",
  limit_reached: "Limit reached",
  interrupted: "Interrupted",
};
export function ownsRuntime(task) {
  return (
    !!task &&
    (task.owns_runtime === true ||
      (!TERMINAL.has(task.status) && task.owns_runtime !== false))
  );
}
export function mergeTask(state, task) {
  state.taskVersions ||= {};
  if ((state.taskVersions[task.id] || 0) > (task.updated_at || 0)) return false;
  state.taskVersions[task.id] = task.updated_at || 0;
  state.taskVersions = Object.fromEntries(
    Object.entries(state.taskVersions).slice(-100),
  );
  if (ownsRuntime(task)) state.active = task;
  else if (state.active?.id === task.id) state.active = null;
  if (
    task.conversation_id === state.selected &&
    (!state.readingEarlier || task.id in state.tasks)
  )
    state.tasks[task.id] = task;
  return true;
}
export function createState() {
  return {
    conversation: null,
    messages: [],
    tasks: {},
    activity: {},
    cursor: 0,
    hasMore: false,
    active: null,
    expanded: {},
    drafts: {},
    views: {},
    selected: null,
    mode: "new_task",
    target: null,
    context: null,
    pending: null,
    connected: false,
    stream: "offline",
  };
}
export function applySnapshot(state, snapshot) {
  state.conversation = snapshot.conversation;
  state.selected = snapshot.conversation.id;
  state.messages = snapshot.messages;
  state.tasks = Object.fromEntries(
    snapshot.tasks.map((task) => [task.id, task]),
  );
  state.activity = snapshot.activity;
  state.hasMore = snapshot.has_more;
  for (const task of snapshot.tasks) mergeTask(state, task);
  for (const task of snapshot.tasks)
    if (!(task.id in state.expanded))
      state.expanded[task.id] = !TERMINAL.has(task.status);
}
export function applyEvent(state, event) {
  if (!Number.isSafeInteger(event.sequence) || event.sequence <= state.cursor)
    return false;
  state.cursor = event.sequence;
  const task = event.payload?.task;
  if (task) mergeTask(state, task);
  if (event.conversation_id !== state.selected || state.readingEarlier)
    return false;
  if (task) {
    if (!(task.id in state.expanded))
      state.expanded[task.id] = !TERMINAL.has(task.status);
  }
  const message = event.payload?.message;
  if (message) {
    const index = state.messages.findIndex((item) => item.id === message.id);
    if (index >= 0) state.messages[index] = message;
    else state.messages.push(message);
    if (state.messages.length > 50) {
      state.messages = state.messages.slice(-50);
      state.hasMore = true;
    }
  }
  if (event.type === "conversation" && state.conversation)
    state.conversation.title = event.payload.title;
  if (task && event.payload.text) {
    const page = state.activity[task.id] || { items: [], has_more: false };
    if (!page.older) {
      page.items = [
        ...page.items.filter((item) => item.sequence !== event.sequence),
        event,
      ];
      if (page.items.length > 30) {
        page.items = page.items.slice(-30);
        page.has_more = true;
      }
      state.activity[task.id] = page;
    }
  }
  const visible = new Set(state.messages.map((item) => item.task_id));
  for (const id of Object.keys(state.tasks))
    if (!visible.has(id) && id !== state.active?.id) {
      delete state.tasks[id];
      delete state.activity[id];
    }
  return true;
}
export function rememberView(state, scroll) {
  const key = state.selected || "new";
  state.views[key] = {
    scroll,
    mode: state.mode,
    target: state.target,
    context: state.context,
  };
  state.views = Object.fromEntries(Object.entries(state.views).slice(-20));
}
export function persistentState(state) {
  return {
    selected: state.selected,
    drafts: Object.fromEntries(Object.entries(state.drafts).slice(-20)),
    views: state.views,
    expanded: Object.fromEntries(Object.entries(state.expanded).slice(-100)),
    pending: state.pending,
    extensions: state.extensions || {},
  };
}
