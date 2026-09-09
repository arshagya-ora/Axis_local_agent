const paths = {
  history: "M12 8v4l3 2 M21 12a9 9 0 1 1-3-6.7 M21 3v5h-5",
  edit: "M15 5l4 4 M14 4l3-2 5 5-3 3-9 9-5 1 1-5z M10 3H4v17h17v-7",
  settings:
    "M9 3h6l1 3 3 1 2 5-2 5-3 1-1 3H9l-1-3-3-1-2-5 2-5 3-1z M15 12a3 3 0 1 1-6 0 3 3 0 0 1 6 0",
  close: "M6 6l12 12 M18 6L6 18",
  search: "M21 21l-5-5 M18 10a8 8 0 1 1-16 0 8 8 0 0 1 16 0",
  send: "M12 20V4 M5 11l7-7 7 7",
  back: "M20 12H4 M10 6l-6 6 6 6",
  external: "M14 3h7v7 M21 3L11 13 M10 3H4v17h17v-7",
  browser: "M3 4h18v16H3z M3 8h18 M6 6h1",
  monitor: "M3 3h18v14H3z M8 21h8 M12 17v4",
  file: "M14 2H5v20h14V7z M14 2v6h5",
  check: "M5 12l4 4L19 6",
  up: "M6 15l6-6 6 6",
  down: "M6 9l6 6 6-6",
  stop: "M6 6h12v12H6z",
  pause: "M8 5v14 M16 5v14",
  play: "M8 4l12 8-12 8z",
  link: "M10 14l4-4 M8 16l-2 2a4 4 0 0 1-5-6l4-4a4 4 0 0 1 6 0 M16 8l2-2a4 4 0 0 1 5 6l-4 4a4 4 0 0 1-6 0",
  globe:
    "M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0 M3 12h18 M12 3c-5 5-5 13 0 18 5-5 5-13 0-18",
  sun: "M16 12a4 4 0 1 1-8 0 4 4 0 0 1 8 0 M12 1v3 M12 20v3 M1 12h3 M20 12h3 M4 4l2 2 M18 18l2 2 M4 20l2-2 M18 6l2-2",
  shield: "M12 2l9 4v6c0 5-9 10-9 10S3 17 3 12V6z",
  cube: "M12 2l9 5v10l-9 5-9-5V7z M3 7l9 5 9-5 M12 12v10 M7 4l10 6",
  limits: "M5 20v-7 M12 20V8 M19 20V3",
  lock: "M6 10h12v11H6z M8 10V6a4 4 0 0 1 8 0v4 M12 14v3",
  trash: "M3 6h18 M9 6V3h6v3 M6 6l1 15h10l1-15 M10 10v7 M14 10v7",
  info: "M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0 M12 11v6 M12 7h.01",
};
export function icon(name) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  for (const [key, value] of Object.entries({
    viewBox: "0 0 24 24",
    fill: "none",
    stroke: "currentColor",
    "stroke-width": "1.7",
    "stroke-linecap": "round",
    "stroke-linejoin": "round",
    "aria-hidden": "true",
  }))
    svg.setAttribute(key, value);
  const path = document.createElementNS(svg.namespaceURI, "path");
  path.setAttribute("d", paths[name] || paths.file);
  svg.append(path);
  return svg;
}
export function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
}
export function button(label, action, iconName, cls = "") {
  const node = el("button", cls, cls.includes("icon-button") ? "" : label);
  node.type = "button";
  if (iconName) {
    node.prepend(icon(iconName));
    node.setAttribute("aria-label", label);
  }
  node.addEventListener("click", action);
  return node;
}
export function decorate(root = document) {
  root
    .querySelectorAll("[data-icon]")
    .forEach((node) => node.replaceChildren(icon(node.dataset.icon)));
}
export function feedback(node, text, error = false) {
  node.textContent = text;
  node.hidden = !text;
  node.classList.toggle("error", error);
}
