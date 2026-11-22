"""CI/CD triage: why did this pipeline go red, and what should someone do next.

A red build blocks the whole loop: no merge, no deploy, no "Ready for Testing". The
question people ask first is always the same — *is it the code, the tests, or the
machine?* — and the answer is usually in three places of the build: which step failed,
the step's error issues, and the last lines of its log. This reads those (already
fetched, see ``ado.client``) and sorts the failure into one category with a next step.

Deterministic and conservative. Rules are checked in order, most specific first, and
"unknown" is a real answer: a guessed category sends someone to fix the wrong thing.

A failure is also marked **flaky** when the same commit later built green on the same
pipeline — nothing in the code changed, so the code was not the cause.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime

# (category, label, next step, patterns matched against step name + errors + log tail)
_RULES: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    ("timeout", "Hết thời gian", "Xem bước nào chạy lâu bất thường; tăng timeoutInMinutes "
     "chỉ khi bước đó thật sự cần lâu hơn.",
     (r"timed? ?out", r"exceeded the maximum execution time", r"canceled.*timeout",
      r"has exceeded")),
    ("auth", "Xác thực / secret", "Kiểm service connection, PAT, secret variable hoặc "
     "quyền của build identity — thường là hết hạn hoặc bị thu hồi.",
     (r"\b401\b", r"\b403\b", r"unauthori[sz]ed", r"authentication failed",
      r"access (is )?denied", r"permission denied", r"invalid (username|password|token)",
      r"secret.*(not found|missing)", r"az login", r"aadsts\d+")),
    ("agent", "Hạ tầng agent", "Không phải lỗi code: agent hết đĩa / mất kết nối / bị "
     "thu hồi. Chạy lại; lặp lại thì báo người quản lý agent pool.",
     (r"no space left on device", r"not enough space on the disk", r"lost communication",
      r"agent.*(went offline|was removed|disconnect)", r"the operation was canceled",
      r"could not find a pool", r"no agent found", r"out of memory|oom ?kill")),
    ("restore", "Tải gói phụ thuộc", "Lỗi feed / mạng khi restore gói: kiểm feed NuGet / "
     "npm / pip, phiên bản đã ghim có thật không, và lockfile có khớp không.",
     (r"npm err!", r"\bnu1\d{3}\b", r"unable to load the service index", r"eresolve",
      r"could not resolve dependency", r"no matching distribution", r"etimedout.*registry",
      r"restore failed", r"package .* not found", r"econnreset")),
    ("security", "Cổng bảo mật", "Bước quét bảo mật chặn build: mở kết quả quét, sửa hoặc "
     "ghi nhận ngoại lệ có lý do + hạn.",
     (r"gitleaks", r"semgrep", r"trivy", r"vulnerabilit(y|ies) found", r"secret detected",
      r"sonar.*quality gate")),
    ("build", "Lỗi biên dịch", "Lỗi trong code: sửa theo dòng lỗi đầu tiên — các lỗi sau "
     "thường là hệ quả của nó.",
     (r"error cs\d{4}", r"error ts\d+", r"build failed", r"compilation failed",
      r"syntaxerror", r"cannot find (module|symbol)", r"error msb\d{4}", r"\bng build\b.*error")),
    ("test", "Test trượt", "Mở test trượt đầu tiên. Nếu cùng commit chạy lại xanh thì là "
     "test chập chờn — xem bàn QC.",
     (r"\bfailed\s+\S+::", r"tests? failed", r"\d+ failed", r"assert(ion)?error",
      r"expected .* (to|but)", r"test run failed", r"failed tests?:", r"\bFAIL\b")),
    ("docker", "Docker / đẩy image", "Kiểm Dockerfile, quyền đẩy lên registry (ACR) và "
     "image nền có kéo được không.",
     (r"docker", r"dockerfile", r"failed to push", r"manifest unknown",
      r"denied: requested access to the resource", r"\bacr\b")),
)
LABELS = {cat: label for cat, label, _, _ in _RULES} | {"unknown": "Chưa rõ"}
_COMPILED = [(cat, hint, [re.compile(p, re.IGNORECASE) for p in pats])
             for cat, _, hint, pats in _RULES]
UNKNOWN_HINT = "Không khớp mẫu nào đã biết — mở log của bước lỗi."


@dataclass
class FailedBuild:
    build_id: int
    number: str
    pipeline: str
    branch: str
    commit: str
    requested_for: str
    finished: datetime | None
    url: str = ""
    failed_step: str = ""
    errors: list[str] = field(default_factory=list)
    log_tail: str = ""
    category: str = "unknown"
    hint: str = UNKNOWN_HINT
    evidence: str = ""          # the line the category was read from
    flaky: bool = False         # same commit built green later on this pipeline

    @property
    def label(self) -> str:
        return LABELS.get(self.category, "Chưa rõ")


def classify(step: str, errors: list[str], log_tail: str) -> tuple[str, str, str]:
    """``(category, next step, evidence line)``. Errors are read before the log:
    the step's own issue list is what the runner itself flagged."""
    sources = [step or "", *(errors or []), *(log_tail or "").splitlines()[-200:]]
    for cat, hint, patterns in _COMPILED:
        for text in sources:
            if any(p.search(text) for p in patterns):
                return cat, hint, text.strip()[:240]
    return "unknown", UNKNOWN_HINT, ""


