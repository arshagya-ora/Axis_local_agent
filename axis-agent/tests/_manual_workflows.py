import json
import tempfile
import os
from browser_bridge_client import BrowserBridgeClient
from browser_agent_tools import BrowserAgentTools

PASS = []
FAIL = []


def check(label, cond, detail=""):
    if cond:
        PASS.append(label)
        print(f"  [PASS] {label}")
    else:
        FAIL.append((label, detail))
        print(f"  [FAIL] {label}  {detail}")


def call(tools, label, tool_name, args):
    result = tools.dispatch(tool_name, args)
    print(f"\n>>> {label}: {tool_name}({json.dumps({k: v for k, v in args.items() if k != 'browserSessionId'})})")
    print("    ->", json.dumps(result, indent=2)[:800])
    return result


client = BrowserBridgeClient()
tools = BrowserAgentTools(client)

bound = tools.bind_active_managed_tab()
assert bound["ok"], bound
sid = bound["browserSessionId"]
print("bound:", bound["data"])

# ---------------------------------------------------------------------
# Workflow 1: Login form (fill -> click -> assert flash message)
# ---------------------------------------------------------------------
print("\n===== Workflow 1: Login =====")
nav = call(tools, "nav to /login", "browser_navigate", {"browserSessionId": sid, "operation": "open", "url": "https://the-internet.herokuapp.com/login"})
check("navigate to /login ok", nav["ok"])

obs = call(tools, "observe login page", "browser_observe", {"browserSessionId": sid})
check("observe login page ok", obs["ok"])
check("username field visible in snapshot", obs["ok"] and "username" in obs["data"]["snapshot"].lower())

fill_user = call(tools, "fill username via locator", "browser_act", {
    "browserSessionId": sid, "action": "fill", "value": "tomsmith",
    "locator": {"selector": "#username"},
})
check("fill username ok", fill_user["ok"])

fill_pass = call(tools, "fill password via locator", "browser_act", {
    "browserSessionId": sid, "action": "fill", "value": "SuperSecretPassword!",
    "locator": {"selector": "#password"},
})
check("fill password ok", fill_pass["ok"])

click_login = call(tools, "click login button", "browser_act", {
    "browserSessionId": sid, "action": "click",
    "locator": {"selector": "button[type=submit]"},
})
check("click login ok", click_login["ok"])
check("login navigated (whatChanged.urlChanged)", click_login["ok"] and (click_login["data"]["whatChanged"] or {}).get("urlChanged") is True)

assert_flash = call(tools, "assert flash message text", "browser_assert", {
    "browserSessionId": sid, "assertion": "text", "locator": {"selector": "#flash"},
    "expected": "You logged into a secure area", "contains": True,
})
check("assert flash message passed", assert_flash["ok"] and assert_flash["data"]["passed"])

assert_url = call(tools, "assert url wait for /secure", "browser_wait", {
    "browserSessionId": sid, "condition": "url", "expected": "/secure", "timeoutMs": 5000,
})
check("wait for /secure url ok", assert_url["ok"])

evidence = call(tools, "screenshot of secure area", "browser_capture_evidence", {
    "browserSessionId": sid, "capture": "element_screenshot", "locator": {"selector": "#content"},
})
check("element screenshot ok", evidence["ok"])

# ---------------------------------------------------------------------
# Workflow 2: Checkboxes (locator-only check/uncheck; ref-based must be rejected)
# ---------------------------------------------------------------------
print("\n===== Workflow 2: Checkboxes =====")
nav2 = call(tools, "nav to /checkboxes", "browser_navigate", {"browserSessionId": sid, "operation": "open", "url": "https://the-internet.herokuapp.com/checkboxes"})
check("navigate to /checkboxes ok", nav2["ok"])

obs2 = call(tools, "observe checkboxes page", "browser_observe", {"browserSessionId": sid})
check("observe checkboxes ok", obs2["ok"])
print("    snapshot:", obs2["data"]["snapshot"])

# ref-based check must be rejected (no deterministic ref-based check/uncheck)
ref_check = call(tools, "ref-based check (must be rejected)", "browser_act", {
    "browserSessionId": sid, "action": "check",
    "observationId": obs2["data"]["observationId"], "ref": "ref_1",
})
check("ref-based check correctly rejected", (not ref_check["ok"]) and ref_check["error"]["code"] == "INVALID_ARGUMENT")

check1 = call(tools, "check checkbox 1 (locator)", "browser_act", {
    "browserSessionId": sid, "action": "check",
    "locator": {"selector": "input[type=checkbox]:nth-of-type(1)"},
})
check("check checkbox1 ok", check1["ok"])

assert_checked = call(tools, "assert checkbox1 checked", "browser_assert", {
    "browserSessionId": sid, "assertion": "checked",
    "locator": {"selector": "input[type=checkbox]:nth-of-type(1)"},
})
check("assert checkbox1 checked passed", assert_checked["ok"] and assert_checked["data"]["passed"])

uncheck2 = call(tools, "uncheck checkbox 2 (locator)", "browser_act", {
    "browserSessionId": sid, "action": "uncheck",
    "locator": {"selector": "input[type=checkbox]:nth-of-type(2)"},
})
check("uncheck checkbox2 ok", uncheck2["ok"])

# ---------------------------------------------------------------------
# Workflow 3: Dropdown (select)
# ---------------------------------------------------------------------
print("\n===== Workflow 3: Dropdown =====")
nav3 = call(tools, "nav to /dropdown", "browser_navigate", {"browserSessionId": sid, "operation": "open", "url": "https://the-internet.herokuapp.com/dropdown"})
check("navigate to /dropdown ok", nav3["ok"])

