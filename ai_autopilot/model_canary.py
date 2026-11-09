"""Model canary — a handful of questions with known answers, asked before a model change
is trusted.

The failure it exists for is quiet: a model version changes, a default moves with it
(output budget, effort, a stop rule), and the answer that comes back is *empty*. Nothing
raises. The run "succeeds", the reviewer reads nothing, the score is computed over
nothing. A test suite does not see it because no test asked this model this question.

So the check is deliberately dull and deterministic. Each probe has a prompt and a rule
that needs no model to grade — non-empty, long enough, contains the right token, parses
as JSON. Five probes finish in seconds and cost almost nothing, which is the point: cheap
enough to run on *every* change to the model or its effort settings, and strict enough
that "empty" or "truncated" can never pass.

The result is stored against a fingerprint of the settings that shape the answer. Doctor
compares the live fingerprint to the last one that passed: a change nobody ran the canary
for shows up as a warning instead of as a bad week.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ai_autopilot.logging_config import describe_exc

Ask = Callable[[str], Awaitable[str]]

# Settings that change what a model returns. Anything that does not belong here (timeouts,
# concurrency) must not: a fingerprint that moves on every edit trains people to ignore it.
_FINGERPRINT_FIELDS = ("claude_model", "claude_effort_chat", "claude_effort_agentic",
                       "claude_effort_task")


@dataclass(frozen=True)
class Probe:
    id: str
    prompt: str
    min_chars: int = 1
    must_match: str = ""        # regex, searched case-insensitively
    must_be_json: bool = False


PROBES: tuple[Probe, ...] = (
    Probe("arithmetic", "Reply with ONLY the result of 17*23 as a number, nothing else.",
          must_match=r"\b391\b"),
    Probe("exact-token", "Reply with exactly the single word: PINEAPPLE", must_match=r"pineapple"),
    Probe("json", 'Reply with ONLY a JSON object {"status":"ok","count":3}, no prose, no fences.',
          must_be_json=True, must_match=r'"count"\s*:\s*3'),
    Probe("summary", "In two or three sentences, explain why a disk that is 100% full can make "
          "a database stop accepting writes.", min_chars=80, must_match=r"disk|space|full"),
    Probe("code", "Write a Python function `is_even(n)` that returns True for even n. "
          "Reply with the code only.", min_chars=20, must_match=r"def\s+is_even"),
)


@dataclass
class ProbeResult:
    id: str
    passed: bool
    reason: str = ""
    chars: int = 0
    seconds: float = 0.0


@dataclass
class CanaryReport:
    fingerprint: str
    model: str = ""
    results: list[ProbeResult] = field(default_factory=list)
    ran_at: str = ""

    @property
    def passed(self) -> bool:
        return bool(self.results) and all(r.passed for r in self.results)

    @property
    def failures(self) -> list[ProbeResult]:
        return [r for r in self.results if not r.passed]

    def summary(self) -> str:
        ok = sum(r.passed for r in self.results)
        head = f"{ok}/{len(self.results)} probes passed"
        if not self.failures:
            return head
        return head + " - " + "; ".join(f"{r.id}: {r.reason}" for r in self.failures)


def grade(probe: Probe, answer: str) -> tuple[bool, str]:
    """Pure: does ``answer`` satisfy ``probe``? Returns ``(passed, reason)``."""
    text = (answer or "").strip()
    if not text:
        return False, "EMPTY answer"
    if len(text) < probe.min_chars:
        return False, f"too short ({len(text)} < {probe.min_chars} chars)"
    if probe.must_be_json:
        body = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
        try:
            json.loads(body)
        except ValueError:
            return False, "not valid JSON"
    if probe.must_match and not re.search(probe.must_match, text, re.IGNORECASE):
        return False, f"missing expected content /{probe.must_match}/"
    return True, ""


def fingerprint(config: Any) -> str:
    """Short stable id of the settings that shape a model's answer."""
    raw = "|".join(f"{k}={getattr(config, k, '') or ''}" for k in _FINGERPRINT_FIELDS)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]  # noqa: S324 — not security


