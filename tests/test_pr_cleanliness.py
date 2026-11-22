"""Merged-without-a-person's-edit: the pure assessment and the best-effort gather."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from ai_autopilot import pr_cleanliness
from ai_autopilot.data.entities import QualityKind
from ai_autopilot.north_star import merge_clean_kpi
from ai_autopilot.pr_cleanliness import assess, gather


def _url(n: int, repo: str = "repo") -> str:
    return f"https://dev.azure.com/org/P/_git/{repo}/pullrequest/{n}"


def _run(item: int, pr: int, project: str = "P") -> SimpleNamespace:
    return SimpleNamespace(work_item_id=item, pr_url=_url(pr), project=project)


def test_a_single_iteration_is_clean():
    res = assess([(1, 10)], {1: 1}, {})
    assert res.rate == 100 and res.clean == 1


def test_a_push_after_open_is_a_persons_unless_the_autopilot_revised():
    res = assess(
        [(1, 10), (2, 20), (3, 30)],
        {1: 3, 2: 3, 3: 2},
        {20: 2},                    # item 20 had two /ai revise rounds
    )
    by_pr = {a.pr_id: a for a in res.assessed}
    assert not by_pr[1].clean and by_pr[1].human_pushes == 2
    assert by_pr[2].clean           # both extra pushes were the autopilot's
    assert not by_pr[3].clean
    assert res.rate == 33


def test_unreadable_pr_is_unknown_never_clean():
    res = assess([(1, 10), (2, 20)], {1: 1, 2: None}, {})
    assert res.unknown == 1 and len(res.assessed) == 1
    assert res.rate == 100          # judged over the PRs we could actually read


def test_no_merged_pr_gives_no_rate():
    assert assess([], {}, {}).rate is None
    kpi = merge_clean_kpi(assess([], {}, {}))
    assert not kpi.has_data


def test_kpi_reports_unknown_count_and_tone():
    kpi = merge_clean_kpi(assess([(1, 10), (2, 20), (3, 30)], {1: 1, 2: 1, 3: None}, {}))
    assert kpi.value == "100%" and "1 PR chưa kiểm được" in kpi.sub and kpi.tone == "good"
    only_unknown = merge_clean_kpi(assess([(1, 10)], {1: None}, {}))
    assert not only_unknown.has_data and "chưa kiểm được" in only_unknown.sub


class _Ado:
    def __init__(self, table: dict[int, list | None]):
        self.table = table
        self.calls: list[int] = []

    async def get_pull_request_iterations(self, repo, pr_id, project=""):
        self.calls.append(pr_id)
        value = self.table[pr_id]
        if isinstance(value, Exception):
            raise value
        return value


def test_gather_only_reads_merged_prs_and_survives_failures():
    pr_cleanliness._CACHE.clear()
    ado = _Ado({1: [{}], 2: [{}, {}, {}], 3: RuntimeError("boom")})
    runs = [_run(10, 1), _run(20, 2), _run(30, 3), _run(40, 4)]      # PR 4 not merged
    quality = [SimpleNamespace(kind=QualityKind.PR_REVISION, work_item_id=20)]
    res = asyncio.run(gather(ado, runs, quality, {1, 2, 3}))
    assert 4 not in ado.calls
    assert res.unknown == 1                           # PR 3 raised
    by_pr = {a.pr_id: a for a in res.assessed}
    assert by_pr[1].clean
    assert not by_pr[2].clean and by_pr[2].human_pushes == 1   # 2 pushes, 1 was revise


def test_gather_caches_merged_prs():
    pr_cleanliness._CACHE.clear()
    ado = _Ado({1: [{}, {}]})
    asyncio.run(gather(ado, [_run(10, 1)], [], {1}))
    asyncio.run(gather(ado, [_run(10, 1)], [], {1}))
    assert ado.calls == [1]
