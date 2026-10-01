# AXIS

AXIS is a local, two-agent browser assistant. A planner interprets a task and a
navigator operates an already-open Chrome browser through the bundled Browser
Agent Bridge extension. It works with the user's existing Chrome profile and
sessions; it does not start a disposable test browser.

## What is included

| Path | Purpose |
| --- | --- |
| [axis-agent/](axis-agent/) | Python planner, navigator, runtime, CLI, UI service, and tests |
| [browser-agent-bridge-main/](browser-agent-bridge-main/) | Chrome extension and native host that connect AXIS to Chrome |
| [axis-agent/axis.yaml](axis-agent/axis.yaml) | Runtime limits, safety controls, and feature configuration |
| [docs/AXIS_UI.md](docs/AXIS_UI.md) | Browser workspace and pairing reference |
| [axis-agent/evals/](axis-agent/evals/) | Local evaluation harness |

## Requirements

- Google Chrome 116 or later
- Python 3.12 or later
- [uv](https://docs.astral.sh/uv/)
- A supported model-provider configuration

## Quick start

Clone the repository and install the locked Python environment:

```bash
git clone https://github.com/arshagya-ora/Axis_local_agent.git
cd Axis_local_agent
uv sync
```

Create local provider configuration. The file is ignored by Git; use only
values issued for your own environment.

```powershell
Copy-Item axis-agent/.env.example axis-agent/.env
```

Set `AXIS_API_KEY`, `AXIS_MODEL`, and `AXIS_OCI_GENAI_PROJECT_OCID` in that
file, or leave `AXIS_API_KEY` empty and configure OCI request signing in your
user profile. Never place real credentials, project identifiers, tokens, or
private keys in this repository.

## Connect Chrome

1. Open `chrome://extensions`, enable **Developer mode**, and choose **Load
   unpacked**. Select `browser-agent-bridge-main/extension`.
2. Copy the extension ID shown by Chrome.
3. Install the local native host:

   ```powershell
   cd browser-agent-bridge-main
   powershell -ExecutionPolicy Bypass -File .\scripts\install-native-host-win.ps1 <extension-id>
   ```

   On macOS or Linux, run `./scripts/install-native-host-unix.sh <extension-id>`.
4. Reload the extension, open its side panel, and select **Settings →
   Connection → Start bridge**. The bridge must report **Connected**.

The installer creates a machine-local bridge token. AXIS reads it automatically;
do not copy it into `.env` or source control.

## Run AXIS

For the browser workspace, start the local UI service and pair it from the
extension Settings screen. The service prints the one-time pairing credential.

```powershell
cd axis-agent
uv run python -m axis.ui_service --debug
```

For a terminal task, keep the bridge connected and run:

```powershell
cd axis-agent
uv run python -m axis.cli "Open Hacker News and summarize the top five stories."
```

Use `--approve` to request confirmation for consequential actions, `--capture`
to enable evidence capture, and `--debug` for an interactive full trace. Run
only the UI service or the CLI at one time; both control the same browser.

## Verify the checkout

```bash
uv run python -m pytest axis-agent/tests -q
cd browser-agent-bridge-main
node --test tests/*.mjs
python -m unittest discover -s tests -p "test_*.py"
```

The Python suite uses local fixtures and does not require Chrome or provider
credentials. See the component-level READMEs for implementation details:
[agent runtime](axis-agent/README.md), [browser bridge](browser-agent-bridge-main/README.md),
[attachments](axis-agent/axis/attachments/README.md), and
[evaluations](axis-agent/evals/README.md).

## Safety and privacy

AXIS can act in the user's signed-in browser, so review the task before running
it. Keep secrets in ignored local files, use `--approve` when changes need a
human checkpoint, and do not commit run output, downloaded files, screenshots,
traces, or personal evaluation notes. The repository ignores those local
artifacts by default.

## Contributing

Keep changes reproducible: update the relevant test, run the focused suite,
and avoid committing generated output or environment-specific configuration.
