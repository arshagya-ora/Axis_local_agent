# Attachments in AXIS

This directory owns file ingestion, document reading, hybrid search, and saved
workflow progress. The extension controls live separately in
`browser-agent-bridge-main/extension/ui/attachments/`. Integration with the existing
planner, browser executor, and UI service is deliberately small; there is no
additional agent, database server, or orchestration framework.

## Use it

1. Run `uv sync` from the repository root and reload the Chrome extension.
2. Start the paired service as usual: from `axis-agent`, run
   `uv run python -m axis.ui_service --debug`.
3. Choose **Attach files**, or drop files onto the composer. You can submit while
   extraction is processing; required reads wait automatically. The original is
   available for website upload even when document reading fails.
4. Describe the goal and select Approval (default) or Automatic mode. New files
   use `auto` and can be read and uploaded together; no role choice is required.
   File instructions are followed only when the user delegates that work.
   Say which files, sheets, or cells to use and what to do with them. Explicit
   restrictions and later changes come from your messages.

The API accepts old role fields for compatibility; they no longer grant or deny
operations. Reading, summarizing, uploading, and executing instructions are
separate uses. For example, "Read rows 2 through 4, columns A through E. Do not
summarize the entire workbook" permits the exact read without a permission prompt.
Attachment contents cannot authorize actions or override your instructions.

Example: attach `MAPD.xlsx`, `workflow.md`, and a supporting PDF.
Ask AXIS to follow the entire workflow in order, use
the workbook's production values, and upload the PDF at the documentation step.
Clarify which file defines ordering if multiple instruction documents overlap.

There are at most 8 attachments per task, 32 per conversation, and 25 MiB per
file. Newly attached files can be added to a current task through a follow-up.
Pause/resume and explicit budget extensions continue to use the existing task
controls. A long file may require a larger execution budget.

## Formats and limits

| Format | Reading support |
| --- | --- |
| `.xlsx` | Sheet/row/cell locations, merge coordinates, raw values, formulas and cached results, number formats; direct range reads |
| `.docx` | Body paragraphs, headings, and tables in document order |
| `.pptx` | Slide text, tables, grouped text shapes, and speaker notes |
| `.pdf` | Page-indexed text extraction |
| `.md`, `.txt` | UTF-8 text with exact line locations and lossless long-line slices |
| `.csv` | CSV rows with their headers and values |
| `.doc`, `.ppt`, `.xls` | Original-file website upload; save in a modern Office format to read contents |

Text extraction does not read images, interpret diagrams, or run OCR. A PDF with
unreadable pages is reported explicitly, and full instruction completion is
blocked when pages require OCR. Provide a searchable PDF/text version. Word and
PowerPoint visuals, embedded objects, and unsupported document parts require
separate review. Password-protected files must be unlocked before reading.

Formulas and macros are never executed. Excel formula results are cached values,
not recalculated values. Raw values and number formats are preserved; display
formatting is not a full Excel renderer. The agent must resolve units, environment,
stale/missing formula results, and ambiguous headers before configuring a product.

Extraction limits include 100 MiB expanded Office content, 10,000 archive entries,
8 million text characters, 20,000 sections, and 2,000 PDF pages. Large files fail
with a readable limitation instead of silently dropping their remaining content.
The original remains available for upload.

## Retrieval

Broad orientation requests such as "what is written in this file?" use one
bounded overview of structure and source excerpts spread across the file. This
avoids a model round trip for every 16 paragraphs in a long document. The result
is explicitly sampled and does not count as exhaustive read coverage. Exact
ranges, all-item enumeration, full summaries, and workflow execution retain their
coverage requirements. Overview packets survive task recovery.

`parsing.py` creates stable source sections and structured spreadsheet data.
`store.py` stores them in the existing UI SQLite database. FTS5 provides BM25
keyword ranking. `search.py` uses FastEmbed/ONNX and the small English
`BAAI/bge-small-en-v1.5` model for normalized, 384-dimensional vectors. NumPy scans
the eligible task vectors directly. The two independent top-20 rankings are
merged with reciprocal rank fusion; up to eight candidates are returned.

