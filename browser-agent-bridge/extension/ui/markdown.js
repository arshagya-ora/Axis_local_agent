// Render a bounded Markdown subset using DOM nodes only. Raw HTML stays text.
import { el } from "./dom.js";

export function safeLink(value) {
  try {
    const url = new URL(value);
    return ["https:", "http:"].includes(url.protocol) ? url.href : null;
  } catch { return null; }
}

function inline(root, text, depth = 0) {
  if (depth > 5) { root.append(document.createTextNode(text)); return; }
  const pattern = /(`[^`\n]+`|\*\*[^*\n]+\*\*|__[^_\n]+__|\*[^*\n]+\*|\[[^\]\n]+\]\([^\s)]+\))/g;
  let start = 0;
  for (const match of text.matchAll(pattern)) {
    root.append(document.createTextNode(text.slice(start, match.index)));
    const token = match[0];
    if (token.startsWith("`")) root.append(el("code", "", token.slice(1, -1)));
    else if (token.startsWith("[")) {
      const parts = /^\[([^\]]+)\]\((.+)\)$/.exec(token);
      const href = safeLink(parts[2]);
      if (href) {
        const link = el("a");
        link.href = href;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        inline(link, parts[1], depth + 1);
        root.append(link);
      } else root.append(document.createTextNode(token));
    } else {
      const strong = token.startsWith("**") || token.startsWith("__");
      const node = el(strong ? "strong" : "em");
      inline(node, token.slice(strong ? 2 : 1, strong ? -2 : -1), depth + 1);
      root.append(node);
    }
    start = match.index + token.length;
  }
  root.append(document.createTextNode(text.slice(start)));
}

export function renderMarkdown(text) {
  const root = el("div", "answer-content");
  const lines = String(text || "").replace(/\r\n?/g, "\n").split("\n");
  let paragraph = [], list = null;
  const flush = () => {
    if (paragraph.length) {
      const p = el("p"); inline(p, paragraph.join("\n")); root.append(p); paragraph = [];
    }
  };
  for (let index = 0; index < lines.length; index++) {
    const line = lines[index];
    if (/^\s*```/.test(line)) {
      flush(); list = null;
      const code = [];
      while (++index < lines.length && !/^\s*```/.test(lines[index])) code.push(lines[index]);
      const pre = el("pre"); pre.append(el("code", "", code.join("\n"))); root.append(pre);
    } else if (!line.trim()) { flush(); list = null; }
    else if (/^#{1,6}\s/.test(line)) {
      flush(); list = null;
      const heading = el("h" + Math.min(6, line.match(/^#+/)[0].length + 1));
      inline(heading, line.replace(/^#+\s+/, "")); root.append(heading);
    } else if (/^\s*(?:[-+*]|\d+[.)])\s+/.test(line)) {
      flush();
      const ordered = /^\s*\d/.test(line);
      const tag = ordered ? "OL" : "UL";
      if (!list || list.tagName !== tag) {
        list = el(tag.toLowerCase());
        if (ordered) list.start = Number(line.match(/\d+/)[0]);
        root.append(list);
      }
      const item = el("li"); inline(item, line.replace(/^\s*(?:[-+*]|\d+[.)])\s+/, "")); list.append(item);
    } else if (/^>\s?/.test(line)) {
      flush(); list = null;
      const quote = el("blockquote"); inline(quote, line.replace(/^>\s?/, "")); root.append(quote);
    } else { list = null; paragraph.push(line); }
  }
  flush();
  return root;
}
