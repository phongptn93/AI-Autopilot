"""Every form control on the dashboard must be reached by the ONE rule that styles them.

This is not a test about colour. It is a test about a selector that could silently miss
a control, which is a different and much older problem: the shared rule in `base.html`
used to be an ALLOWLIST of input types — `input[type=text], input[type=password], …`.
Two things slip through an allowlist without a word:

  * `<input name="assignee">` with no `type` at all. Valid HTML, defaults to text, and
    `input[type=text]` does not match it.
  * any type nobody thought to add — `datetime-local`, `tel`, `url`, `time`.

Both then fall through to the browser default, which on a dark dashboard is a pale box
sitting beside a correctly dark one. That is what shipped: the Planning filter's
Assignee field was a different colour from the State and Type dropdowns next to it,
thirteen fields on Workspaces had the same problem, and so did the schedule picker.

So the test reads the real selector out of `base.html`, works out what it would match,
and checks it against every control in every template. Reverting the rule to an
allowlist fails here rather than on somebody's screen.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

TEMPLATES = Path("ai_autopilot/dashboard/templates")

# Types that are NOT text-entry controls and are styled (or not) on their own terms.
NOT_TEXT_ENTRY = {
    "checkbox", "radio", "hidden", "submit", "button",
    "reset", "file", "range", "color", "image",
}


def _control_selector() -> str:
    """The selector of the shared control rule — the one that sets `background`."""
    css = (TEMPLATES / "base.html").read_text(encoding="utf-8")
    for block in re.finditer(r"([^{}]+)\{([^{}]*)\}", css):
        selector, body = block.group(1), block.group(2)
        if ("background: var(--bg-input)" in body and "select" in selector
                and "input" in selector and "textarea" in selector):
            return " ".join(selector.split())
    raise AssertionError("the shared form-control rule is no longer recognisable")


def _matches_a_bare_input(selector: str) -> bool:
    """Would this selector style `<input>` with no type attribute?"""
    # An exclusion list does (that is the point of writing it that way); an allowlist
    # of `input[type=...]` does not.
    if re.search(r"\binput:not\(", selector):
        return True
    return not re.search(r"\binput\[type=", selector)


def _allowed_types(selector: str) -> set[str] | None:
    """The types an ALLOWLIST selector covers, or None when it is an exclusion list."""
    if re.search(r"\binput:not\(", selector):
        return None
    return set(re.findall(r"input\[type=([\w-]+)\]", selector))


def _inputs():
    """(template name, tag, type or None) for every <input> the dashboard renders."""
    for path in sorted(TEMPLATES.glob("*.html")):
        for match in re.finditer(r"<input\b[^>]*>", path.read_text(encoding="utf-8")):
            tag = match.group(0)
            found = re.search(r"""type\s*=\s*["']?([\w-]+)""", tag)
            yield path.name, tag, (found.group(1) if found else None)


def test_every_text_entry_control_is_reached_by_the_shared_rule():
    selector = _control_selector()
    allowed = _allowed_types(selector)
    bare_ok = _matches_a_bare_input(selector)

    missed: list[str] = []
    for name, tag, kind in _inputs():
        if kind in NOT_TEXT_ENTRY:
            continue
        if kind is None:
            if not bare_ok:
                missed.append(f"{name}: no type= — {tag[:70]}")
        elif allowed is not None and kind not in allowed:
            missed.append(f"{name}: type={kind} — {tag[:70]}")

    assert not missed, (
        "these controls fall outside the shared style and will render with browser "
        "defaults:\n  " + "\n  ".join(missed)
    )


def test_the_shared_rule_is_written_as_an_exclusion_list():
    """Stated separately from the scan above, because the scan passes for the WRONG
    reason if someone both reverts to an allowlist and adds `type="text"` everywhere:
    green today, and the next typeless input reintroduces the bug."""
    selector = _control_selector()
    assert _allowed_types(selector) is None, (
        "the control rule is an allowlist of input types again — it will silently miss "
        "any <input> with no type attribute, and any type not listed"
    )
    for kind in ("checkbox", "radio"):
        assert f":not([type={kind}])" in selector


def test_the_controls_the_bug_was_reported_on_are_covered():
    """Named on purpose. These are the fields in the report: the Planning filter's
    Assignee box beside its two dropdowns, and its schedule picker."""
    planning = (TEMPLATES / "planning.html").read_text(encoding="utf-8")
    assignee = re.search(r'<input[^>]*name="assignee"[^>]*>', planning)
    assert assignee is not None
    selector = _control_selector()
    # Whatever the markup says — typed or not — the rule has to reach it.
    kind = re.search(r"""type\s*=\s*["']?([\w-]+)""", assignee.group(0))
    if kind is None:
        assert _matches_a_bare_input(selector)
    else:
        allowed = _allowed_types(selector)
        assert allowed is None or kind.group(1) in allowed

    assert "datetime-local" in planning        # the schedule picker still exists
    assert ":not([type=datetime-local])" not in selector


@pytest.mark.parametrize("page", ["planning.html", "workspaces.html"])
def test_pages_do_not_re_declare_the_control_background(page: str):
    """Each page re-declaring the look under its own class (.pf-input, .ws-input) is how
    four paddings, two radii and a missing focus ring shipped in the first place."""
    css_head = (TEMPLATES / page).read_text(encoding="utf-8").split("{% block")[0]
    for cls in (".pf-input", ".ws-input"):
        rule = re.search(re.escape(cls) + r"\s*\{([^}]*)\}", css_head)
        if rule:
            assert "background" not in rule.group(1), (
                f"{page}: {cls} sets its own background — that belongs in base.html"
            )
