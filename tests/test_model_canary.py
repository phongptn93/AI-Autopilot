"""Model canary — empty or truncated answers must never pass."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from ai_autopilot import model_canary as mc

GOOD = {
    "arithmetic": "391",
    "exact-token": "PINEAPPLE",
    "json": '{"status":"ok","count":3}',
    "summary": "When the disk is full the database cannot write its log or data pages, so "
               "it refuses writes or flips read-only to protect what is already stored.",
    "code": "def is_even(n):\n    return n % 2 == 0",
}


def _answers(overrides=None):
    table = {**GOOD, **(overrides or {})}

    async def ask(prompt):
        for probe in mc.PROBES:
            if probe.prompt == prompt:
                value = table[probe.id]
                if isinstance(value, Exception):
                    raise value
                return value
        raise AssertionError("unknown prompt")

    return ask


def _run(ask, fp="f1"):
    return asyncio.run(mc.run_canary(ask, fp=fp, model="m"))


def test_a_healthy_model_passes_every_probe():
    report = _run(_answers())
    assert report.passed and report.summary() == "5/5 probes passed"


def test_empty_answer_is_the_failure_it_exists_for():
    report = _run(_answers({"summary": "", "json": "   "}))
    assert not report.passed
    assert {r.id: r.reason for r in report.failures} == {
        "summary": "EMPTY answer", "json": "EMPTY answer"}


def test_truncated_and_wrong_answers_fail():
    report = _run(_answers({"summary": "The disk is full.", "arithmetic": "about 400"}))
    reasons = {r.id: r.reason for r in report.failures}
    assert "too short" in reasons["summary"] and "missing expected" in reasons["arithmetic"]


def test_json_in_a_fence_is_accepted_but_prose_is_not():
    assert _run(_answers({"json": '```json\n{"status":"ok","count":3}\n```'})).passed
    assert "not valid JSON" in _run(_answers({"json": "Sure! count is 3"})).failures[0].reason


def test_exception_and_timeout_are_failed_probes_not_crashes():
    report = _run(_answers({"code": RuntimeError("boom")}))
    assert report.failures[0].reason.startswith("error:")

    async def slow(_):
        await asyncio.sleep(5)

    timed = asyncio.run(mc.run_canary(slow, fp="f", timeout_seconds=0.01,
                                      probes=(mc.PROBES[0],)))
    assert "timed out" in timed.failures[0].reason


def test_fingerprint_tracks_model_and_effort_only():
    a = SimpleNamespace(claude_model="m1", claude_effort_chat="low", other=1)
    b = SimpleNamespace(claude_model="m1", claude_effort_chat="low", other=2)
    c = SimpleNamespace(claude_model="m2", claude_effort_chat="low")
    assert mc.fingerprint(a) == mc.fingerprint(b) != mc.fingerprint(c)


def test_status_walks_never_ok_stale_failed(tmp_path):
    ws = str(tmp_path)
    assert mc.status(ws, "f1")[0] == "never"
    mc.save(ws, _run(_answers(), fp="f1"))
    assert mc.status(ws, "f1")[0] == "ok"
    assert mc.status(ws, "f2")[0] == "stale"
    mc.save(ws, _run(_answers({"json": ""}), fp="f1"))
    assert mc.status(ws, "f1")[0] == "failed"


def test_doctor_stays_quiet_until_a_change_is_untested(tmp_path):
    from ai_autopilot import doctor

    cfg = SimpleNamespace(workspace_directory=str(tmp_path), claude_model="m1",
                          claude_effort_chat="low", claude_effort_agentic="", claude_effort_task="")
    assert doctor.check_model_canary(cfg) == []                       # never run: silent
    mc.save(str(tmp_path), _run(_answers(), fp=mc.fingerprint(cfg)))
    assert doctor.check_model_canary(cfg) == []                       # passing, same settings
    cfg.claude_model = "m2"
    out = doctor.check_model_canary(cfg)
    assert [f.level for f in out] == [doctor.WARN] and "canary" in out[0].fix


@pytest.mark.parametrize("probe", mc.PROBES)
def test_every_probe_has_a_rule_a_model_cannot_dodge(probe):
    assert not mc.grade(probe, "")[0]          # nothing passes on emptiness
