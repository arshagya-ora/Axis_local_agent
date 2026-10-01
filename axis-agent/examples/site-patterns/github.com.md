# GitHub site pattern

Navigation, issue forms, pull requests and Actions logs. Verify repository and item identity before acting.

## Scope and status

- Host: github.com; English UI. Enterprise hosts need a separate file.
- Docs reviewed: 2026-09-14. Example; not tested in a signed-in session.
- Labels are candidates; verify their live roles/names.
- {owner}, {repo}, {number}, {run_id}: use task values or observed links.

## Screen map

Relative to https://github.com; verify path and identity.

| Screen | Path | Expected anchor |
| --- | --- | --- |
| Repository | /{owner}/{repo} | Owner/repo header; repository navigation |
| Issues | /{owner}/{repo}/issues | Issue list and filter field |
| Issue | /{owner}/{repo}/issues/{number} | Matching title, number and state |
| Pull request | /{owner}/{repo}/pull/{number} | Matching number; Conversation, Checks, Files changed |
| Actions run | /{owner}/{repo}/actions/runs/{run_id} | Run summary and jobs |

## Targeting and session

- Scope repository navigation below the owner/repo header; avoid global search.
- Use fresh refs or role/name targets scoped to a form, list or dialog. Re-observe after page changes; never save refs or coordinates here.
- Identity: repo + item number; runs: repo + run ID + attempt. Titles can repeat.
- Sign-in/SSO: use the authorized authentication path. A 404 may mean missing access.
- Menus/dialogs: check heading, choose the requested value, verify selection.

## Flow 1: Find an issue

1. Open the repository URL; verify owner/repo in the header.
2. Select Issues in repository navigation; verify the issue list.
3. Fill the list filter, e.g. `is:issue is:open label:"{label}"`; submit and verify filter/results.
4. Open the matching item link; verify repo, number and title.
5. Use visible pagination; verify items change and deduplicate by URL. Empty results apply only to the current filter.

## Flow 2: Create an issue (when requested)

1. Choose New issue; verify the composer or template chooser.
2. If templates appear, choose the requested type; verify its form heading.
3. Fill required fields by label; map supplied values and preserve Markdown. Clarify missing required facts.
4. Set requested assignees/labels through their menus; verify selections. Preview the body if available.
5. Submit once; verify the new issue URL/number and read back title, body and requested metadata.
6. Correct field errors. If acknowledgement is lost, check for the created issue before retrying; the outcome is unknown.

## Flow 3: Inspect a pull request

1. Open the known PR link; verify repo/number plus base and head branches.
2. Select Conversation for description/state, Files changed for diffs, Checks for results; verify each view after switching.
3. Expand diffs; account for unloaded files before claiming full coverage.
4. Record URL, revision and check results. Pending is not success; new commits require fresh checks.

## Flow 4: Inspect an Actions run

1. Select Actions, then the workflow in the left sidebar; verify its name.
2. Open the matching run; verify branch/commit, run ID and selected attempt.
3. Open the job and step; wait for logs or an explicit unavailable state.
4. Record status and conclusion. Queued/in-progress is pending; use bounded rechecks. Inspection does not authorize reruns or approvals.

## Recovery and maintenance

- Missing/ambiguous control: re-observe and refine scope; do not force stale selectors.
- Keep run evidence in task history; exclude secrets/private content from this file.
- Maintainer: record live-tested changes/date; keep this file under 4,000 characters.

## Sources

- [PR views](https://docs.github.com/en/pull-requests/get-started/about-pull-requests)
- [Issue forms](https://docs.github.com/en/issues/tracking-your-work-with-issues/using-issues/creating-an-issue)
- [Run logs](https://docs.github.com/en/actions/how-tos/monitor-workflows/use-workflow-run-logs)
