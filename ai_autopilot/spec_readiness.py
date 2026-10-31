"""Spec readiness: is this specification precise enough to hand to an agent?

An agent builds exactly what it reads. A spec that says "the screen should be fast" or
lists acceptance criteria nobody can check turns into code that is wrong quickly — and
the mistake surfaces at UAT, not at the moment someone could have fixed the sentence.
Spec drift reports the gap AFTER the code exists; this is the check BEFORE it does.

What it looks for, and why each one matters:

* **No acceptance criteria** — nothing to verify the work against; blocks everything.
* **Criteria nobody can check** — an AC is useful only if a person (or a test) can say
  "yes" or "no" to it: Given/When/Then, or a concrete observable result.
* **Vague words** in a requirement or criterion ("fast", "user-friendly", "hợp lý").
* **Orphan requirements** — an FR that no criterion refers to will never be tested.
* **Open questions** — TBD / TODO / unchecked assumptions left in the document.
* **Missing security section** when the text clearly touches permissions, money,
  credentials or personal data.
* **No edge cases** at all.

Pure and dependency-free, like :mod:`ai_autopilot.risk`: text in, verdict out. It judges
the *document*, not the product; it can say a sentence is uncheckable, never that a
requirement is wrong. The heuristics are deliberately conservative about what they
flag, because a checker that cries wolf gets ignored.
"""

from __future__ import annotations

import html as html_mod
import re
from dataclasses import dataclass, field

CRITICAL = "critical"
WARNING = "warning"
INFO = "info"

READY = "ready"
NEEDS_WORK = "needs_work"
NOT_READY = "not_ready"

VERDICT_LABELS: dict[str, tuple[str, str]] = {
    READY: ("✅", "Sẵn sàng giao agent"),
    NEEDS_WORK: ("🟡", "Cần chỉnh trước khi giao"),
    NOT_READY: ("⛔", "Chưa sẵn sàng"),
}

#: Prefix of the work-item comment, stable like ``spec_drift.DRIFT_PREFIX`` so a board
#: query or a "already posted" check can match it across releases.
COMMENT_PREFIX = "📋 SPEC-READINESS"

# Words that make a requirement impossible to verify. Curated, not exhaustive: each
# entry is something people actually write and nobody can test. Matched as whole words
# (``(?<!\w)…(?!\w)`` so Vietnamese diacritics do not break the boundary).
VAGUE_TERMS: tuple[str, ...] = (
    "nhanh chóng", "nhanh", "thân thiện", "dễ dùng", "dễ sử dụng", "hợp lý", "phù hợp",
    "tối ưu", "linh hoạt", "mượt mà", "mượt", "v.v", "vân vân", "sẽ xác định sau",
    "càng nhiều càng tốt", "khi cần thiết", "nếu cần",
    "fast", "quick", "quickly", "user-friendly", "easy to use", "intuitive", "appropriate",
    "reasonable", "as needed", "flexible", "etc", "and so on",
)
_VAGUE = re.compile(
    r"(?<!\w)(" + "|".join(re.escape(t) for t in sorted(VAGUE_TERMS, key=len, reverse=True))
    + r")(?!\w)", re.IGNORECASE)

# Keywords that mean "this feature touches something security-relevant". Two DISTINCT
# hits are required before the security section is demanded, so one passing "role" in a
# sentence about layout does not raise a flag.
_SENSITIVE = re.compile(
    r"(?<!\w)(phân quyền|quyền truy cập|đăng nhập|mật khẩu|token|thanh toán|tiền|lương|"
    r"hoá đơn|hóa đơn|dữ liệu cá nhân|cccd|cmnd|số tài khoản|"
    r"permission|authoriz\w*|authenticat\w*|password|credential|payment|salary|invoice|"
    r"personal data|pii|admin)(?!\w)", re.IGNORECASE)

_OPEN_MARK = re.compile(
    r"(?<!\w)(tbd|tba|todo|chưa chốt|chưa xác định|cần xác nhận|chờ xác nhận|cần hỏi)(?!\w)"
    r"|\?\?", re.IGNORECASE)

