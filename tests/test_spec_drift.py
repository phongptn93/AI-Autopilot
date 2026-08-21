"""Tests for spec-drift reporting and the PR ↔ work-item link guarantee."""

from __future__ import annotations

from types import SimpleNamespace

from ai_autopilot import spec_drift
from ai_autopilot.config import Settings
from ai_autopilot.execution.result_contract import Deviation, _parse
from ai_autopilot.models import ExecutionResult, WorkItemInfo
from ai_autopilot.services.spec_guard import SpecGuard

PR = "https://dev.azure.com/org/proj/_git/api/pullrequest/77"


def _dev(**kw):
    return Deviation(**{"kind": "logic_differs", "summary": "s", **kw})


# ── the contract the agent writes ────────────────────────────────────────────

def test_deviations_parse_from_objects_and_from_plain_strings():
    """A model told to "list what differed" writes strings about as often as objects.
    Rejecting the string form would drop exactly the reports this feature collects."""
    result = _parse({
        "status": "completed",
        "deviations": [
            {"kind": "spec_unclear", "summary": "AC không nói khi mã trùng",
             "detail": "chọn báo lỗi", "where": "AC-3"},
            "Tồn kho tính theo lô thay vì theo mã",
            {"kind": "nonsense-kind", "summary": "vẫn giữ"},
            {"summary": "   "},          # empty → dropped
            12345,                        # not a note at all → dropped
        ],
    })
    assert result is not None
    kinds = [d.kind for d in result.deviations]
    summaries = [d.summary for d in result.deviations]
    assert kinds == ["spec_unclear", "assumption", "assumption"]
    assert summaries[1] == "Tồn kho tính theo lô thay vì theo mã"
    assert result.deviations[0].where == "AC-3"


def test_a_run_with_no_deviations_is_the_normal_case():
    assert _parse({"status": "completed"}).deviations == []
    assert _parse({"status": "completed", "deviations": []}).deviations == []


# ── what a human ends up reading ─────────────────────────────────────────────

def test_comment_carries_the_prefix_and_says_what_is_expected():
    notice = spec_drift.render_comment(
        [_dev(summary="Tính tồn theo lô", where="AC-3"), _dev(kind="spec_gap", summary="Mã trùng")],
        pr_url=PR, tag="spec-update-needed",
    )
    assert notice.count == 2
    # The prefix is a contract with future queries (WIQL Contains, dashboard scans),
    # so it must survive any rewording around it.
    assert spec_drift.DRIFT_PREFIX in notice.html
    assert "AC-3" in notice.html and PR in notice.html
    assert "spec-update-needed" in notice.html
    assert "Cần làm" in notice.html          # the reader is told what to do, not just what happened


def test_agent_text_cannot_break_the_comment_markup():
    notice = spec_drift.render_comment([_dev(summary="<script>alert(1)</script> & co")])
    assert "<script>" not in notice.html
    assert "&lt;script&gt;" in notice.html


def test_a_pasted_diff_is_bounded():
    notice = spec_drift.render_comment([_dev(summary="x", detail="y" * 5000)])
    assert len(notice.html) < 2500 and "…" in notice.html


def test_nothing_to_report_renders_nothing():
    assert spec_drift.render_comment([]).is_empty
    assert spec_drift.render_comment([Deviation(summary="  ")]).is_empty
    assert spec_drift.render_pr_comment([]) == ""


def test_already_reported_matches_the_prefix():
    """A rework re-runs the agent, which re-reports the same decisions; without this
    the item collects one identical notice per revision."""
    assert spec_drift.already_reported([f"<div><b>{spec_drift.DRIFT_PREFIX}</b> — 2 điểm</div>"])
    assert not spec_drift.already_reported(["🔍 PR created (draft)", ""])


# ── the guard: filing, and the PR link ───────────────────────────────────────

class _FakeAdo:
    def __init__(self, linked=(), comments=(), repos=None):
        self.linked = list(linked)
        self.comments_in = list(comments)
        self.posted: list[tuple[int, str]] = []
        self.tags: list[tuple[int, str]] = []
        self.untags: list[tuple[int, str]] = []
        self.pr_comments: list[tuple[int, str]] = []
        self.links: list[tuple[int, int]] = []
        self._repos = repos if repos is not None else [
            {"id": "repo-guid", "name": "api", "project": {"id": "proj-guid"}}
        ]

    async def get_repositories(self):
        return self._repos

    async def get_pull_request(self, repo_id, pr_id, project=""):
        return {"sourceRefName": "refs/heads/feature/be/4021-thing"}

    async def get_pull_request_work_items(self, repo_id, pr_id, project=""):
        return list(self.linked)

    async def link_work_item_to_pr(self, work_item_id, project_guid, repo_guid, pr_id, pr_url=""):
        self.links.append((work_item_id, pr_id))
        self.linked.append(work_item_id)
        return True

    async def get_work_item_comments(self, work_item_id):
        return [{"text": t} for t in self.comments_in]

    async def add_comment(self, work_item_id, text):
        self.posted.append((work_item_id, text))
        return True

    async def add_tag(self, work_item_id, tag):
        self.tags.append((work_item_id, tag))
        return True

    async def remove_tag(self, work_item_id, tag):
        self.untags.append((work_item_id, tag))
        return True

    async def add_pull_request_comment(self, repo_id, pr_id, text):
        self.pr_comments.append((pr_id, text))
        return True


