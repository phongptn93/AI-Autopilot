# Security scanning — SAST · SCA · secrets · AI review

AI Autopilot doubles as a security testing tool for the repositories it works on.
The same engine runs from four places and cannot disagree with itself:

| Entry point | When | What it does |
|---|---|---|
| `ai-autopilot scan` | on demand / CI | full or diff scan, prints table / JSON / **SARIF** / HTML / Markdown, **exit code gates the pipeline** |
| Scan loop (`mode: scan`) | on a cron | same scan, stored, rendered, notified |
| Pre-PR review gate | every autopilot PR | deterministic scanners on the branch diff — a committed secret blocks the PR **regardless of what the model says** |
| `/dashboard/security` | always | every finding with a life: open · fixed · suppressed · false positive; trend; suppress with reason + expiry |

## What runs

```
scanners (parallel) ──► model pass (optional) ──► merge ──► fingerprint ──► suppress ──► baseline diff ──► store ──► render
   builtin   pure-Python rules: secrets (PEM, AWS, Azure, ADO PAT, JWT, conn strings…),
             C#/.NET (raw SQL concat, BinaryFormatter, JWT validation off, CORS *, TLS off…),
             TS/Angular (bypassSecurityTrust, innerHTML, eval, token in localStorage…),
             Python (shell=True, yaml.load, pickle, verify=False…)
   gitleaks  secrets in tree or in a commit range
   semgrep   rule-based SAST with dataflow (OWASP packs + language packs)
   sca       vulnerable dependencies: trivy fs, dotnet list --vulnerable, npm audit, pip-audit
   ai        `agent-security-reviewer` + the `security-review` skill, READ-ONLY (file tools denied
             at the tool surface). It is handed the scanners' findings first: it triages them
             (CONFIRMED / FALSE POSITIVE with a reason) and then looks for what regexes cannot
             see — BOLA/IDOR, missing role checks, mass assignment.
```

Every scanner except `builtin` is optional. A missing binary is **skipped and said so** —
in the run output, in the report, and by `ai-autopilot doctor`. "0 findings" and "did
not run" are opposite answers and are never conflated.

## Findings have identity

A finding's **fingerprint** = `sha1(tool | rule | file | normalised snippet-or-title)`.
Line numbers are excluded on purpose: the same secret in the same file is the same
finding after fifty lines are added above it. That identity is what makes everything
else possible:

- **Baseline** — the second scan of a repo reports *new* vs *known* vs *fixed*.
  The gate (`fail_on`) only trips on **new, unsuppressed** findings, so a repo with
  known debt still gates on regressions. A fixed finding that comes back is *new* again.
- **Lifecycle** — one row per (repo, fingerprint) in `security_findings`:
  `open → fixed` when a full scan no longer reports it, reopened if it returns,
  `suppressed` while a suppression is active. A `diff` scan only vouches for the files
  it read; it never closes findings elsewhere.
- **Suppressions** — `<workspace>/.autopilot/security-suppressions.yaml`, versioned with
  the workspace so a CI runner sees the same list as the dashboard:

  ```yaml
  suppressions:
    - fingerprint: 3f2a9c1e0b7d4e5f6a8b9c0d
      reason: "test fixture key, never deployed"
      by: phong.pham
      expires: 2026-12-31        # optional — expired → reported again, and the run says so
  ```
  Every entry needs a reason. Get fingerprints from `--format json` or the Security page.

## The Security page — `/dashboard/security`

Built around the three questions a visit asks, in order:

