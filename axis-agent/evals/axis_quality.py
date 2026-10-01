"""Pydantic Evals cases for model-dependent AXIS behavior.

The task callable supplied to ``Dataset.evaluate`` should run one AXIS task in
an isolated browser fixture and return a compact JSON/text transcript containing
the final status/answer, planner decisions, public tool calls, and evidence.
The same dataset can therefore be run against different model/provider
factories without coupling probabilistic evaluation to deterministic pytest.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from pydantic_evals import Case, Dataset
from pydantic_evals.evaluators import Evaluator, EvaluatorContext


class AxisQualityInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request: str
    case_name: str = ""
    initial_url: str = "https://fixture.test/start"
    page_fixture: str
    unexpected_change: str | None = None
    provider_tags: list[str] = Field(default_factory=list)


QUALITY_RUBRIC = (
        "Judge whether the AXIS run transcript satisfies the expected behavior. "
        "Require browser claims to be supported by recorded public actions or evidence; "
        "penalize invented diagnostics, lost literals, wrong tools, selector-first action "
        "when a fresh ref exists, unnecessary actions, and claims of success after failure."
)


class FixtureChecks(Evaluator):
    """The agent's own completion claim is never the evaluation oracle."""

    def evaluate(self, ctx: EvaluatorContext) -> dict[str, bool]:
        if ctx.output.get("prerequisite_unavailable"):
            return {}
        return {
            "task_success": bool(ctx.output.get("success")),
            "no_false_completion": not ctx.output.get("false_completion", False),
            "no_duplicate_actions": ctx.output.get("duplicate_actions", 0) == 0,
        }


def _case(name: str, request: str, fixture: str, expected: str, **metadata: Any) -> Case:
    return Case(
        name=name,
        inputs=AxisQualityInput(
            request=request,
            case_name=name,
            initial_url=f"https://fixture.test/eval/{name}",
            page_fixture=fixture,
            unexpected_change=metadata.pop("unexpected_change", None),
            provider_tags=metadata.pop("provider_tags", []),
        ),
        expected_output=expected,
        metadata=metadata,
    )


AXIS_QUALITY_DATASET = Dataset(
    name="axis_browser_quality",
    evaluators=[FixtureChecks()],
    cases=[
        _case(
            "literal_preservation",
            "Open https://fixture.test/eval/report?case=Exact-42 and summarize the heading.",
            "The target page heading is 'Exact report 42'.",
            "The exact literal URL, including case and query, is navigated to and remains visible in the plan/action transcript.",
            dimension="literal preservation",
        ),
        _case(
            "planner_decision_quality",
            "What page am I on, and what is its title?",
            "Current URL is /eval/planner_decision_quality and title is 'Fixture Home'.",
            "The planner browses only as needed, obtains current evidence, and completes without unrelated mutations.",
            dimension="planner decision quality",
        ),
        _case(
            "correct_tool_selection",
            "Select Canada in the Country dropdown and verify it is selected.",
            "A fresh select ref named Country is present; selecting reveals text 'Country: Canada'.",
            "The run uses browser_act select followed by deterministic observation or assertion, not navigation or coordinate control.",
            dimension="correct tool selection",
        ),
        _case(
            "ref_first_interaction",
            "Click the Continue button, verify the heading is 'Step 2', and report the resulting heading.",
            "A fresh button ref named Continue is present; clicking reveals heading 'Step 2'.",
            "The first interaction targets the fresh Continue ref. Locator fallback is acceptable only after a recorded stale/missing-ref failure.",
            dimension="ref-first interaction",
        ),
        _case(
            "grounded_final_answer",
            "Report the invoice total shown on this page.",
            "The page contains invoice total '$184.27' and no other monetary amount.",
            "The final answer reports exactly $184.27 and cites observed/extracted page evidence without adding unsupported details.",
            dimension="grounded final answers",
        ),
        _case(
            "no_invented_diagnostics",
            "Click Run diagnostics, check the browser console and network requests, then report any failures.",
            "Console evidence contains one TypeError; network evidence contains one HTTP 503 response.",
            "The answer mentions only diagnostics actually collected by browser_diagnose and does not infer unobserved console or network facts.",
            dimension="diagnostic grounding",
        ),
        _case(
            "unexpected_ui_recovery",
            "Open Settings and enable compact mode.",
            "The original Settings ref becomes stale after a rerender; a new observation exposes a fresh Settings ref and Compact mode checkbox.",
            "After the stale-reference result, the run re-observes, uses fresh refs, completes the action once, and verifies the resulting state.",
            unexpected_change="Invalidate the first observation immediately before its first action.",
            dimension="recovery after unexpected UI changes",
        ),
        _case(
            "cross_provider_consistency",
            "Find the visible account status and return only that status.",
            "The page contains 'Account status: Active'.",
            "Across repeated model/provider runs, the output remains 'Active', uses grounded read-only evidence, and does not mutate the page.",
            provider_tags=["repeat", "openai-compatible", "cross-model"],
            dimension="cross-model/provider regression",
        ),
        _case(
            "multi_tab_memory",
            "Read product Alpha on this page. Open https://fixture.test/eval/product-beta in a new tab, "
            "read product Beta, then report both prices and which is cheaper. Keep both tabs open.",
            "Alpha costs $31.50. Beta costs $24.25 on a separate page.",
            "The answer retains both prices and identifies Beta as cheaper, with evidence from both tabs.",
            dimension="memory across tabs",
        ),
        _case(
            "changing_spa_content",
            "Click Advance, wait for Step 2, then click Finish and verify 'Workflow complete'.",
            "Advance asynchronously replaces Step 1 with Step 2; Finish then shows Workflow complete.",
            "Both transitions complete exactly once without a false no-progress stop.",
            dimension="progress on dynamic pages",
        ),
        _case(
            "download_completion",
            "Click Download report and verify that axis-eval-report.csv has completed downloading.",
            "One local CSV download named axis-eval-report.csv is offered.",
            "The run verifies a completed download from this fixture, with exactly one request for the file.",
            dimension="download completion",
        ),
        _case(
            "canvas_interaction",
            "Use the visual canvas to click the blue Confirm tile and verify 'Canvas confirmed'.",
            "The only Confirm tile is drawn in a canvas and has no DOM control or accessible label.",
            "Visual recovery clicks the tile once and verifies the resulting text.",
            dimension="visual recovery",
        ),
    ],
)


def quality_dataset(names: list[str] | None = None, *, judge_model: Any = None) -> Dataset:
    """Select runnable fixtures and instantiate the optional judge only on request."""
    selected = set(names or [])
    known = {case.name for case in AXIS_QUALITY_DATASET.cases}
    if selected - known:
        raise ValueError(f"Unknown evaluation cases: {', '.join(sorted(selected - known))}")
    evaluators: list[Any] = [FixtureChecks()]
    if judge_model is not None:
        from pydantic_evals.evaluators import LLMJudge

        evaluators.append(LLMJudge(
            rubric=QUALITY_RUBRIC, model=judge_model,
            include_input=True, include_expected_output=True,
        ))
    return Dataset(
        name=AXIS_QUALITY_DATASET.name,
        cases=[case for case in AXIS_QUALITY_DATASET.cases if not selected or case.name in selected],
        evaluators=evaluators,
    )


__all__ = ["AXIS_QUALITY_DATASET", "AxisQualityInput", "quality_dataset"]
