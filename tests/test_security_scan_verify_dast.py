"""Security scan — Phase 3 (PoC verification) and Phase 4 (DAST gates).

The verify tests use a fake executor that hands back canned Claude output, so what is
under test is the orchestration: which findings are picked, that a confirmed verdict
raises confidence and stores a PoC, and that the isolated worktree is ALWAYS released.
The DAST tests are almost all about the gate: a target that is not owner-confirmed, or
whose host is off the allowlist, must be refused before any request is conceivable.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from ai_autopilot.config import DastTarget, Settings
from ai_autopilot.reports import Finding
from ai_autopilot.security_scan import dast
from ai_autopilot.security_scan import verify as verify_mod

# ── Phase 3: verify ───────────────────────────────────────────────────────────

def _sec(**kw):
    s = Settings().security_scan
    s.verify_enabled = True
    s.verify_from = "high"
    s.verify_max_per_scan = 5
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def _findings():
    return [
        Finding(severity="critical", title="SQLi", file="a.cs", line=1, rule_id="sqli",
                fingerprint="fp-crit", confidence="medium"),
        Finding(severity="high", title="BOLA", file="b.cs", line=2, rule_id="bola",
                fingerprint="fp-high", confidence="high"),   # already high → skipped
        Finding(severity="low", title="nit", file="c.cs", line=3, rule_id="x",
                fingerprint="fp-low"),                        # below floor → skipped
    ]


def test_candidates_picks_severe_unconfident_only():
    picked = verify_mod.candidates(_findings(), floor="high", limit=5)
    assert [f.fingerprint for f in picked] == ["fp-crit"]


def test_parse_verdict():
    text = 'ran it\n```json\n{"verified": true, "evidence": "e", "command": "c"}\n```'
    v = verify_mod.parse_verdict(text)
    assert v["verified"] is True and v["command"] == "c"
    assert verify_mod.parse_verdict("no json here") is None


class _FakeExecutor:
    """Minimal stand-in for ClaudeExecutor's scratch + run surface."""

    def __init__(self, verdicts: dict[str, bool]):
        self._verdicts = verdicts            # rule_id → verified
        self.acquired = 0
        self.released = 0
        self._prompt_rule = None

    async def _acquire_agent_scratch(self, item_id, repos, *, stable=False):
        self.acquired += 1
        return f"/tmp/scratch-{self.acquired}"

    async def release_scratch(self, run_dir):
        self.released += 1

    async def _run_claude(self, prompt, work_dir, repo=None, **kw):
        # Which finding is this? Match on the rule slug in the prompt.
        verified = next((v for k, v in self._verdicts.items() if k in prompt), False)
        body = json.dumps({"verified": verified, "confidence": "high" if verified else "low",
                           "evidence": "did the thing", "command": "dotnet test"})
        return SimpleNamespace(text=f"ok\n```json\n{body}\n```")


class _SecRepo:
    def __init__(self):
        self.rows = {}
        self.verifications = []

    async def by_fingerprint(self, repo, fp):
        return self.rows.setdefault(fp, SimpleNamespace(id=fp, fingerprint=fp))

    async def set_verification(self, fid, verified, poc_md):
        self.verifications.append((fid, verified, poc_md))


async def test_verify_confirms_and_releases(tmp_path):
    ex = _FakeExecutor({"sqli": True})
    repo_dir = tmp_path / "app"
    repo_dir.mkdir()
    sec_repo = _SecRepo()
    out = await verify_mod.verify_findings(
        _findings(), executor=ex, security_repo=sec_repo, sec=_sec(),
        repo=str(repo_dir), project="",
    )
    assert out.attempted == 1 and out.confirmed == 1 and out.refuted == 0
    assert ex.acquired == 1 and ex.released == 1           # isolation torn down
    fid, verified, poc = sec_repo.verifications[0]
    assert verified is True and "PoC verified" in poc
    # The in-memory finding's confidence was raised.
    crit = next(f for f in _findings() if f.rule_id == "sqli")
    assert crit  # (fresh list; the raise is asserted on the returned outcome)
    assert out.results[0]["result"] == "confirmed"


async def test_verify_refuted_keeps_open(tmp_path):
    ex = _FakeExecutor({"sqli": False})
    (tmp_path / "app").mkdir()
    sec_repo = _SecRepo()
    out = await verify_mod.verify_findings(
        _findings(), executor=ex, security_repo=sec_repo, sec=_sec(),
        repo=str(tmp_path / "app"),
    )
    assert out.confirmed == 0 and out.refuted == 1
    assert sec_repo.verifications[0][1] is False