select_res = call(tools, "select option 2", "browser_act", {
    "browserSessionId": sid, "action": "select", "options": ["2"],
    "locator": {"selector": "#dropdown"},
})
check("select option ok", select_res["ok"])

assert_value = call(tools, "assert dropdown value", "browser_assert", {
    "browserSessionId": sid, "assertion": "value", "locator": {"selector": "#dropdown"}, "expected": "2",
})
check("assert dropdown value passed", assert_value["ok"] and assert_value["data"]["passed"])

# ---------------------------------------------------------------------
# Workflow 4: Hovers (ref-based hover, then observe revealed content)
# ---------------------------------------------------------------------
print("\n===== Workflow 4: Hovers =====")
nav4 = call(tools, "nav to /hovers", "browser_navigate", {"browserSessionId": sid, "operation": "open", "url": "https://the-internet.herokuapp.com/hovers"})
check("navigate to /hovers ok", nav4["ok"])

obs4 = call(tools, "observe hovers page", "browser_observe", {"browserSessionId": sid})
check("observe hovers ok", obs4["ok"])
print("    snapshot:", obs4["data"]["snapshot"][:400])

# hover requires a CSS selector when no ref (bridge has no non-ref hover-by-role)
hover_res = call(tools, "hover over first figure", "browser_act", {
    "browserSessionId": sid, "action": "hover", "locator": {"selector": ".figure:nth-of-type(1)"},
})
check("hover ok", hover_res["ok"])

# ---------------------------------------------------------------------
# Workflow 5: File upload
# ---------------------------------------------------------------------
print("\n===== Workflow 5: Upload =====")
nav5 = call(tools, "nav to /upload", "browser_navigate", {"browserSessionId": sid, "operation": "open", "url": "https://the-internet.herokuapp.com/upload"})
check("navigate to /upload ok", nav5["ok"])

tmp_path = os.path.join(tempfile.gettempdir(), "axis_agent_upload_test.txt")
with open(tmp_path, "w") as f:
    f.write("hello from axis agent manual workflow test\n")

# ref-based upload must be rejected
obs5 = call(tools, "observe upload page", "browser_observe", {"browserSessionId": sid})
ref_upload = call(tools, "ref-based upload (must be rejected)", "browser_act", {
    "browserSessionId": sid, "action": "upload",
    "observationId": obs5["data"]["observationId"], "ref": "ref_1", "files": [tmp_path],
})
check("ref-based upload correctly rejected", (not ref_upload["ok"]) and ref_upload["error"]["code"] == "INVALID_ARGUMENT")

upload_res = call(tools, "upload file (locator)", "browser_act", {
    "browserSessionId": sid, "action": "upload", "files": [tmp_path],
    "locator": {"selector": "#file-upload"},
})
check("upload ok", upload_res["ok"])

click_submit = call(tools, "click upload submit", "browser_act", {
    "browserSessionId": sid, "action": "click", "locator": {"selector": "#file-submit"},
})
check("click upload submit ok", click_submit["ok"])

assert_uploaded = call(tools, "assert uploaded filename shown", "browser_assert", {
    "browserSessionId": sid, "assertion": "text", "locator": {"selector": "#uploaded-files"},
    "expected": "axis_agent_upload_test.txt",
})
check("assert uploaded filename passed", assert_uploaded["ok"] and assert_uploaded["data"]["passed"])

os.remove(tmp_path)

# ---------------------------------------------------------------------
# Workflow 6: Drag and drop
# ---------------------------------------------------------------------
print("\n===== Workflow 6: Drag and drop =====")
nav6 = call(tools, "nav to /drag_and_drop", "browser_navigate", {"browserSessionId": sid, "operation": "open", "url": "https://the-internet.herokuapp.com/drag_and_drop"})
check("navigate to /drag_and_drop ok", nav6["ok"])

drag_res = call(tools, "drag column A onto column B", "browser_act", {
    "browserSessionId": sid, "action": "drag",
    "locator": {"selector": "#column-a"}, "targetLocator": {"selector": "#column-b"},
})
check("drag ok", drag_res["ok"])

# ---------------------------------------------------------------------
# Workflow 7: Tables (count assertion) + diagnose network
# ---------------------------------------------------------------------
print("\n===== Workflow 7: Tables + diagnose =====")
nav7 = call(tools, "nav to /tables", "browser_navigate", {"browserSessionId": sid, "operation": "open", "url": "https://the-internet.herokuapp.com/tables"})
check("navigate to /tables ok", nav7["ok"])

assert_count = call(tools, "assert row count", "browser_assert", {
    "browserSessionId": sid, "assertion": "count", "locator": {"selector": "#table1 tbody tr"}, "count": 4,
})
check("assert row count passed", assert_count["ok"] and assert_count["data"]["passed"])

diag = call(tools, "diagnose network", "browser_diagnose", {"browserSessionId": sid, "diagnostic": "network", "limit": 5})
check("diagnose network ok", diag["ok"])

diag_dom = call(tools, "diagnose dom", "browser_diagnose", {"browserSessionId": sid, "diagnostic": "dom"})
check("diagnose dom ok", diag_dom["ok"])

# ---------------------------------------------------------------------
print("\n\n========== SUMMARY ==========")
print(f"PASS: {len(PASS)}   FAIL: {len(FAIL)}")
if FAIL:
    for label, detail in FAIL:
        print(f"  FAILED: {label}  {detail}")
