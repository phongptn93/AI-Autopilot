#!/usr/bin/env python3
"""Tinh diem cong (gate score) cho quy trinh SDLC thoi dai AI.

Chi dung thu vien chuan. Doc ``gate-scorecard.json`` (cau hinh) va mot file input
(trang thai tung tieu chi), in ra diem 0-100, quyet dinh va ly do.

    python gate_score.py gate-scorecard.json examples/g3-yellow-review.json
    python gate_score.py gate-scorecard.json input.json --json

Quy tac (khop voi ``ai_autopilot/execution/pr_scorer.py``: tin hieu chua biet cham
trung tinh-thap, khong cham diem toi da):

* pass=1.0, partial=0.5, unknown=0.5, fail=0.0, na = bo khoi tinh (trong so duoc chuan hoa lai).
* pass/partial ma khong co ``evidence`` -> ha xuong unknown (khong co bang chung != chat luong cao).
* Tieu chi vang mat trong input = unknown.
* Hard-fail: tieu chi ``fail`` co ``hard`` chua tang hien tai -> BLOCK bất kể điểm.
* Quyet dinh: diem >= auto_min -> PASS; >= review_min -> REVIEW; con lai BLOCK.
  Neu PASS ma cong yeu cau nguoi: confirm -> CONFIRM, full -> REVIEW.

Diem tra loi "lam tot den dau"; quyet dinh tra loi "co phai nguoi xem truoc khi di tiep".
Hai thu do khac nhau nen hard-fail/ yeu cau nguoi luon thang diem.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PASS, CONFIRM, REVIEW, BLOCK = "PASS", "CONFIRM", "REVIEW", "BLOCK"
_VALID = {"pass", "partial", "fail", "unknown", "na"}


@dataclass
class GateResult:
    gate: str
    tier: str
    score: int
    outcome: str
    components: dict[str, float] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)
    hard_fails: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "gate": self.gate, "tier": self.tier, "score": self.score,
            "outcome": self.outcome, "components": self.components,
            "hard_fails": self.hard_fails, "reasons": self.reasons,
        }


def load_scorecard(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def score_gate(scorecard: dict[str, Any], gate: str, tier: str,
               criteria: dict[str, dict[str, Any]] | None) -> GateResult:
    """Cham mot cong. ``criteria`` = {id: {"status": ..., "evidence": ...}}."""
    if gate not in scorecard["gates"]:
        raise ValueError(f"Cổng không tồn tại: {gate}")
    if tier not in scorecard["tiers"]:
        raise ValueError(f"Tầng rủi ro không tồn tại: {tier}")
    values = scorecard["status_values"]
    tcfg = scorecard["tiers"][tier]
    given = criteria or {}

    applicable = [c for c in scorecard["gates"][gate]["criteria"]
                  if tier in c.get("applies", list(scorecard["tiers"]))]
    reasons: list[str] = []
    hard_fails: list[str] = []
    earned: dict[str, tuple[int, float]] = {}   # id -> (weight, value)

    for c in applicable:
        raw = given.get(c["id"], {}) or {}
        status = str(raw.get("status", "unknown")).lower()
        if status not in _VALID:
            raise ValueError(f"Trạng thái không hợp lệ cho {c['id']}: {status}")
        evidence = str(raw.get("evidence", "") or "").strip()
        if status in ("pass", "partial") and not evidence:
            reasons.append(f"{c['id']}: thiếu bằng chứng -> tính là unknown")
            status = "unknown"
        if status == "na":
            continue
        if status == "unknown" and c["id"] not in given:
            reasons.append(f"{c['id']}: chưa có dữ liệu -> unknown")
        if status == "fail":
            reasons.append(f"{c['id']}: KHÔNG ĐẠT - {c['label']}")
            if tier in c.get("hard", []):
                hard_fails.append(c["id"])
        earned[c["id"]] = (c["weight"], values[status])

    total_w = sum(w for w, _ in earned.values())
    score = round(100 * sum(w * v for w, v in earned.values()) / total_w) if total_w else 0
    score = max(0, min(100, score))

    if hard_fails:
        outcome = BLOCK
        reasons.append("hard-fail: " + ", ".join(hard_fails) + " -> BLOCK bất kể điểm")
    elif score >= tcfg["auto_min"]:
        need = tcfg["human"].get(gate, "full")
        outcome = {"none": PASS, "confirm": CONFIRM}.get(need, REVIEW)
        if need == "confirm":
            reasons.append("cổng yêu cầu người xác nhận nhanh")
        elif need == "full":
            reasons.append("cổng bắt buộc có người xem đầy đủ ở tầng này")
    elif score >= tcfg["review_min"]:
        outcome = REVIEW
        reasons.append(f"điểm {score} nằm giữa {tcfg['review_min']} và {tcfg['auto_min']}: người phải xem")
    else:
        outcome = BLOCK
        reasons.append(f"điểm {score} < {tcfg['review_min']}: trả lại")

    comp = {cid: round(w * v, 1) for cid, (w, v) in earned.items()}
    return GateResult(gate, tier, score, outcome, comp, reasons, hard_fails)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("scorecard")
    ap.add_argument("input", help='JSON: {"gate":"G3","tier":"yellow","criteria":{"id":{"status":"pass","evidence":"..."}}}')
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    card = load_scorecard(args.scorecard)
    data = json.loads(Path(args.input).read_text(encoding="utf-8"))
    res = score_gate(card, data["gate"], data["tier"], data.get("criteria"))
    if args.json:
        print(json.dumps(res.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(f"{res.gate} | tầng {res.tier} | điểm {res.score}/100 | quyết định: {res.outcome}")
        for r in res.reasons:
            print(f"  - {r}")
    return {PASS: 0, CONFIRM: 0, REVIEW: 1, BLOCK: 2}[res.outcome]


if __name__ == "__main__":
    sys.exit(main())