async def run_canary(ask: Ask, *, fp: str, model: str = "", timeout_seconds: float = 60,
                     probes: tuple[Probe, ...] = PROBES) -> CanaryReport:
    """Ask every probe. Never raises: an exception or timeout is a failed probe, because
    "the model did not answer" is exactly what this exists to surface."""
    report = CanaryReport(fp, model, ran_at=datetime.now(UTC).isoformat(timespec="seconds"))
    for probe in probes:
        started = time.monotonic()
        try:
            answer = await asyncio.wait_for(ask(probe.prompt), timeout=timeout_seconds)
        except TimeoutError:
            report.results.append(ProbeResult(
                probe.id, False, f"timed out after {timeout_seconds:.0f}s",
                seconds=time.monotonic() - started))
            continue
        except Exception as exc:  # noqa: BLE001
            report.results.append(ProbeResult(probe.id, False, f"error: {describe_exc(exc)[:120]}",
                                              seconds=time.monotonic() - started))
            continue
        ok, reason = grade(probe, answer)
        report.results.append(ProbeResult(probe.id, ok, reason, len((answer or "").strip()),
                                          round(time.monotonic() - started, 2)))
    return report


# ── state: the last canary and the fingerprint it vouched for ───────────────


def state_path(workspace: str) -> Path:
    return Path(workspace) / ".autopilot" / "canary.json"


def save(workspace: str, report: CanaryReport) -> None:
    path = state_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(report), indent=2, ensure_ascii=False), encoding="utf-8")


def load(workspace: str) -> CanaryReport | None:
    try:
        data = json.loads(state_path(workspace).read_text(encoding="utf-8"))
        return CanaryReport(
            fingerprint=data["fingerprint"], model=data.get("model", ""),
            ran_at=data.get("ran_at", ""),
            results=[ProbeResult(**r) for r in data.get("results", [])])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def status(workspace: str, current_fp: str) -> tuple[str, str]:
    """``(state, message)``; state is ok | never | stale | failed.

    ``stale`` is the interesting one: a canary passed, but for different settings than
    the ones running now.
    """
    last = load(workspace)
    if last is None:
        return "never", "No model canary has been run for this workspace."
    if not last.passed:
        return "failed", f"Last canary FAILED ({last.ran_at}): {last.summary()}"
    if last.fingerprint != current_fp:
        return "stale", (f"Model settings changed since the last passing canary ({last.ran_at}, "
                         f"model '{last.model or 'default'}').")
    return "ok", f"Canary passed {last.ran_at}: {last.summary()}"


# ── the real asker (used by the CLI only; tests inject their own) ───────────


def claude_ask(config: Any) -> Ask:
    async def _ask(prompt: str) -> str:
        import tempfile

        from ai_autopilot.execution.claude_client import run_claude

        run = await run_claude(
            prompt, tempfile.gettempdir(), timeout_seconds=60,
            model=getattr(config, "claude_model", "") or None, max_turns=1,
            allowed_tools=[], effort=getattr(config, "claude_effort_chat", "") or None,
        )
        return run.text or ""

    return _ask


def run_cli(argv: list[str]) -> int:
    """``ai-autopilot canary`` — exit 0 pass, 1 fail. Needs only the config and Claude."""
    import sys

    from ai_autopilot.config import load_settings

    config = load_settings()
    workspace = config.workspace_directory or "."
    fp = fingerprint(config)
    if "--status" in argv:
        state, msg = status(workspace, fp)
        print(f"[{state}] {msg}")
        return 0 if state == "ok" else 1
    report = asyncio.run(run_canary(claude_ask(config), fp=fp, model=config.claude_model))
    save(workspace, report)
    for r in report.results:
        mark = "PASS" if r.passed else "FAIL"
        print(f"  {mark}  {r.id:<12}{r.chars:>5} chars  {r.seconds:>5.1f}s  {r.reason}")
    print(("Canary PASSED" if report.passed else "Canary FAILED") + f" - {report.summary()}")
    if not report.passed:
        print("Do not roll this model/effort change out until the failing probe is explained.",
              file=sys.stderr)
    return 0 if report.passed else 1
