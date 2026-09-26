"""PR / run scoring — the loop-engineering "get a score" checker stage.

Turns the objective signals a run produces into a single 0–100 score, a letter
grade, and a *gate* decision (auto / review / escalate). The score is a **pure
function** of its inputs so it is trivially testable and deterministic — the
control plane gathers the signals; this module only judges them.

Rubric (weights sum to 100):

    delivery  35  — did it finish and open a PR?
    review    30  — auto-review verdict (critical/warning counts)
    ci        20  — CI/build status on the PR branch
    scope     15  — did files actually change, threads resolved?

Unknown signals (no auto-review ran, CI status unavailable) score *neutral-low*
rather than full marks: a run we cannot verify must not be able to reach the
auto-merge threshold on completion alone. That is the honest default — no
evidence of quality ≠ high quality.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ScoreInput:
    """Objective signals gathered about one run. Optional signals default to
    ``None`` (unknown) and are scored neutrally rather than as failures."""

    completed: bool
    has_pr: bool
    files_changed: int
    needs_human: bool = False
    had_error: bool = False
    # Was this run supposed to produce a pull request at all? The rubric was written when
    # every run was a dev run, so "delivered" meant "opened a PR" and "did something"
    # meant "changed files". A QC or BA role satisfies neither by design — it scored
    # 48/100 for doing its job exactly right, which is below `review_min` and therefore
    # HELD FOR A HUMAN every single time. Not-applicable is not the same as unmeasured:
    # we are not missing the evidence, we know there is none to have.
    expected_pr: bool = True
    # Richer signals — None means "not measured this run".
    review_passed: bool | None = None
    review_critical: int = 0
    review_warnings: int = 0
    ci_passed: bool | None = None
    unresolved_threads: int = 0
    # Cases this run EXECUTED and their verdict — the QC half, which the rubric could
    # not see at all. `ci_passed` is a different signal: that is the repository's own
    # test suite run as a gate, not the test cases a QC role executed against the item.
    # Without these a QC run that found a real defect scored — and gated — identically
    # to one that found nothing, so the verdict had no authority over the pipeline.
    tests_failed: int = 0
    tests_blocked: int = 0


@dataclass
class RunScore:
    score: int                                   # 0..100
    grade: str                                   # A / B / C / D / F
    gate: str                                    # "auto" | "review" | "escalate"
    components: dict[str, int] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)


def _grade(score: int) -> str:
    return (
        "A" if score >= 90 else
        "B" if score >= 80 else
        "C" if score >= 70 else
        "D" if score >= 60 else
        "F"
    )


def score_run(inp: ScoreInput, *, auto_min: int = 85, review_min: int = 60) -> RunScore:
    """Grade a run 0–100 from its signals; decide the gate against the thresholds."""
    comp: dict[str, int] = {}
    why: list[str] = []

    # ── delivery (35) ──
    if inp.needs_human or inp.had_error or not inp.completed:
        comp["delivery"] = 0
        why.append("không hoàn tất / có lỗi")
    elif inp.has_pr:
        comp["delivery"] = 35
    elif not inp.expected_pr:
        comp["delivery"] = 35
        why.append("role này không mở PR — tính là đã giao đủ")
    else:
        comp["delivery"] = 20
        why.append("hoàn tất nhưng không mở PR (report)")

    # ── review (30); unknown → 18 (neutral-low) ──
    if inp.review_passed is None:
        comp["review"] = 18
        why.append("chưa chạy auto-review")
    else:
        r = 30 - min(inp.review_critical, 2) * 15 - min(inp.review_warnings * 2, 10)
        comp["review"] = max(r, 0)
        if inp.review_critical:
            why.append(f"{inp.review_critical} lỗi critical từ auto-review")
        elif inp.review_warnings:
            why.append(f"{inp.review_warnings} cảnh báo từ auto-review")

    # ── ci (20); unknown → 10 (neutral-low) ──
    if inp.ci_passed is None:
        comp["ci"] = 10
        why.append("chưa biết trạng thái CI")
    elif inp.ci_passed:
        comp["ci"] = 20
    else:
        comp["ci"] = 0
        why.append("CI đỏ")

    # ── scope (15) ──
    # "Changed no files" is a defect for a role that delivers a diff and a description
    # of the job for one that does not: a QC pass is cases and a verdict, a BA pass is a
    # written item. Penalising it there marks the role down for being itself.
    if inp.files_changed == 0 and not inp.expected_pr:
        sc = 15
        why.append("role này không tạo diff — không tính thiếu file")
    else:
        sc = 15 if inp.files_changed > 0 else 0
        if inp.files_changed == 0:
            why.append("không có file nào thay đổi")
    penalty = min(inp.unresolved_threads * 2, 6)
    if penalty:
        why.append(f"{inp.unresolved_threads} thread review chưa giải quyết")
    comp["scope"] = max(sc - penalty, 0)

    total = max(0, min(100, sum(comp.values())))
    gate = "auto" if total >= auto_min else "review" if total >= review_min else "escalate"

    # A failed or blocked case forces the gate REGARDLESS of the score, and the score is
    # deliberately left alone. The two measure different things and conflating them was
    # the mistake: the score asks "how well did this run go", the gate asks "must a human
    # look before this moves". A QC run that finds a real defect went WELL — marking it
    # down would penalise the agent for doing its job — and it is precisely the run
    # nobody may let through unread.
    #
    # Blocked counts too. A case that could not be run is not a case that passed, which
    # is already how the comment renderer reads it (TestReport.is_clean).
    if inp.tests_failed or inp.tests_blocked:
        gate = "escalate"
        if inp.tests_failed:
            why.append(f"{inp.tests_failed} test case KHÔNG ĐẠT — người phải quyết định")
        if inp.tests_blocked:
            why.append(f"{inp.tests_blocked} test case chưa chạy được — chưa có kết luận")

    return RunScore(score=total, grade=_grade(total), gate=gate, components=comp, reasons=why)


def score_badge_html(rs: RunScore) -> str:
    """Small HTML snippet for the ADO comment — grade pill + breakdown + reasons."""
    color = {"A": "#16a34a", "B": "#16a34a", "C": "#d97706", "D": "#d97706", "F": "#dc2626"}[rs.grade]
    parts = " · ".join(f"{k} {v}" for k, v in rs.components.items())
    reasons = ("<br/>" + "; ".join(rs.reasons)) if rs.reasons else ""
    return (
        f'<div><b>📊 Run score:</b> '
        f'<span style="color:{color};font-weight:700">{rs.score}/100 ({rs.grade})</span> '
        f'— gate: <code>{rs.gate}</code><br/>'
        f'<span style="color:#64748b">{parts}{reasons}</span></div>'
    )