class _FakeDriftRepo:
    def __init__(self):
        self.rows: list = []
        self.resolved: list[tuple[int, str]] = []

    async def add(self, item, pr_url, deviations):
        self.rows += list(deviations)
        return len(deviations)

    async def resolve(self, work_item_id, by=""):
        self.resolved.append((work_item_id, by))
        return len(self.rows)


def _guard(ado, **overrides):
    c = SimpleNamespace(
        config=Settings(ado_organization="https://dev.azure.com/org", **overrides),
        ado=ado, spec_drift_repo=_FakeDriftRepo(),
    )
    return SpecGuard(c), c


def _result(pr_url=PR):
    r = ExecutionResult.ok(4021, "agent", "done")
    r.pr_url = pr_url
    r.pr_urls = [pr_url] if pr_url else []
    return r


async def test_pr_missing_its_work_item_is_linked():
    """The brief also asks for this, but an instruction is advice a model can drop on a
    long run — this is the check that notices."""
    ado = _FakeAdo(linked=[])
    guard, _ = _guard(ado)
    fixed = await guard.ensure_pr_links(WorkItemInfo(id=4021, title="t"), _result())
    assert fixed == [PR]
    assert ado.links == [(4021, 77)]


async def test_pr_already_linked_is_left_alone():
    ado = _FakeAdo(linked=[4021])
    guard, _ = _guard(ado)
    assert await guard.ensure_pr_links(WorkItemInfo(id=4021, title="t"), _result()) == []
    assert ado.links == []


async def test_link_targets_the_item_named_by_the_branch():
    """A batched run opens one PR per item; each must be linked to ITS item, not to
    whichever item the finalise loop happens to be holding."""
    ado = _FakeAdo(linked=[])
    guard, _ = _guard(ado)
    await guard.ensure_pr_links(WorkItemInfo(id=9999, title="lead"), _result())
    assert ado.links == [(4021, 77)]          # from refs/heads/feature/be/4021-thing


async def test_link_check_can_be_turned_off():
    ado = _FakeAdo(linked=[])
    guard, _ = _guard(ado, pr_require_work_item_link=False)
    assert await guard.ensure_pr_links(WorkItemInfo(id=4021, title="t"), _result()) == []
    assert ado.links == []


async def test_report_drift_comments_tags_records_and_tells_the_reviewer():
    ado = _FakeAdo()
    guard, c = _guard(ado)
    n = await guard.report_drift(
        WorkItemInfo(id=4021, title="t"), _result(),
        [_dev(summary="Tính tồn theo lô"), _dev(kind="spec_gap", summary="Mã trùng")],
    )
    assert n == 2
    assert spec_drift.DRIFT_PREFIX in ado.posted[0][1]         # on the work item
    assert ado.tags == [(4021, "spec-update-needed")]          # board can filter it
    assert ado.pr_comments and str(spec_drift.DRIFT_PREFIX) in ado.pr_comments[0][1]
    assert len(c.spec_drift_repo.rows) == 2                    # BA has rows to tick off


async def test_drift_is_not_reported_twice_across_reworks():
    ado = _FakeAdo(comments=[f"<div><b>{spec_drift.DRIFT_PREFIX}</b> — 1 điểm</div>"])
    guard, c = _guard(ado)
    assert await guard.report_drift(WorkItemInfo(id=4021, title="t"), _result(), [_dev()]) == 0
    assert ado.posted == [] and ado.tags == [] and c.spec_drift_repo.rows == []


async def test_a_clean_run_files_nothing():
    ado = _FakeAdo()
    guard, _ = _guard(ado)
    assert await guard.report_drift(WorkItemInfo(id=4021, title="t"), _result(), []) == 0
    assert ado.posted == [] and ado.tags == []


async def test_feature_can_be_switched_off():
    ado = _FakeAdo()
    guard, _ = _guard(ado, spec_drift_enabled=False)
    assert await guard.report_drift(WorkItemInfo(id=1, title="t"), _result(), [_dev()]) == 0
    assert ado.posted == []


async def test_dry_run_reports_nothing_to_ado():
    ado = _FakeAdo()
    guard, _ = _guard(ado, dry_run=True)
    assert await guard.report_drift(WorkItemInfo(id=1, title="t"), _result(), [_dev()]) == 0
    assert await guard.ensure_pr_links(WorkItemInfo(id=1, title="t"), _result()) == []
    assert ado.posted == [] and ado.links == []


async def test_marking_resolved_clears_the_tag_and_says_so():
    ado = _FakeAdo()
    guard, c = _guard(ado)
    c.spec_drift_repo.rows = [_dev(), _dev()]
    n = await guard.mark_resolved(4021, by="phong")
    assert n == 2
    assert ado.untags == [(4021, "spec-update-needed")]
    assert spec_drift.RESOLVED_PREFIX in ado.posted[0][1] and "phong" in ado.posted[0][1]
    assert c.spec_drift_repo.resolved == [(4021, "phong")]


class _BrokenAdo(_FakeAdo):
    """ADO calls fail — a finished, delivered run must survive that."""

    async def get_repositories(self):
        raise RuntimeError("ADO down")

    async def add_comment(self, work_item_id, text):
        raise RuntimeError("ADO down")


async def test_a_delivered_run_survives_ado_being_down():
    """This runs AFTER the work shipped. A traceability check that raises would turn a
    finished task into a failed one — the exact opposite of its purpose."""
    guard, _ = _guard(_BrokenAdo())
    item = WorkItemInfo(id=4021, title="t")
    assert await guard.ensure_pr_links(item, _result()) == []
    assert await guard.report_drift(item, _result(), [_dev()]) == 0
