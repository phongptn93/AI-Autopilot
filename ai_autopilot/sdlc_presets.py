"""SDLC starter presets — a known-good way of working, applied in one step.

A new install faced roughly forty SDLC-related settings across three pages, each
correct in isolation and wrong in combination: the loop switched on with nobody
told it is headless-only, a default profile of six stages under a budget of three
revisions, and an empty role map so finished work had nowhere to go. Every team that
got it working arrived at one of a small number of shapes. These are those shapes.

A preset is a plain ``{setting: value}`` patch plus, for the relay, a role chain
whose state names the operator confirms against THEIR board before anything is
written — ADO processes name states differently (Agile, Scrum, CMMI, custom), and a
preset that guessed would wire a door no item can ever stand in.

Pure: no I/O. The Roles page previews ``diff`` and applies ``patch``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class RoleStep:
    """One link of a relay chain, with the suggested states the form pre-fills."""

    name: str
    stages: tuple[str, ...]
    label: str
    waits_in: str
    done: str
    auto: bool = True


@dataclass(frozen=True)
class Preset:
    key: str
    icon: str
    title: str
    summary: str
    good_for: str
    settings: dict[str, Any]
    chain: tuple[RoleStep, ...] = ()
    caveats: tuple[str, ...] = field(default_factory=tuple)


PRESETS: dict[str, Preset] = {
    "safe": Preset(
        key="safe", icon="🟢", title="An toàn — bắt đầu",
        summary="Một vai Dev: code → tự review → PR nháp cho người duyệt. Bạn lái từng "
                "phiên trong console, không có gì tự merge.",
        good_for="Tuần đầu dùng autopilot, repo quan trọng, đội chưa quen.",
        settings={
            "autonomy_level": "assisted",
            "execution_mode": "interactive",
            "sdlc_loop_enabled": False,
            "sdlc_default_profile": "dev",
            "max_concurrent": 1,
            "use_worktrees": True,
            "auto_review_enabled": True,
            "pr_scoring_enabled": True,
            "dependency_scheduling_enabled": True,
        },
    ),
    "scrum": Preset(
        key="scrum", icon="🔵", title="Đội Scrum — chuyền vai BA → Dev → QC → Review",
        summary="Mỗi vai nhận item ở một state, làm xong chuyển sang state của vai kế "
                "tiếp. Review dừng lại cho người duyệt. Chạy được cả interactive lẫn "
                "headless.",
        good_for="Đội có BA, Dev, QC tách vai; board ADO có state cho từng chặng.",
        settings={
            "autonomy_level": "assisted",
            "sdlc_default_profile": "dev",
            "use_worktrees": True,
            "auto_review_enabled": True,
            "qc_create_test_case_items": True,
            "sdlc_max_iterations": 4,
        },
        chain=(
            RoleStep("ba", ("analyze",), "BA — phân tích & spec",
                     "Ready for Analysis", "Ready for Development"),
            RoleStep("dev", ("implement", "review", "pr"), "Dev — code & PR",
                     "Ready for Development, Rework Required", "Ready for Test"),
            RoleStep("qc", ("test",), "QC — test case & kiểm thử",
                     "Ready for Test", "Ready for Review"),
            RoleStep("review", ("review",), "Review — rà soát cuối",
                     "Ready for Review", "", auto=False),
        ),
        caveats=(
            "Tên state là gợi ý — đổi cho khớp board của bạn trước khi áp dụng.",
            "Review không tự chạy (auto tắt) và không chuyển state: người duyệt quyết.",
        ),
    ),
    "autonomous": Preset(
        key="autonomous", icon="🟣", title="Tự động hoàn toàn — vòng kín có cổng chất lượng",
        summary="Headless: analyze → design → implement → test → review → PR, mỗi chặng "
                "qua cổng chất lượng, tự sửa tối đa 5 vòng rồi mới gọi người. Vẫn chỉ "
                "tạo PR nháp.",
        good_for="Backlog việc nhỏ/đều, repo có test tốt, máy chạy không người trực.",
        settings={
            "autonomy_level": "assisted",
            "execution_mode": "headless",
            "sdlc_loop_enabled": True,
            "sdlc_default_profile": "full",
            "sdlc_max_iterations": 5,
            "max_concurrent": 2,
            "use_worktrees": True,
            "auto_review_enabled": True,
            "pr_scoring_enabled": True,
            "dependency_scheduling_enabled": True,
        },
        caveats=(
            "Headless = không có console để lái tay. Theo dõi ở In flight / History.",
            "Tốn token nhiều hơn: 6 chặng mỗi item, tối đa 5 vòng sửa.",
        ),
    ),
}


def roles_from_chain(preset: Preset, overrides: dict[str, dict[str, str]] | None = None,
                     ) -> dict[str, dict[str, Any]]:
    """The ``sdlc_roles`` map a relay preset writes, with the operator's state names.

    ``overrides`` is ``{role: {"waits_in": ..., "done": ...}}`` from the form. A blank
    override is honoured — blank ``done`` means "stop here", a real choice.
    """
    overrides = overrides or {}
    roles: dict[str, dict[str, Any]] = {}
    for step in preset.chain:
        mine = overrides.get(step.name, {})
        roles[step.name] = {
            "stages": list(step.stages),
            "waits_in": str(mine.get("waits_in", step.waits_in)).strip(),
            "done": str(mine.get("done", step.done)).strip(),
            "auto": step.auto,
        }
    return roles


def chain_problems(roles: dict[str, dict[str, Any]], trigger_states: list[str]) -> list[str]:
    """What would make the chain misbehave, in words — checked BEFORE it is written.

    Two roles behind one door cannot be told apart; a hand-off into a trigger state is
    picked up forever (#8526). Both are refused rather than written and warned about.
    """
    problems: list[str] = []
    seen: dict[str, str] = {}
    triggers = {(t or "").strip().lower() for t in trigger_states if (t or "").strip()}
    for name, role in roles.items():
        for door in [d.strip() for d in str(role.get("waits_in") or "").split(",") if d.strip()]:
            other = seen.get(door.lower())
            if other and other != name:
                problems.append(f"State '{door}' là cửa vào của cả '{other}' và '{name}'.")
            seen[door.lower()] = name
        done = str(role.get("done") or "").strip()
        if done and done.lower() in triggers:
            problems.append(
                f"'{name}' xong chuyển sang '{done}' — state này đang là Trigger state, "
                "item sẽ bị nhận lại mãi. Bỏ nó khỏi Trigger states trước."
            )
    return problems


def diff(preset: Preset, config: Any) -> list[dict[str, Any]]:
    """Every setting the preset would change: ``[{key, now, then}]``, unchanged omitted.

    The preview is the point. Applying a preset blind is how a team lost a carefully
    tuned value without noticing; this list is what the confirm button stands on.
    """
    rows = []
    for key, value in preset.settings.items():
        now = getattr(config, key, None)
        if now != value:
            rows.append({"key": key, "now": now, "then": value})
    return rows
