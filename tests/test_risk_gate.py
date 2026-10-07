"""Risk gate: risky changes are held for a person instead of being handed on."""

from __future__ import annotations

from ai_autopilot import risk
from ai_autopilot.config import SdlcRole, Settings
from ai_autopilot.data import PipelineState
from ai_autopilot.models import ExecutionResult, WorkItemInfo
from tests.test_poller_agent import _poller

PATTERNS = Settings().risk_patterns


# ── classification ─────────────────────────────────────────────────────────────


def test_globs_cross_folders_only_where_they_say_so():
    assert risk.matches("src/Data/Migrations/2026_add.cs", "**/Migrations/**")
    assert risk.matches("Migrations/x.cs", "**/Migrations/**")          # root level too
    assert risk.matches("db/scripts/seed.sql", "*.sql")                  # name anywhere
    assert risk.matches("k8s/api/deploy.yaml", "k8s/**")
    assert not risk.matches("src/k8s/deploy.yaml", "k8s/**")              # anchored
    assert risk.matches("\\Api\\appsettings.Production.json", "**/appsettings*.json")
    assert risk.matches("./.github/workflows/ci.yml", ".github/workflows/**")
    assert risk.matches("web/PACKAGE.JSON", "package.json")              # case-insensitive


def test_ordinary_code_is_normal():
    v = risk.classify(["src/Orders/OrderService.cs", "web/src/app/list.component.ts"],
                      PATTERNS, 25)
    assert v.level == risk.NORMAL and v.reasons == []


def test_each_matching_pattern_is_its_own_reason():
    v = risk.classify(
        ["/Api/Migrations/2026_x.cs", "/Api/Api.csproj", "/Api/Services/AuthService.cs"],
        PATTERNS, 25,
    )
    assert v.high
    joined = " | ".join(v.reasons)
    assert "Migrations" in joined and ".csproj" in joined and "Auth" in joined
    assert "Api/Api.csproj" in v.files


def test_size_alone_is_a_risk():
    files = [f"src/f{i}.cs" for i in range(30)]
    v = risk.classify(files, [], 25)
    assert v.high and "30 file" in v.reasons[0]
    assert risk.classify(files, [], 0).level == risk.NORMAL   # 0 = no size rule


def test_a_malformed_pattern_cannot_break_the_gate():
    assert risk.classify(["a.cs"], ["[", None, ""], 0).level == risk.NORMAL


# ── poller wiring ─────────────────────────────────────────────────────────────


def _risky_result() -> ExecutionResult:
    r = ExecutionResult.ok(7, "agent", "done")
    r.pr_url = "https://dev.azure.com/o/P/_git/Api/pullrequest/12"
    r.files_changed = ["Api/Migrations/2026_drop.cs", "Api/Orders.cs"]
    return r


class _PrAdo:
    def __init__(self, files=None):
        self.pr_comments: list[tuple[str, int, str]] = []
        self.files = files or []

    async def add_pull_request_comment(self, repo, pr_id, text, *, active=False):
        self.pr_comments.append((repo, pr_id, text))
        return True

    async def pull_request_changed_files(self, url):
        return self.files


def _with_pr_api(c, files=None):
    pr = _PrAdo(files)
    c.ado.add_pull_request_comment = pr.add_pull_request_comment
    c.ado.pull_request_changed_files = pr.pull_request_changed_files
    return pr


async def test_a_risky_run_is_held_not_handed_on():
    svc, c = _poller(sdlc_roles={
        "dev": SdlcRole(stages=["implement", "pr"], waits_in="Ready for Development",
                        done="Ready for Testing"),
    }, processed_tag="autopilot-done")
    pr = _with_pr_api(c)
    item = WorkItemInfo(id=7, title="t", work_item_type="Task", state="Ready for Development")
    result = _risky_result()

    await svc._handle_agent_result(item, result)
    await svc._apply_sdlc_handoff(item, result, "dev")

    assert result.needs_human and result.success          # held, NOT failed
    assert (7, PipelineState.NEEDS_HUMAN) in c.state_repo.calls
    assert (7, "autopilot-risk-review") in c.ado.tags
    assert (7, c.config.escalation_tag) in c.ado.tags
    assert not any(s == "Ready for Testing" for _i, s in c.ado.states)   # no hand-off
    held = [t for _i, t in c.ado.comments if "rủi ro cao" in t]
    assert len(held) == 1 and "Migrations" in held[0]
    assert pr.pr_comments and pr.pr_comments[0][:2] == ("Api", 12)


async def test_headless_runs_are_judged_from_the_pr_file_list_without_rescoring_them():
    svc, c = _poller()
    _with_pr_api(c, files=["/k8s/api/deploy.yaml"])
    result = ExecutionResult.ok(7, "agent", "done")
    result.pr_url = "https://dev.azure.com/o/P/_git/Api/pullrequest/12"

    await svc._handle_agent_result(WorkItemInfo(id=7, title="t"), result)

    assert result.needs_human
    assert result.files_changed == []        # looked at, not written into the result


async def test_a_normal_run_goes_through_as_before():
    svc, c = _poller()
    _with_pr_api(c)
    result = ExecutionResult.ok(7, "agent", "done")
    result.pr_url = "https://dev.azure.com/o/P/_git/Api/pullrequest/12"
    result.files_changed = ["Api/Orders.cs"]

    await svc._handle_agent_result(WorkItemInfo(id=7, title="t"), result)

    assert not result.needs_human
    assert (7, PipelineState.IN_REVIEW) in c.state_repo.calls


async def test_the_gate_can_be_turned_off():
    svc, c = _poller(risk_gate_enabled=False)
    _with_pr_api(c)
    result = _risky_result()
    await svc._handle_agent_result(WorkItemInfo(id=7, title="t"), result)
    assert not result.needs_human


async def test_a_gate_error_lets_the_run_through(monkeypatch):
    svc, c = _poller()

    def boom(*_a, **_k):
        raise RuntimeError("bad pattern table")

    monkeypatch.setattr(risk, "classify", boom)
    result = _risky_result()
    await svc._handle_agent_result(WorkItemInfo(id=7, title="t"), result)
    assert not result.needs_human
    assert (7, PipelineState.IN_REVIEW) in c.state_repo.calls
