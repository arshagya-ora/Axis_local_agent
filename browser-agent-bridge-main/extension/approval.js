import { mountApprovals } from "./ui/approvals.js";
import { applyAppearance } from "./ui/settings.js";
await applyAppearance();
mountApprovals(document.querySelector("#permission-card"), { popup: true });