| Block | Answers | What you can do |
|---|---|---|
| **▶ Scan now** + **Readiness** | *Can I scan, and with what?* | Pick repo · tools · AI mode (off/fast/deep) · scope (full/diff) · PoC verify — then watch **live progress** (stage, elapsed, per-tool result, link to the AI feed). Readiness shows, per tool, installed / not installed / off, and whether the AI pass has a key. |
| **⚙ Scan settings** (fold-out) | *What is a scan, by default?* | AI mode, gate severity, default & pre-PR tools, ADO Bug filing, PoC verify, **disabled rules**, **ignore paths** — saved to `config.yaml`, applied live. |
| **Open now** + **Trend** | *How bad, and is it getting better?* | Tiles filter the list; the trend is stacked per severity per scan, red underline = gate failed, click a bar → that scan. |
| **Findings** | *What do I do about each one?* | Filter by repo / status / severity (with counts) / tool / **new in 7 days**; free-text search over title, file, rule, CWE, fingerprint; per row: **Suppress** (reason + expiry), **False positive**, **Verify** (PoC), **File Bug**, **Reopen**. |
| **Recent scans** | *What did the last run do?* | Per-tool ✓/✗, counts, new · fixed · suppressed, gate, report; `#` opens the run's page. |
| ⬇ SARIF / JSON | export the current list | for ADO Advanced Security, VS Code SARIF viewer, or your own tooling |

**Finding page** (`/security/f/{id}`) — the full record: location, snippet, CWE ↗ / OWASP ↗
links, confidence + PoC verdict, fingerprint, history (first/last seen, every scan it
appeared in, ADO Bug), and every action with its consequence spelled out. Plus a ready-made
`claude "Fix …"` prompt for the repo.

**Scan page** (`/security/scans/{id}`) — what ran (tool statuses — *0 findings from a tool
that did not run is not clean*), counts, **new in this scan**, **fixed since the previous**.

### Noise control — rules and paths, not just findings

Suppressing ten `ts-target-blank` rows one by one is the wrong tool. Two config knobs
(also on the page):

```yaml
security_scan:
  disabled_rules: [ts-target-blank, cfg-http-not-https]   # rule ids, any tool
  ignore_paths:                                           # repo-relative globs
    - "**/Migrations/**"
    - "**/*.Designer.cs"
    - "**/wwwroot/lib/**"
```

Applied to every entry point (CLI, loops, pre-PR gate, page); the scan reports how many it
dropped (`filtered`), so the number is visible rather than silently smaller.

### Inline exceptions — next to the code they excuse

For a single reviewed line, put the reason where the next reader will see it (and where
it moves with the code), instead of in a list that drifts:

```python
# autopilot:ignore[py-shell-true] operator-configured test command, not untrusted input
proc = await asyncio.create_subprocess_shell(cmd, ...)
```

| Marker | Scope |
|---|---|
| `autopilot:ignore[rule-a, rule-b] <reason>` | only those rules |
| `autopilot:ignore <reason>` | every rule on that line |
| `nosec` · `nosemgrep` · `gitleaks:allow` | honoured too (all rules) — one marker satisfies every scanner |

On the finding's own line, or alone on a **comment line** directly above it (a code line
above does not count — its marker is its own). Always give the reason: it is the review.

Two more things the builtin rules do so the list stays worth reading:

- **Documented example credentials are allowlisted** — AWS's `AKIAIOSFODNN7EXAMPLE` and
  any secret whose value says `example` (gitleaks uses the same stopword). In tests, build
  fake keys at runtime (`"AKIA" + "…"`) so no scanner — ours, gitleaks, ADO/GitHub push
  protection — sees a literal.
- **Code rules skip comment lines; secret rules do not.** "Never call `eval()` here" is
  advice; a key pasted into a comment is still a leaked key.

Every exception is **counted, not hidden**: the tool line reads
`builtin → 0 finding(s) in 3.4s (7 inline ignored, 3 allowlisted)`.

## CLI

```bash
ai-autopilot scan                                  # config defaults; repo = repo_working_directory
ai-autopilot scan --repo ../Backend --mode deep    # full OWASP checklist pass by the model
ai-autopilot scan --scope diff --base development  # only files changed on this branch
ai-autopilot scan --no-ai --tools builtin,gitleaks --fail-on critical     # CI, no API key needed
ai-autopilot scan --format sarif --out scan.sarif  # for ADO Advanced Security / GitHub code scanning
ai-autopilot scan --no-store --no-html             # laptop: no database, no report file
```

Exit codes: `0` gate passed · `1` new finding at/above `--fail-on` · `2` usage · `3` scan could not run.
Logs go to stderr; stdout is only the formatted result, so `--format json | jq` works.

### In Azure Pipelines

