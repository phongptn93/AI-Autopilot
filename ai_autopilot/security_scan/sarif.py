"""SARIF 2.1.0 export — the format code-scanning UIs (Azure DevOps Advanced Security,
GitHub code scanning, VS Code SARIF viewer) ingest directly.

One run per tool, one rule per distinct ``rule_id``, one result per finding. Kept
minimal and valid rather than exhaustive: a SARIF file that a viewer rejects for one
optional property it did not like is worse than one with fewer properties.
"""

from __future__ import annotations

import json

from ai_autopilot.reports import Finding

_LEVEL = {"critical": "error", "high": "error", "medium": "warning", "low": "note", "info": "note"}
_RANK = {"critical": 95.0, "high": 80.0, "medium": 50.0, "low": 20.0, "info": 5.0}
_TOOL_URI = {
    "semgrep": "https://semgrep.dev", "gitleaks": "https://github.com/gitleaks/gitleaks",
    "sca": "https://github.com/aquasecurity/trivy", "builtin": "https://github.com/",
    "ai": "https://claude.com/claude-code", "dast": "https://owasp.org/API-Security/",
    "poc": "https://claude.com/claude-code",
}


def to_sarif(findings: list[Finding], *, repo: str = "", version: str = "") -> dict:
    runs: dict[str, dict] = {}
    for f in findings:
        tool = f.tool or "ai"
        run = runs.setdefault(tool, {"rules": {}, "results": []})
        rule_id = f.rule_id or (f.cwe or "finding").lower()
        rule = run["rules"].setdefault(rule_id, {
            "id": rule_id,
            "name": rule_id.replace("-", " ").title().replace(" ", ""),
            "shortDescription": {"text": f.title[:200] or rule_id},
            "properties": {
                "tags": [t for t in ("security", f.cwe, f.owasp) if t],
                "security-severity": str(_RANK.get(f.severity, 5.0)),
            },
        })
        if f.cwe and f"external/cwe/{f.cwe.lower()}" not in rule["properties"]["tags"]:
            rule["properties"]["tags"].append(f"external/cwe/{f.cwe.lower()}")
        result: dict = {
            "ruleId": rule_id,
            "level": _LEVEL.get(f.severity, "note"),
            "rank": _RANK.get(f.severity, 5.0),
            "message": {"text": (f.title + (" — " + f.detail if f.detail else ""))[:1000]},
            "partialFingerprints": {"aiAutopilot/v1": f.fingerprint} if f.fingerprint else {},
            "properties": {
                "severity": f.severity, "confidence": f.confidence or "",
                "cwe": f.cwe, "owasp": f.owasp, "tool": tool,
            },
        }
        if f.file:
            loc: dict = {"artifactLocation": {
                "uri": f.file.replace("\\", "/"), "uriBaseId": "%SRCROOT%",
            }}
            if f.line:
                loc["region"] = {"startLine": int(f.line)}
                if f.snippet:
                    loc["region"]["snippet"] = {"text": f.snippet[:500]}
            result["locations"] = [{"physicalLocation": loc}]
        run["results"].append(result)

    return {
        "$schema": "https://raw.githubusercontent.com/oasis-tcs/sarif-spec/main/sarif-2.1/schema/sarif-schema-2.1.0.json",
        "version": "2.1.0",
        "runs": [
            {
                "tool": {"driver": {
                    "name": f"ai-autopilot/{tool}",
                    "informationUri": _TOOL_URI.get(tool, "https://github.com/"),
                    **({"version": version} if version else {}),
                    "rules": list(run["rules"].values()),
                }},
                "originalUriBaseIds": {"%SRCROOT%": {"uri": _file_uri(repo)}} if repo else {},
                "results": run["results"],
            }
            for tool, run in sorted(runs.items())
        ],
    }


def _file_uri(path: str) -> str:
    p = path.replace("\\", "/").rstrip("/") + "/"
    return ("file:///" + p.lstrip("/")) if not p.startswith("file:") else p


def dumps(findings: list[Finding], **kw) -> str:
    return json.dumps(to_sarif(findings, **kw), indent=2, ensure_ascii=False)
