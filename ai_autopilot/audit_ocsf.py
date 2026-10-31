"""The audit trail as OCSF events — what a SIEM (Sentinel, Splunk, Security Lake) ingests.

The trail stays one plain table; this is only an export shape. Each row is mapped by
its ``action`` to the closest OCSF class:

==============================  ===============================  =====================
action                          class                            activity
==============================  ===============================  =====================
dashboard.login / login_failed  Authentication (3002)            Logon (1)
dashboard.logout                Authentication (3002)            Logoff (2)
dashboard.denied                Web Resource Access (6004)       Access Deny (2)
user.add / user.signup          Account Change (3001)            Create (1)
user.enable / disable / delete  Account Change (3001)            Enable / Disable / Delete
user.password                   Account Change (3001)            Password Change (3)
user.approve / user.save        User Access Management (3005)    Assign Privileges / Other
user.roles, auth.mode, config.  Entity Management (3004)         Update (3)
anything else                   API Activity (6003)              Other (99)
==============================  ===============================  =====================

The original row always rides along under ``unmapped``, so nothing the dashboard shows
is lost to a mapping that turns out too coarse. ``OCSF_VERSION`` is pinned: the schema
moves between minor versions and a consumer validates against the one we name.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

OCSF_VERSION = "1.3.0"

_SEVERITY_INFO, _SEVERITY_LOW = 1, 2
_SUCCESS, _FAILURE = 1, 2

# action -> (class_uid, activity_id, activity_name)
_EXACT: dict[str, tuple[int, int, str]] = {
    "dashboard.login": (3002, 1, "Logon"),
    "dashboard.login_failed": (3002, 1, "Logon"),
    "dashboard.logout": (3002, 2, "Logoff"),
    "dashboard.denied": (6004, 2, "Access Deny"),
    "user.add": (3001, 1, "Create"),
    "user.signup": (3001, 1, "Create"),
    "user.enable": (3001, 2, "Enable"),
    "user.password": (3001, 3, "Password Change"),
    "user.disable": (3001, 5, "Disable"),
    "user.delete": (3001, 6, "Delete"),
    "user.approve": (3005, 1, "Assign Privileges"),
    "user.save": (3005, 99, "Other"),
    "user.roles": (3004, 3, "Update"),
    "auth.mode": (3004, 3, "Update"),
    "dashboard.audit_export": (6003, 2, "Read"),
}
_CLASS_NAME = {
    3001: "Account Change", 3002: "Authentication", 3004: "Entity Management",
    3005: "User Access Management", 6003: "API Activity", 6004: "Web Resource Access Activity",
}
_FAILED = {"dashboard.login_failed", "dashboard.denied"}


def _classify(action: str) -> tuple[int, int, str]:
    if action in _EXACT:
        return _EXACT[action]
    if action.startswith("config."):
        return (3004, 3, "Update")
    return (6003, 99, "Other")


def _user(name: str) -> dict:
    out: dict = {"name": name}
    if "@" in name:
        out["email_addr"] = name
    return out


def to_ocsf(event, *, product_version: str = "") -> dict:
    """One ``AuditEvent`` row as one OCSF event dict."""
    action = event.action or ""
    class_uid, activity_id, activity_name = _classify(action)
    failed = action in _FAILED
    at = event.at if event.at.tzinfo else event.at.replace(tzinfo=UTC)
    target = event.target or ""
    out: dict = {
        "class_uid": class_uid,
        "class_name": _CLASS_NAME[class_uid],
        "category_uid": class_uid // 1000,
        "activity_id": activity_id,
        "activity_name": activity_name,
        "type_uid": class_uid * 100 + activity_id,
        "time": int(at.timestamp() * 1000),
        "severity_id": _SEVERITY_LOW if failed else _SEVERITY_INFO,
        "status_id": _FAILURE if failed else _SUCCESS,
        "message": f"{action} {target}".strip(),
        "actor": {"user": _user(event.actor or "")},
        "metadata": {
            "version": OCSF_VERSION,
            "uid": str(event.id),
            "log_name": "audit_events",
            "product": {"name": "AI-Autopilot", "vendor_name": "AI-Autopilot",
                        **({"version": product_version} if product_version else {})},
        },
        "unmapped": {"action": action, "source": event.source or "", "target": target,
                     "detail": event.detail or ""},
    }
    if failed and event.detail:
        out["status_detail"] = event.detail
    if getattr(event, "src_ip", ""):
        out["src_endpoint"] = {"ip": event.src_ip}

    if class_uid == 3002:
        # The account being signed in to. A failed login names what was typed.
        out["user"] = _user(event.actor or "")
        out["auth_protocol"] = "OIDC" if "sso" in (event.detail or "") else "Password"
    elif class_uid in (3001, 3005):
        out["user"] = _user(target)
        if class_uid == 3005 and event.detail:
            out["privileges"] = [p for p in event.detail.split(",") if p]
    elif class_uid == 3004:
        out["entity"] = {"name": target or action, "type": action.split(".")[0]}
    elif class_uid == 6004:
        method, _, path = target.partition(" ")
        out["http_request"] = {"http_method": method, "url": {"path": path or target}}
        out["web_resources"] = [{"url_string": path or target}]
    else:
        out["api"] = {"operation": action}
    return out


def dumps_ndjson(events, *, product_version: str = "") -> str:
    """Newline-delimited JSON — one event per line, what log shippers read."""
    return "".join(json.dumps(to_ocsf(e, product_version=product_version),
                              ensure_ascii=False, separators=(",", ":")) + "\n"
                   for e in events)


def filename(now: datetime | None = None) -> str:
    return f"audit-ocsf-{(now or datetime.now(UTC)).strftime('%Y%m%d-%H%M%S')}.ndjson"
