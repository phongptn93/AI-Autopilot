"""Claude Code's own transcript, read as a signal about a live interactive session.

A headless run streams its events back through the SDK, so the control plane always
knows what it is doing and what it spent. An **interactive** run tells us neither: it
is a separate ``claude`` console launched with ``--remote-control``, and everything it
does happens out of process. Two holes followed, and interactive is the DEFAULT mode:

- **"Is it still going?"** had no answer, so a session that hung had no ceiling. The
  only thing that ever ended one was its own result file, which a hung session never
  writes — so it hung forever, holding the item's live tag and its worktree.
- **"What did it cost?"** had no answer either, so every interactive run was recorded
  at zero tokens and the spend figures covered only the mode nobody was running.

Both are answerable from the one artefact the CLI writes regardless: the transcript it
files under ``<config>/projects/<cwd-with-separators-dashed>/<session-id>.jsonl``. Its
mtime is the session's pulse, and its assistant entries carry the usage.

**Usage MUST be de-duplicated by message id.** The CLI appends the same assistant
message more than once as it streams, and the counts on those lines are that message's
running total, not increments. On a real session on this machine, 940 usage lines
carried 537 distinct ``message.id`` — summing lines overstated the bill by three
quarters. A billing number that is 75% wrong is worse than no number, because it gets
believed.

Nothing here raises: a missing, partial or unreadable transcript means "unknown", and
every caller has to treat unknown as "no evidence", never as zero.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

# Enough of the end of the file to hold the last few entries. A transcript grows into
# the megabytes, and the dashboard asks this question for every running row on every
# refresh — reading the whole file to show one line would make the page cost grow with
# the length of the session it is describing.
_TAIL_BYTES = 32_768
_SUMMARY_CHARS = 140
# The live view wants a scrollback, not one line, so it reads more — still a bounded
# slice of the end rather than a file that grows all session.
_FEED_BYTES = 400_000


def project_dir(cwd: str) -> Path:
    """Directory Claude Code files this working directory's transcripts under."""
    config_dir = Path(os.environ.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude"))
    return config_dir / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(Path(cwd)))


def newest(cwd: str) -> Path | None:
    """The most recently written transcript for ``cwd``, or None if there is none."""
    if not cwd:
        return None
    try:
        return max(project_dir(cwd).glob("*.jsonl"),
                   key=lambda f: f.stat().st_mtime, default=None)
    except OSError:
        return None


def session_id(cwd: str) -> str | None:
    """Session id of the newest conversation recorded for ``cwd``.

    The interactive CLI never reports its own session id, so this is how a later
    headless rework picks that conversation back up instead of re-reading the codebase.
    """
    found = newest(cwd)
    return found.stem if found else None


def quiet_seconds(cwd: str) -> float | None:
    """Seconds since the session last wrote anything. None = no transcript at all.

    This is the pulse a watchdog runs on. It deliberately measures SILENCE rather than
    total runtime: a long run is not a symptom — a build, a big refactor and a careful
    review all take a while — whereas a run that has produced nothing for a long time
    is either wedged or abandoned, and those are the two an operator must hear about.
    """
    found = newest(cwd)
    if found is None:
        return None
    try:
        return max(0.0, time.time() - found.stat().st_mtime)
    except OSError:
        return None


def _blocks(content: object) -> list[dict]:
    if isinstance(content, list):
        return [b for b in content if isinstance(b, dict)]
    return []


def _summarise(entry: dict) -> str:
    """One readable line for an assistant entry, or '' when it says nothing useful."""
    from ai_autopilot import activity

    message = entry.get("message")
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()[:_SUMMARY_CHARS]
    # Last block wins: an assistant turn is usually some prose and then the tool call
    # it settled on, and the tool call is the more informative half of "what is it
    # doing right now".
    for block in reversed(_blocks(content)):
        kind = block.get("type")
        if kind == "tool_use":
            args = block.get("input")
            return activity.tool_summary(
                str(block.get("name") or "tool"), args if isinstance(args, dict) else None
            )
        text = str(block.get("text") or "").strip()
        if kind == "text" and text:
            return text.replace("\n", " ")[:_SUMMARY_CHARS]
    return ""


def last_activity(cwd: str) -> tuple[str, float | None]:
    """``(what it last did, seconds ago)`` for the live session in ``cwd``.

    ``("", None)`` when there is no transcript. The age comes from the file's mtime,
    not from the entry's own timestamp, so it stays right even for an entry kind this
    function does not know how to summarise.
    """
    found = newest(cwd)
    if found is None:
        return "", None
    age = quiet_seconds(cwd)
    try:
        with found.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - _TAIL_BYTES))
            tail = f.read().decode("utf-8", "replace")
    except OSError:
        return "", age
    lines = tail.splitlines()
    if size > _TAIL_BYTES and lines:
        lines = lines[1:]   # the seek almost certainly cut the first line in half
    for raw in reversed(lines):
        stripped = raw.strip()
        if not stripped:
            continue
        try:
            entry = json.loads(stripped)
        except ValueError:
            continue
        if isinstance(entry, dict) and entry.get("type") == "assistant":
            summary = _summarise(entry)
            if summary:
                return summary, age
    return "", age


