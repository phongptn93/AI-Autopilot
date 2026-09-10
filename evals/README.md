# Eval suite — regression tests for the agent's configuration

This repo has over a thousand tests for its Python and, until this directory existed,
none at all for the thing that decides what the agent actually produces: the skills,
rules and `CLAUDE.md` under `.claude/`. Editing one of those changes the product's
behaviour, and the only way to find out whether it changed for the better was to ship it.

An eval is a real task plus the checks that say what an acceptable answer looks like.

```bash
ai-autopilot evals                       # run every case, demand 100%
ai-autopilot evals evals --min-pass-rate 0.9
```

Non-zero exit when the pass rate is under the threshold, so CI can gate a change to
`.claude/` the way it gates a change to code.

## Writing a case

One YAML file per case (or a list of cases in one file):

```yaml
name: bugfix-workflow-writes-the-failing-test-first
prompt: |
  Read .claude/skills/bugfix-workflow/SKILL.md and answer in plain text:
  at which step does it tell you to write the failing test?
checks:
  - kind: contains
    value: reproduce
  - kind: not_contains
    value: I could not find
```

| Field | Meaning |
|---|---|
| `name` | Shown in the report. Defaults to the filename. |
| `prompt` | The task, exactly as a person would ask it. |
| `cwd` | Where the agent works. Blank = the case file's own directory. |
| `allowed_tools` | Restrict the tool surface. Omit for the default. |
| `timeout_seconds` | Per case. Default 900. |
| `checks` | What must hold. **A case with no checks fails.** |

### Check kinds

| Kind | Holds when |
|---|---|
| `contains` / `not_contains` | `value` appears (or does not) in the output |
| `regex` | `value` matches the output |
| `file_exists` / `file_absent` | `value` is a path under the case's cwd |
| `file_contains` | `path` exists and holds `value` |
| `shell` | `value` runs in the cwd and exits zero |

Text comparison is case-insensitive.

## The rule that keeps the suite worth running

**Assert facts, never style.** Text that must appear, a file that must exist, a command
that must exit zero — those survive a model upgrade. "Is the wording good" does not, and
an eval that fails for the wrong reason is worse than no eval, because the team learns
to ignore it.

Two consequences worth stating:

- **A case with no checks fails.** "The agent said something" is not a standard, and a
  case asserting nothing would quietly lift the pass rate while testing nothing.
- **An empty suite scores 0%, not 100%.** A suite that loaded nothing has proved
  nothing, and reporting that as a clean run is how a bad path becomes a green gate
  checking air.

## Where cases come from

The playbook's advice, which holds here: collect real tasks from recent work, and give
every production incident an eval that stays in the suite as a regression test. A case
written the day something broke is the one that stops it breaking twice.