Both searches are restricted to attachments linked to the current task. An
explicit attachment ID narrows the scope. Vector search is not restricted to
keyword hits. Ranking identifies candidates; the planner requests an exact source
read before using values. Search does not mark instruction sections as covered.
Full workflows use ordered, paginated reads rather than top-k retrieval alone.

`enumerate` traverses every section in the requested scope; `search_complete`
becomes true only after that scope is covered. `read` with `advance=true` selects
the next unread batch. Repeated traversal requests advance automatically. Read
coverage and validated, source-linked quotes are stored per task; `notes` retrieves
older findings in bounded pages. Reading some cells does not count as reading the
whole workbook. Empty ranges can finish with an empty result.

The model is loaded lazily and cached under `.axis-ui/attachments/model-cache`.
Parsing and embedding use separate bounded background workers, so a model download
does not delay reading the next file. Content-hash/model-keyed embeddings are
reused. No document data is sent to an embedding API. Relevant extracted content
is sent to the already-configured reasoning model when AXIS uses it.

To provision and test the embedding model ahead of the first upload:

```powershell
# From axis-agent; model download requires internet access once.
uv run python -m axis.attachments.prepare
```

For a custom UI service data directory, pass the same `--data-dir` to this command.
Unavailable embeddings leave keyword search usable and visible in the UI. Partial
indexing is reported as `hybrid_partial` in search results. If indexing failed,
restart the service and reattach the file after resolving model access.

## Execution and recovery

The planner remains tool-free: its typed `document_request` selects outline,
read, enumerate, search, notes, plan, skip, or workflow operations. The orchestrator executes them
locally and provides bounded results on the next planner pass. No browser session
is required for document-only questions. File instructions are task-scoped source
material, not authority to expand the user's request.

Workflow steps point to read source sections. The ledger records pending,
running, verified, and skipped steps, with explanations for non-applicable
sections. A step must use its predeclared postcondition and a matching runtime
`browser_assert` to become verified. Document reads and navigator prose cannot
complete browser actions. File selection alone is not a website upload receipt.

After a process restart or recoverable provider failure, **Resume saved progress**
is available for document tasks. It starts a fresh browser/model session and
retains cumulative budgets, read coverage, findings, and verified workflow steps.
It reconciles any running step against the website.
Its saved verification cannot be weakened on recovery. This is recovery from
business postconditions, not replay of browser commands; it is not a guarantee of
exactly-once execution on websites with ambiguous results. Such cases need user
input. Ordinary tasks without a workflow ledger retain their existing restart
behavior. Transient provider errors receive at most two retries with cancellable
backoff, charged to the request budget. A model run that has already executed a
browser tool is never replayed automatically. Internal document failures release
the active task instead of asking for unrelated user permission.

Document reads can extend the step budget in increments of five, only with new
coverage, to a maximum of 30 additional steps (`document_extra_steps`). Model
request and browser-action limits remain bounded. The planner reserves capacity
for a final or explicitly partial answer. These settings are in `axis.yaml`.

Wall-clock scheduling is not implemented; uploads at later workflow
steps are supported while the service and browser are available.

Original files stay under generated directories in `.axis-ui/attachments`.
The model uses IDs; only the browser executor resolves approved task files to
local paths. Existing action policy and bridge approvals still apply. Unused
draft attachments can be removed. Files linked to task history are removed by
deleting the conversation. Deletion cascades through document indexes and
workflow records, removes original bytes, and clears the reusable embedding cache.

## Verification

From the repository root:

```powershell
uv run python -m pytest axis-agent/tests/test_attachments.py -q
node --test browser-agent-bridge-main/tests/test_ui_attachments.mjs
```

The tests cover all parser formats, exact values and original bytes, independent
semantic matches, offline fallback, scoped access, pagination, idempotent message
submission, ordered/verified workflow steps, and restart recovery.

An optional isolated MV3 UI check exercises all seven formats, prompt-based use, panel
reload, history, narrow layout, and visible Send controls without invoking a paid
reasoning model or a real product site:

```powershell
# From axis-agent; use an installed Playwright Chromium executable if necessary.
uv run python -m axis.attachments.check_ui --executable "C:\path\to\chrome.exe"
```
