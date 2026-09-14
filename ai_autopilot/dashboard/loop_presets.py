"""Ready-made report loops the Loops page can add in one click.

Presets, not defaults: nothing here is scheduled until somebody adds it. What they are
for is the blank page — "an agent that runs on a schedule" is easy to want and awkward
to write, because the useful part is the prompt and the choice of sub-agents, and both
have to be right before the first run tells you anything.

Every cadence deliberately avoids :00 and :30. Those are where every scheduler in the
world already fires, and an audit that starts while the nightly build, the backup and
three other loops are starting is an audit that will be blamed for the machine being
slow.
"""

from __future__ import annotations

PRESETS: list[dict] = [
    {
        "key": "code-review-daily",
        "label": "Code review hằng ngày",
        "why": "Quét thay đổi trong ngày, báo lỗi logic và rủi ro bảo mật trước khi kịp đi xa.",
        "loop": {
            "name": "code-review-daily",
            "mode": "report",
            "cron": "7 18 * * 1-5",
            "agents": ["agent-pr-reviewer", "agent-security-reviewer"],
            "prompt": (
                "Review every commit made to this repository in the last 24 hours "
                "(`git log --since=\"24 hours ago\"`, and the diff of those commits "
                "against their parents). Judge only what those commits changed — do not "
                "review the whole repository. Look for correctness bugs, missing error "
                "handling, data-loss risks, and security problems in the changed lines. "
                "Say what breaks and under which input, not what could be prettier."
            ),
        },
    },
    {
        "key": "security-audit-weekly",
        "label": "Security audit OWASP hằng tuần",
        "why": "Rà toàn repo theo OWASP API/Web Top 10 — sâu hơn review ngày, chạy cuối tuần.",
        "loop": {
            "name": "security-audit-weekly",
            "mode": "report",
            "cron": "23 2 * * 6",
            "agents": ["agent-security-reviewer"],
            "prompt": (
                "Run a deep security audit of this repository against the OWASP API "
                "Security Top 10 and the OWASP Top 10 for web. Prioritise authentication "
                "and authorisation gaps, injection, secrets committed to the tree, unsafe "
                "deserialisation, and endpoints missing an access check. For each finding "
                "name the file and line and say what an attacker gets."
            ),
        },
    },
    {
        "key": "performance-sweep-weekly",
        "label": "Performance sweep hằng tuần",
        "why": "Soi truy vấn N+1, index thiếu, bundle phình — trước khi người dùng báo chậm.",
        "loop": {
            "name": "performance-sweep-weekly",
            "mode": "report",
            "cron": "41 3 * * 6",
            "agents": ["agent-performance-investigator"],
            "prompt": (
                "Sweep this repository for performance problems that are visible in the "
                "code: N+1 query patterns, queries with no supporting index, unbounded "
                "result sets, work done inside loops that could be batched, and front-end "
                "bundles pulling in modules they do not use. Report only what you can "
                "point at; do not speculate about load you cannot see."
            ),
        },
    },
    {
        "key": "spec-drift-weekly",
        "label": "Spec drift & CI triage hằng tuần",
        "why": "Đối chiếu spec với code thật, và gom lỗi pipeline đỏ trong tuần.",
        "loop": {
            "name": "spec-drift-weekly",
            "mode": "report",
            "cron": "13 7 * * 1",
            "agents": ["agent-spec-updater", "agent-ci-triage"],
            "prompt": (
                "Two questions about the last week. First: where has the code drifted "
                "from the specs that describe it — behaviour that changed without the "
                "spec changing, or a spec describing something the code no longer does? "
                "Second: which CI failures recurred, and what is the shared cause? "
                "Report the drift and the failure patterns; do not edit any spec."
            ),
        },
    },
]


def by_key(key: str) -> dict | None:
    """One preset's loop definition, or None."""
    for preset in PRESETS:
        if preset["key"] == key:
            return dict(preset["loop"])
    return None