def _clock(entry: dict) -> str:
    """The entry's own timestamp as local ``HH:MM:SS`` — '' when it carries none."""
    raw = str(entry.get("timestamp") or "")
    if not raw:
        return ""
    try:
        return (datetime.fromisoformat(raw.replace("Z", "+00:00"))
                .astimezone().strftime("%H:%M:%S"))
    except ValueError:
        return ""


def recent(cwd: str, limit: int = 80) -> str:
    """The session's last actions as a readable feed — '' when there is no transcript.

    Shaped like the activity log a headless run writes, so the dashboard's live view
    can show an interactive session with no idea that it is reading a different source.
    Human turns are kept as well as the agent's: on a steered session, what the person
    just told it is usually the most important line on the page.
    """
    found = newest(cwd)
    if found is None:
        return ""
    try:
        with found.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - _FEED_BYTES))
            tail = f.read().decode("utf-8", "replace")
    except OSError:
        return ""
    lines = tail.splitlines()
    if size > _FEED_BYTES and lines:
        lines = lines[1:]
    out: list[str] = []
    for raw in lines:
        stripped = raw.strip()
        if not stripped:
            continue
        try:
            entry = json.loads(stripped)
        except ValueError:
            continue
        if not isinstance(entry, dict):
            continue
        kind = entry.get("type")
        if kind == "assistant":
            summary = _summarise(entry)
        elif kind == "user":
            # A plain string is a person typing. A list is tool RESULTS being fed back,
            # which is the bulk of a transcript and says nothing a reader wants.
            message = entry.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            said = content.strip() if isinstance(content, str) else ""
            summary = f"👤 {said[:_SUMMARY_CHARS]}" if said else ""
        else:
            summary = ""
        if summary:
            stamp = _clock(entry)
            out.append(f"[{stamp}] {summary}" if stamp else summary)
    return "\n".join(out[-limit:])


@dataclass
class TranscriptUsage:
    """What a session spent, as its own transcript records it."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    models: dict[str, int] = field(default_factory=dict)   # model -> tokens
    messages: int = 0                                      # distinct assistant messages

    @property
    def total_tokens(self) -> int:
        return (self.input_tokens + self.output_tokens
                + self.cache_read_tokens + self.cache_creation_tokens)

    def apply(self, result) -> None:
        """Write the totals onto an ``ExecutionResult``.

        ``cost_usd`` is deliberately NOT set: this transcript format carries no price,
        and inventing one — or writing 0.0 — would turn "we do not know" into a figure
        someone budgets against. The dashboard already renders an unknown cost as "—"
        and says how many runs it could not price.
        """
        result.cost_tokens = self.total_tokens
        result.input_tokens = self.input_tokens
        result.output_tokens = self.output_tokens
        result.cache_read_tokens = self.cache_read_tokens
        result.cache_creation_tokens = self.cache_creation_tokens
        if self.models:
            top = max(self.models, key=lambda k: self.models[k])
            extra = len(self.models) - 1
            result.model_used = f"{top} +{extra}" if extra > 0 else top


def read_usage(cwd: str) -> TranscriptUsage | None:
    """Everything the session in ``cwd`` spent, de-duplicated by message id.

    None when there is no transcript to read. A transcript with no usable usage entries
    returns an empty total rather than None — "it ran and spent nothing we can see" and
    "there was no run" are different answers, and only the second should stay blank.
    """
    found = newest(cwd)
    if found is None:
        return None
    usage = TranscriptUsage()
    seen: set[str] = set()
    try:
        with found.open("r", encoding="utf-8", errors="replace") as f:
            for raw in f:
                stripped = raw.strip()
                if not stripped or '"usage"' not in stripped:
                    continue
                try:
                    entry = json.loads(stripped)
                except ValueError:
                    continue      # a half-written last line is normal on a LIVE session
                if not isinstance(entry, dict) or entry.get("type") != "assistant":
                    continue
                message = entry.get("message")
                if not isinstance(message, dict):
                    continue
                counts = message.get("usage")
                if not isinstance(counts, dict):
                    continue
                # The same message is appended repeatedly while it streams, each line
                # carrying that message's running total — so one line per id is the
                # whole truth about it and the repeats must not be added on top.
                key = str(message.get("id") or entry.get("requestId")
                          or entry.get("uuid") or "")
                if not key or key in seen:
                    continue
                seen.add(key)
                usage.messages += 1
                got = _counts(counts)
                usage.input_tokens += got[0]
                usage.output_tokens += got[1]
                usage.cache_read_tokens += got[2]
                usage.cache_creation_tokens += got[3]
                model = str(message.get("model") or "").strip()
                if model:
                    usage.models[model] = usage.models.get(model, 0) + sum(got)
    except OSError:
        return None
    return usage


def _counts(counts: dict) -> tuple[int, int, int, int]:
    def num(key: str) -> int:
        value = counts.get(key)
        return value if isinstance(value, int) and value > 0 else 0

    return (num("input_tokens"), num("output_tokens"),
            num("cache_read_input_tokens"), num("cache_creation_input_tokens"))