_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$")
_BOLD_HEADING = re.compile(r"^\s*\*\*([^*]{3,80})\*\*\s*:?\s*$")
_LIST_ITEM = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(?:\[[ xX]\]\s+)?(.*\S)\s*$")
_UNCHECKED = re.compile(r"^\s*[-*+]\s+\[ \]\s+(.*\S)")
_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
_TABLE_SEP = re.compile(r"^\s*\|?[\s:|-]+\|?\s*$")
_AC_ID = re.compile(r"^\W{0,4}AC[-_ ]?(\d+)\b", re.IGNORECASE)
_FR_ID_START = re.compile(r"^\W{0,4}FR[-_ ]?(\d+)\b", re.IGNORECASE)
_FR_REF = re.compile(r"(?<!\w)FR[-_ ]?(\d+)(?!\w)", re.IGNORECASE)
_AC_PREFIX = re.compile(r"^\W{0,4}AC[-_ ]?\d+(?!\w)[\s:.)\-]*", re.IGNORECASE)

_SEC_AC = re.compile(r"acceptance|tiêu chí (?:chấp nhận|nghiệm thu)|(?<!\w)ac(?!\w)", re.I)
_SEC_FR = re.compile(r"functional|yêu cầu chức năng|(?<!\w)fr(?!\w)|chức năng", re.I)
_SEC_EDGE = re.compile(r"edge|trường hợp|ngoại lệ|bất thường|lỗi", re.I)
_SEC_SECURITY = re.compile(r"security|bảo mật|phân quyền|permission|authoriz|quyền", re.I)
_SEC_QUESTIONS = re.compile(r"câu hỏi|open question|giả định|assumption|chưa chốt", re.I)

# What makes one acceptance criterion checkable.
_GWT_EN = re.compile(r"(?<!\w)(given|when|then)(?!\w)", re.IGNORECASE)
_GWT_VI = re.compile(r"(?<!\w)(cho|khi|thì|nếu)(?!\w)", re.IGNORECASE)
_OBSERVABLE = re.compile(
    r"(?<!\w)(hiển thị|hiện|trả về|báo lỗi|thông báo|từ chối|không cho phép|cho phép|"
    r"lưu|xoá|xóa|gửi|chuyển (?:sang|đến|hướng)|"
    r"returns?|shows?|displays?|rejects?|redirects?|saves?|deletes?|sends?|"
    r"error|status|http|\b[2-5]\d\d\b)(?!\w)", re.IGNORECASE)
_NUMBER = re.compile(r"\d")

_MAX_VAGUE_REPORTED = 8
_MAX_LIST = 8


@dataclass
class Finding:
    code: str
    severity: str
    message: str
    line: int = 0          # 1-based; 0 = the document as a whole
    snippet: str = ""


@dataclass
class Readiness:
    score: int
    verdict: str
    findings: list[Finding] = field(default_factory=list)
    stats: dict[str, int] = field(default_factory=dict)

    @property
    def ready(self) -> bool:
        return self.verdict == READY

    @property
    def critical(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == CRITICAL]

    @property
    def icon(self) -> str:
        return VERDICT_LABELS[self.verdict][0]

    @property
    def label(self) -> str:
        return VERDICT_LABELS[self.verdict][1]


