import { AxisApi } from "./api.js";
import { applyAppearance, mountSettings } from "./settings.js";
await applyAppearance();
const api = new AxisApi();
await api.load();
const settings = mountSettings(document.querySelector("#settings"), api, {
  options: true,
});
chrome.storage.onChanged.addListener((changes, area) => {
  if (area === "local" && changes.axisTheme) applyAppearance();
});
chrome.runtime.onMessage.addListener((message) => {
  if (message?.type === "NATIVE_STATUS_CHANGED")
    settings.renderBridge(message.status);
});