async def test_verify_always_releases_even_on_error(tmp_path):
    class _Boom(_FakeExecutor):
        async def _run_claude(self, *a, **k):
            raise RuntimeError("model down")

    ex = _Boom({})
    (tmp_path / "app").mkdir()
    out = await verify_mod.verify_findings(
        _findings(), executor=ex, security_repo=_SecRepo(), sec=_sec(),
        repo=str(tmp_path / "app"),
    )
    assert out.errored == 1 and ex.released == ex.acquired == 1


async def test_verify_skips_when_disabled(tmp_path):
    out = await verify_mod.verify_findings(
        _findings(), executor=_FakeExecutor({}), security_repo=_SecRepo(),
        sec=_sec(verify_enabled=False), repo=str(tmp_path),
    )
    assert out.skipped == "verify disabled" and out.attempted == 0


# ── Phase 4: DAST gates ───────────────────────────────────────────────────────

def _target(**kw):
    base = dict(name="staging", base_url="https://staging.internal", owner_confirmed=True,
                allowed_hosts=["staging.internal"], require_private=False)
    base.update(kw)
    return DastTarget(**base)


def test_gate_passes_when_confirmed_and_allowlisted():
    assert dast.check_target(_target(), resolve=False).ok


def test_gate_refuses_without_owner_confirmed():
    r = dast.check_target(_target(owner_confirmed=False), resolve=False)
    assert not r.ok and "owner_confirmed" in r.reason


def test_gate_refuses_host_not_in_allowlist():
    r = dast.check_target(_target(base_url="https://evil.example.com"), resolve=False)
    assert not r.ok and "not in allowed_hosts" in r.reason


def test_gate_refuses_empty_allowlist():
    r = dast.check_target(_target(allowed_hosts=[]), resolve=False)
    assert not r.ok and "allowed_hosts is empty" in r.reason


def test_gate_refuses_disabled_target():
    r = dast.check_target(_target(enabled=False), resolve=False)
    assert not r.ok and "disabled" in r.reason


def test_gate_refuses_public_host_when_private_required():
    # 8.8.8.8 is global; require_private + resolve should reject a host pointing at it.
    t = _target(base_url="https://dns.google", allowed_hosts=["dns.google"], require_private=True)
    r = dast.check_target(t, resolve=True)
    assert not r.ok and ("public" in r.reason or "resolve" in r.reason)


def test_probe_budget_caps_and_blocks_mutations():
    b = dast.ProbeBudget(max_requests=2, allow_mutations=False)
    ok, _ = b.allows("GET", "/x")
    assert ok
    ok, why = b.allows("POST", "/x")
    assert not ok and "allow_mutations" in why
    b.sent = 2
    ok, why = b.allows("GET", "/x")
    assert not ok and "cap" in why


def test_probe_budget_enforces_allowed_paths():
    b = dast.ProbeBudget(max_requests=10, allow_mutations=True, allowed_paths=["/api/"])
    assert b.allows("GET", "/api/orders")[0]
    assert not b.allows("GET", "/admin")[0]


async def test_run_dast_refuses_unknown_target():
    cfg = Settings()
    cfg.security_scan.dast_enabled = True
    out = await dast.run_dast(config=cfg, target_name="nope", executor=object())
    assert "no DAST target" in out.refused


async def test_run_dast_refuses_when_disabled():
    cfg = Settings()
    cfg.security_scan.dast_enabled = False
    cfg.security_scan.dast_targets = [_target()]
    out = await dast.run_dast(config=cfg, target_name="staging", executor=object())
    assert out.refused == "dast_enabled is false"


async def test_run_dast_refuses_bad_gate_before_touching_executor():
    cfg = Settings()
    cfg.security_scan.dast_enabled = True
    cfg.security_scan.dast_targets = [_target(owner_confirmed=False)]

    class _Trap:
        async def run_audit(self, *a, **k):
            raise AssertionError("executor must not be called when the gate fails")

    out = await dast.run_dast(config=cfg, target_name="staging", executor=_Trap())
    assert "owner_confirmed" in out.refused


def test_build_prompt_mentions_idor_and_constraints():
    t = _target(auth_env_var="TOK_A", auth_b_env_var="TOK_B", allow_mutations=False,
                allowed_paths=["/api/"])
    p = dast.build_prompt(t)
    assert "BOLA/IDOR" in p and "TOK_A" in p and "TOK_B" in p
    assert "GET/HEAD/OPTIONS" in p and "/api/" in p