def plain_text_from_html(markup: str) -> str:
    """Tags out, block boundaries kept as line breaks — enough for an HTML spec."""
    text = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", markup or "")
    text = re.sub(r"(?i)<\s*(br|/p|/div|/li|/tr|/h[1-6]|/table|/ul|/ol)\s*/?>", "\n", text)
    text = re.sub(r"(?i)<\s*li\b[^>]*>", "- ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_mod.unescape(text)
    return "\n".join(re.sub(r"[ \t]+", " ", ln).strip() for ln in text.splitlines())


def _clip(s: str, n: int = 140) -> str:
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _is_verifiable(text: str) -> bool:
    """One criterion is checkable when it reads as Given/When/Then or states a concrete
    observable outcome. Deliberately permissive: the aim is to catch "the page should be
    nice", not to police phrasing."""
    t = _AC_PREFIX.sub("", text or "")
    t = _FR_REF.sub("", t)
    if len({m.group(1).lower() for m in _GWT_EN.finditer(t)}) >= 2:
        return True
    vi = {m.group(1).lower() for m in _GWT_VI.finditer(t)}
    if ("khi" in vi or "nếu" in vi) and "thì" in vi:
        return True
    if _OBSERVABLE.search(t):
        return True
    return bool(_NUMBER.search(t) and len(t.split()) >= 4)


@dataclass
class AcItem:
    """One acceptance criterion as found in the text."""

    line: int
    id: str          # "AC-03" when the text names it, "" when it is an unnamed list item
    text: str


@dataclass
class _Scan:
    ac_items: list[AcItem]
    fr_defined: dict[str, tuple[int, str]]
    fr_lines: list[tuple[int, str]]
    open_marks: list[tuple[int, str]]
    has_security_section: bool
    has_edge_section: bool


def _scan(text: str) -> _Scan:
    """One pass over the document: sections, criteria, requirements, open markers."""
    lines = (text or "").splitlines()
    n = len(lines)

    section = ""
    in_code = False
    ac_items: list[AcItem] = []
    fr_defined: dict[str, tuple[int, str]] = {}        # FR number -> (line, text)
    fr_lines: list[tuple[int, str]] = []
    open_marks: list[tuple[int, str]] = []
    has_security_section = False
    has_edge_section = False

    def is_header_row(i: int) -> bool:
        return i + 1 < n and bool(_TABLE_SEP.match(lines[i + 1])) and "-" in lines[i + 1]

    for i, raw in enumerate(lines):
        line = raw.rstrip()
        if line.strip().startswith("```"):
            in_code = not in_code
            continue
        if in_code or not line.strip():
            continue
        head = _HEADING.match(line) or _BOLD_HEADING.match(line)
        if head:
            section = head.group(1).strip()
            has_security_section = has_security_section or bool(_SEC_SECURITY.search(section))
            has_edge_section = has_edge_section or bool(_SEC_EDGE.search(section))
            continue
        if _TABLE_SEP.match(line) and "-" in line:
            continue

        in_ac = bool(_SEC_AC.search(section))
        in_fr = bool(_SEC_FR.search(section)) and not in_ac
        in_questions = bool(_SEC_QUESTIONS.search(section))

        body = line
        cells: list[str] = []
        if _TABLE_ROW.match(line):
            if is_header_row(i):
                continue
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            body = " ".join(c for c in cells if c)
            first = cells[0] if cells else ""
        else:
            li = _LIST_ITEM.match(line)
            if li:
                body = li.group(1)
            first = body

        m_ac = _AC_ID.match(first) or _AC_ID.match(body)
        if m_ac or (in_ac and (cells or _LIST_ITEM.match(line))):
            ac_items.append(AcItem(i + 1, f"AC-{m_ac.group(1)}" if m_ac else "", body))
        m_fr = _FR_ID_START.match(first) or _FR_ID_START.match(body)
        if m_fr and not m_ac:
            fr_defined.setdefault(m_fr.group(1).lstrip("0") or "0", (i + 1, body))
            fr_lines.append((i + 1, body))
        elif in_fr and (cells or _LIST_ITEM.match(line)):
            fr_lines.append((i + 1, body))

        if _OPEN_MARK.search(body) or (in_questions and _UNCHECKED.match(line)):
            open_marks.append((i + 1, body))

    return _Scan(ac_items, fr_defined, fr_lines, open_marks,
                 has_security_section, has_edge_section)


def extract_acs(text: str) -> list[AcItem]:
    """The acceptance criteria of a spec, in document order. Shared by everything that
    needs to talk about "AC-03" the way the readiness check does."""
    return _scan(text).ac_items


def check(text: str, *, ready_min: int = 80) -> Readiness:
    """Judge a specification's text. ``ready_min`` is the score a spec with no critical
    finding must reach to be called ready."""
    scan = _scan(text)
    ac_items = scan.ac_items
    fr_defined, fr_lines, open_marks = scan.fr_defined, scan.fr_lines, scan.open_marks
    has_security_section, has_edge_section = scan.has_security_section, scan.has_edge_section
    findings: list[Finding] = []

    # ── no acceptance criteria at all ──────────────────────────────────────────────
    if not ac_items:
        findings.append(Finding(
            "no_ac", CRITICAL,
            "Không tìm thấy tiêu chí chấp nhận (AC) nào — không có gì để kiểm chứng "
            "bản làm ra. Thêm mục “Acceptance Criteria” hoặc các dòng AC-01, AC-02…"))

    # ── criteria nobody can check ──────────────────────────────────────────────────
    bad_ac = [a for a in ac_items if not _is_verifiable(a.text)]
    for a in bad_ac[:_MAX_LIST]:
        findings.append(Finding(
            "ac_not_verifiable", WARNING,
            f"{a.id or 'Tiêu chí'} khó kiểm chứng: chưa có Given/When/Then hay kết quả "
            "quan sát được (hiển thị gì, trả về gì, báo lỗi gì).",
            a.line, _clip(a.text)))

    # ── vague words in a requirement or a criterion ────────────────────────────────
    seen_vague: set[int] = set()
    vague_total = 0
    for ln, t in [(a.line, a.text) for a in ac_items] + fr_lines:
        if ln in seen_vague:
            continue
        hits = sorted({m.group(1).lower() for m in _VAGUE.finditer(t)})
        if hits:
            seen_vague.add(ln)
            vague_total += 1
            if vague_total <= _MAX_VAGUE_REPORTED:
                findings.append(Finding(
                    "vague_term", WARNING,
                    "Từ mơ hồ không kiểm chứng được: " + ", ".join(f"“{h}”" for h in hits)
                    + ". Thay bằng con số hoặc hành vi cụ thể.", ln, _clip(t)))

    # ── requirements no criterion refers to ────────────────────────────────────────
    ac_text = " ".join(a.text for a in ac_items)
    referenced = {m.group(1).lstrip("0") or "0" for m in _FR_REF.finditer(ac_text)}
    orphans: list[str] = []
    if fr_defined and referenced:
        for num, (ln, t) in fr_defined.items():
            if num not in referenced:
                orphans.append(num)
                if len(orphans) <= _MAX_LIST:
                    findings.append(Finding(
                        "orphan_fr", WARNING,
                        f"FR-{num} không có AC nào trỏ tới — sẽ không bao giờ được kiểm "
                        "thử. Thêm AC hoặc ghi rõ FR này được kiểm bằng AC nào.",
                        ln, _clip(t)))
    elif fr_defined and ac_items and not referenced:
        findings.append(Finding(
            "ac_not_traceable", INFO,
            "Các AC chưa trỏ về FR nào (ví dụ “AC-01 → FR-01”), nên chưa truy vết được "
            "yêu cầu nào đã có tiêu chí."))

    # ── open questions ─────────────────────────────────────────────────────────────
    for ln, t in open_marks[:_MAX_LIST]:
        findings.append(Finding(
            "open_question", WARNING,
            "Còn câu hỏi/giả định chưa chốt — agent sẽ tự đoán chỗ này.", ln, _clip(t)))

    # ── security ───────────────────────────────────────────────────────────────────
    sensitive = {m.group(1).lower() for m in _SENSITIVE.finditer(text or "")}
    if len(sensitive) >= 2 and not has_security_section:
        findings.append(Finding(
            "no_security", WARNING,
            "Spec đụng tới " + ", ".join(sorted(sensitive)[:4])
            + " nhưng không có mục Bảo mật/Phân quyền (ai được làm gì, dữ liệu nhạy cảm nào)."))

    # ── edge cases ─────────────────────────────────────────────────────────────────
    if ac_items and not has_edge_section:
        findings.append(Finding(
            "no_edge_cases", INFO,
            "Chưa có mục Edge case / Trường hợp ngoại lệ (dữ liệu rỗng, trùng, quá lớn, "
            "mất quyền, lỗi mạng…)."))

    # ── score ──────────────────────────────────────────────────────────────────────
    score = 100.0
    if not ac_items:
        score -= 50
    elif bad_ac:
        score -= 35.0 * len(bad_ac) / len(ac_items)
    score -= min(vague_total * 3, 15)
    score -= min(len(orphans) * 4, 16)
    score -= min(len(open_marks) * 4, 20)
    if any(f.code == "no_security" for f in findings):
        score -= 10
    if any(f.code == "no_edge_cases" for f in findings):
        score -= 5
    final = max(0, min(100, round(score)))

    if any(f.severity == CRITICAL for f in findings) or final < 50:
        verdict = NOT_READY
    elif final >= ready_min:
        verdict = READY
    else:
        verdict = NEEDS_WORK

    stats = {
        "ac": len(ac_items),
        "ac_verifiable": len(ac_items) - len(bad_ac),
        "fr": len(fr_defined),
        "fr_without_ac": len(orphans),
        "vague": vague_total,
        "open_questions": len(open_marks),
    }
    order = {CRITICAL: 0, WARNING: 1, INFO: 2}
    findings.sort(key=lambda f: (order[f.severity], f.line))
    return Readiness(score=final, verdict=verdict, findings=findings, stats=stats)


def render_markdown(r: Readiness, title: str = "") -> str:
    """The review a BA can paste or download: verdict first, then what to fix."""
    head = f"# {r.icon} Độ sẵn sàng của spec" + (f" — {title}" if title else "")
    out = [head, "", f"**{r.label}** · điểm **{r.score}/100**", ""]
    s = r.stats
    out.append(f"- AC: {s.get('ac_verifiable', 0)}/{s.get('ac', 0)} kiểm chứng được")
    out.append(f"- FR chưa có AC: {s.get('fr_without_ac', 0)} · từ mơ hồ: {s.get('vague', 0)}"
               f" · câu hỏi mở: {s.get('open_questions', 0)}")
    if r.findings:
        out += ["", "| Mức | Dòng | Vấn đề |", "|---|---|---|"]
        for f in r.findings:
            sev = {"critical": "⛔", "warning": "🟡", "info": "ℹ️"}[f.severity]
            where = str(f.line) if f.line else "—"
            msg = f.message.replace("|", "\\|")
            if f.snippet:
                msg += f" — _{f.snippet.replace('|', chr(92) + '|')}_"
            out.append(f"| {sev} | {where} | {msg} |")
    else:
        out += ["", "Không có vấn đề nào."]
    return "\n".join(out) + "\n"


def render_comment(r: Readiness, title: str = "") -> str:
    """The work-item comment (HTML). Escaped: findings quote the spec's own text."""
    esc = html_mod.escape
    rows = "".join(
        f"<li>{ {'critical': '⛔', 'warning': '🟡', 'info': 'ℹ️'}[f.severity] } "
        f"{esc(f.message)}"
        + (f" <i>(dòng {f.line}: {esc(f.snippet)})</i>" if f.line and f.snippet else "")
        + "</li>"
        for f in r.findings[:12]
    )
    more = (f"<p>… và {len(r.findings) - 12} mục nữa.</p>" if len(r.findings) > 12 else "")
    t = f" — {esc(title)}" if title else ""
    return (
        f"<div><b>{COMMENT_PREFIX}</b>{t}<br/>"
        f"{r.icon} <b>{esc(r.label)}</b> · điểm <b>{r.score}/100</b> "
        f"· AC kiểm chứng được {r.stats.get('ac_verifiable', 0)}/{r.stats.get('ac', 0)}"
        + (f"<ul>{rows}</ul>{more}" if r.findings else "<p>Không có vấn đề nào.</p>")
        + "</div>"
    )