def _dt(value: object) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def from_build(build: dict) -> FailedBuild:
    definition = build.get("definition") or {}
    who = build.get("requestedFor") or {}
    links = (build.get("_links") or {}).get("web") or {}
    return FailedBuild(
        build_id=int(build.get("id") or 0),
        number=str(build.get("buildNumber") or ""),
        pipeline=str(definition.get("name") or ""),
        branch=str(build.get("sourceBranch") or "").removeprefix("refs/heads/"),
        commit=str(build.get("sourceVersion") or ""),
        requested_for=str(who.get("displayName") or ""),
        finished=_dt(build.get("finishTime")),
        url=str(links.get("href") or ""),
    )


def failing_record(timeline: dict | None) -> tuple[str, list[str], int | None]:
    """The first failed Task in a build timeline: ``(name, error messages, log id)``."""
    records = (timeline or {}).get("records") or []
    tasks = [r for r in records
             if str(r.get("result") or "").lower() == "failed"
             and str(r.get("type") or "").lower() == "task"]
    if not tasks:
        tasks = [r for r in records if str(r.get("result") or "").lower() == "failed"]
    if not tasks:
        return "", [], None
    tasks.sort(key=lambda r: (str(r.get("startTime") or ""), int(r.get("order") or 0)))
    rec = tasks[0]
    errors = [str(i.get("message") or "").strip() for i in (rec.get("issues") or [])
              if str(i.get("type") or "").lower() == "error" and i.get("message")]
    log_id = (rec.get("log") or {}).get("id")
    return str(rec.get("name") or ""), errors[:10], log_id if isinstance(log_id, int) else None


def mark_flaky(failed: list[FailedBuild], green: list[dict]) -> None:
    """A failure is flaky when the same commit built green later on the same pipeline."""
    later: dict[tuple[str, str], list[datetime]] = {}
    for b in green:
        pipeline = str((b.get("definition") or {}).get("name") or "")
        key = (pipeline, str(b.get("sourceVersion") or ""))
        when = _dt(b.get("finishTime"))
        if key[1] and when:
            later.setdefault(key, []).append(when)
    for f in failed:
        times = later.get((f.pipeline, f.commit), [])
        f.flaky = bool(f.commit and f.finished and any(t > f.finished for t in times))


@dataclass
class CiReport:
    days: int
    status: str = "ok"
    reason: str = ""
    failed: list[FailedBuild] = field(default_factory=list)
    total_builds: int = 0

    @property
    def by_category(self) -> list[tuple[str, int]]:
        counts: dict[str, int] = {}
        for f in self.failed:
            counts[f.category] = counts.get(f.category, 0) + 1
        return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))

    @property
    def failure_rate(self) -> float | None:
        return len(self.failed) / self.total_builds if self.total_builds else None

    @property
    def flaky_count(self) -> int:
        return sum(1 for f in self.failed if f.flaky)
