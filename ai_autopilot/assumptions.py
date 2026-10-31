"""Assumption ledger: the questions a spec leaves open, and who answered them.

A spec is never finished the day it is written. It carries "TBD", an unchecked "is the
period capped?", an "assumption: the customer pays monthly". An autonomous agent does
not stop to ask about any of them — it picks something, and spec drift reports it AFTER
the code exists. This module turns those open points into a ledger a BA can work
through BEFORE the work is handed on: every question has a stable key, an answer is
recorded against that key, and a question that has an answer is no longer open.

Keys come from the question's own text, not from its line number, so an answered
question stays answered when the spec is re-ordered or an unrelated paragraph is added
above it — and becomes open again (a new key) when its wording changes materially,
which is the correct outcome: the customer answered a different question.

Pure and dependency-free, in the same style as :mod:`ai_autopilot.spec_readiness`
(whose section vocabulary it shares so the two never disagree about what is "open").
"""

from __future__ import annotations

import hashlib
import html as html_mod
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from ai_autopilot import spec_readiness as sr

#: Prefix of the work-item comment recording an answer. Stable, like the drift prefixes.
COMMENT_PREFIX = "💬 SPEC-ANSWER"

OPEN_QUESTION = "question"      # something nobody has decided yet
ASSUMPTION = "assumption"       # something we are proceeding on and want confirmed

_SEC_ASSUMPTION = re.compile(r"giả định|assumption", re.IGNORECASE)
_SEC_QUESTION = re.compile(r"câu hỏi|open question|chưa chốt", re.IGNORECASE)
_CHECKED = re.compile(r"^\s*[-*+]\s+\[[xX]\]\s+")
_STRIP_BOX = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(?:\[[ xX]\]\s+)?")
_MAX_QUESTIONS = 200


@dataclass(frozen=True)
class Question:
    key: str
    text: str
    kind: str            # OPEN_QUESTION | ASSUMPTION
    line: int            # 1-based line in the source text
    section: str = ""


@dataclass
class Ledger:
    open: list[Question] = field(default_factory=list)
    answered: list[tuple[Question, Mapping[str, str]]] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.open) + len(self.answered)

    @property
    def open_count(self) -> int:
        return len(self.open)


def _normalise(text: str) -> str:
    return " ".join((text or "").lower().split())


def key_for(text: str) -> str:
    """Stable identity of a question: a hash of its normalised wording."""
    return hashlib.sha1(_normalise(text).encode("utf-8")).hexdigest()[:12]


def extract(spec_text: str) -> list[Question]:
    """The open questions and the assumptions of a spec, in document order.

    A line counts when it sits in a "Câu hỏi mở"/"Giả định" section (unless it is a
    ticked checkbox — that one is settled), or carries a marker (TBD, "chưa chốt",
    "??") anywhere in the document. Code fences are ignored.
    """
    questions: list[Question] = []
    seen: set[str] = set()
    section = ""
    in_code = False
    for i, raw in enumerate((spec_text or "").splitlines(), start=1):
        line = raw.rstrip()
        if line.strip().startswith("```"):
            in_code = not in_code
            continue
        if in_code or not line.strip():
            continue
        head = sr._HEADING.match(line) or sr._BOLD_HEADING.match(line)
        if head:
            section = head.group(1).strip()
            continue
        in_q = bool(_SEC_QUESTION.search(section))
        in_a = bool(_SEC_ASSUMPTION.search(section))
        is_item = bool(sr._LIST_ITEM.match(line))
        marked = bool(sr._OPEN_MARK.search(line))
        if _CHECKED.match(line):
            continue
        if not ((is_item and (in_q or in_a)) or marked):
            continue
        body = _STRIP_BOX.sub("", line).strip() if is_item else line.strip()
        if not body:
            continue
        key = key_for(body)
        if key in seen:
            continue
        seen.add(key)
        kind = ASSUMPTION if (in_a and not marked) else OPEN_QUESTION
        questions.append(Question(key, body, kind, i, section))
        if len(questions) >= _MAX_QUESTIONS:
            break
    return questions


def ledger(questions: Sequence[Question],
           answers: Mapping[str, Mapping[str, str]]) -> Ledger:
    """Split questions into open and answered. ``answers`` maps a key to the recorded
    answer (``{"answer", "answered_by", ...}``); an answer with no text is not one."""
    out = Ledger()
    for q in questions:
        a = answers.get(q.key)
        if a and str(a.get("answer", "")).strip():
            out.answered.append((q, a))
        else:
            out.open.append(q)
    return out


def render_answer_comment(title: str, question: Question, answer: str, by: str = "") -> str:
    """The work-item comment that records an answer. Escaped: both the question and the
    answer are text somebody typed."""
    esc = html_mod.escape
    who = f" — {esc(by)}" if by else ""
    label = "Giả định" if question.kind == ASSUMPTION else "Câu hỏi"
    t = f" — {esc(title)}" if title else ""
    return (
        f"<div><b>{COMMENT_PREFIX}</b>{t}{who}<br/>"
        f"<b>{label}:</b> {esc(question.text)}<br/>"
        f"<b>Trả lời:</b> {esc(answer.strip())}</div>"
    )