```yaml
# azure-pipelines.yml — gate the PR on NEW findings, publish SARIF for Advanced Security
- script: pip install ai-autopilot   # or: pip install -e path/to/AI-Autopilot
  displayName: Install scanner
- script: |
    ai-autopilot scan --repo $(Build.SourcesDirectory) --scope diff --base $(System.PullRequest.TargetBranch) \
      --no-ai --no-store --tools builtin,gitleaks,semgrep,sca \
      --fail-on high --format sarif --out $(Build.ArtifactStagingDirectory)/scan.sarif
  displayName: Security scan (deterministic, no API key)
  env:
    AUTOPILOT_CONFIG_FILE: $(Build.SourcesDirectory)/.autopilot/ci-config.yaml   # optional
- task: PublishBuildArtifacts@1
  condition: always()
  inputs: { pathToPublish: $(Build.ArtifactStagingDirectory)/scan.sarif, artifactName: CodeAnalysisLogs }
```

`--no-store` means no baseline on the runner: every finding in the diff counts as new,
which is what a PR gate wants. Run the stored, AI-assisted scan on a schedule from the
autopilot itself (scan loop) instead.

## Configuration

See `config.example.yaml` → `security_scan:`. The defaults are safe to leave on:
`tools: [builtin, gitleaks, semgrep, sca]`, `ai_mode: fast`, `fail_on: high`.

## What the model is and is not allowed to do

The AI pass runs through the executor's read-only path: `Write`/`Edit` tools are denied,
the session is fresh every run, and its output is parsed under the same JSON contract every
report loop uses (`cwe`, `owasp`, `rule_id`, `confidence` added). A scanner finding the
model calls a false positive is **demoted, not deleted** — the reason is attached and the
reader can disagree.

## Phase 3 — PoC verification (`--verify` / `verify_enabled`)

A scanner finding is a hypothesis. With verification on, each **new** finding at or above
`verify_from` (default `high`, at most `verify_max_per_scan`) is handed to the model with
one instruction: *build the smallest proof that this actually happens, run it, report
honestly*. It runs the `security-verify-poc` skill inside a **git worktree created for
that one finding** and torn down afterwards — nothing is committed, pushed or merged.

| Verdict | Effect |
|---|---|
| `verified: true` | `confidence → high`, the PoC (command + evidence) is stored on the finding and shown on the Security page |
| `verified: false` | finding stays open, badged **unconfirmed**, with the model's reason (guarded upstream, input unreachable, already parameterised…) |
| error / no isolation | skipped and counted; never run in the real checkout |

```bash
ai-autopilot scan --repo ../Backend --verify        # needs worktrees + ANTHROPIC_API_KEY
```

## Phase 4 — DAST (`--dast <target>` / `dast_enabled`)

Probes a **running** application over HTTP for OWASP API Top 10 issues (missing auth,
BOLA/IDOR with two identities, function-level authz, mass assignment, injection echoes,
security headers, verbose errors) using the `security-dast` skill. Findings flow into the
same fingerprint / baseline / lifecycle / page as everything else, with `tool: dast` and
the request line in the `file` field.

**Gates fail closed** — the probe is refused (exit 2, no request sent) unless all hold:

| Gate | Why |
|---|---|
| `owner_confirmed: true` | You state, in versioned config, that this host is yours to test |
| host ∈ `allowed_hosts` | Exact allowlist; the base URL's host must be on it |
| `require_private` (default on) | Host must resolve to a private/loopback address |
| `allow_mutations` (default off) | Only `GET`/`HEAD`/`OPTIONS` unless you opt in |
| `max_requests`, `rate_limit_rps`, `allowed_paths` | Hard caps the model cannot exceed |

```yaml
security_scan:
  dast_enabled: true
  dast_targets:
    - name: staging
      base_url: https://staging.example.internal
      allowed_hosts: [staging.example.internal]
      owner_confirmed: true
      auth_env_var: DAST_TOKEN_A
      auth_b_env_var: DAST_TOKEN_B     # second identity → IDOR test
```

```bash
ai-autopilot scan --dast staging
```

`ai-autopilot doctor` warns about a target that is enabled but not owner-confirmed.
