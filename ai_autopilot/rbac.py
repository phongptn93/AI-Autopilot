"""Roles and permissions for a dashboard many people use.

One shared password meant everyone who could open the dashboard could also rewrite
its config — the ADO PAT included — and the audit log could only ever say
"dashboard". With ``auth_mode`` set to ``users`` or ``sso`` every request belongs to a
person, and this module answers the one question the guard asks: *may this person do
this?*

Design rules:

* **Deny by default.** A write route nobody mapped here needs ``admin``. Adding a page
  can never silently open a new door; at worst it is admin-only until mapped.
* **Permissions, not page lists.** A role is a set of permission keys, so a custom role
  is a list of checkboxes, and a new page only has to name the permission it needs.
* **Built-in roles are a starting point.** Admins can override them or add their own
  (``roles`` in config); ``admin`` always keeps everything, so no edit can lock the
  last administrator out.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# key → (label, group, description)
PERMISSIONS: dict[str, tuple[str, str, str]] = {
    "view": ("Xem dashboard", "Chung", "Mở mọi trang chỉ-xem: board, lịch sử, báo cáo, phân tích."),
    "runs.control": ("Điều khiển run", "Vận hành",
                     "Chạy / huỷ / tiếp tục run, kéo thả Board, duyệt kế hoạch, lập kế hoạch."),
    "process.edit": ("Sửa quy trình", "Vận hành",
                     "Vai trò SDLC, luồng trạng thái, board, tác vụ định kỳ."),
    "review.run": ("Phòng rà soát", "Chất lượng",
                   "Tạo phiên rà soát, tải tài liệu, hỏi đáp, phản biện."),
    "tracker.write": ("Ghi lên tracker", "Chất lượng",
                      "Tạo work item / comment lên ADO từ báo cáo, rà soát, bảo mật."),
    "requirements.write": ("Tạo yêu cầu", "Yêu cầu", "Tạo yêu cầu mới cho BA."),
    "requirements.approve": ("Duyệt spec", "Yêu cầu",
                             "Duyệt / trả lại spec, quyết định lệch spec."),
    "security.run": ("Quét bảo mật", "Bảo mật",
                     "Quét lại, xác minh, chấp nhận / mở lại phát hiện."),
    "knowledge.edit": ("Sửa tri thức", "Chất lượng", "Thêm / sửa / xoá bài học của agent."),
    "settings.edit": ("Sửa thiết lập", "Hệ thống",
                      "Lưu Thiết lập, workspace, cài đặt nhanh, import cấu hình."),
    "secrets.view": ("Xem secret", "Hệ thống",
                     "Hiện PAT/mật khẩu, export cấu hình đầy đủ (kèm secret)."),
    "fleet.control": ("Điều khiển Fleet", "Hệ thống", "Gửi lệnh, giao việc cho máy trạm."),
    "system.update": ("Cập nhật phần mềm", "Hệ thống", "Kiểm tra và cài bản mới."),
    "users.manage": ("Quản lý người dùng", "Hệ thống",
                     "Duyệt tài khoản, gán vai, sửa ma trận quyền, cấu hình SSO."),
}

ADMIN = "admin"
BUILTIN_ROLES: dict[str, tuple[str, list[str]]] = {
    ADMIN: ("👑 Admin", ["*"]),
    "lead": ("🧑‍💻 Lead / vận hành", [
        "view", "runs.control", "process.edit", "review.run", "tracker.write",
        "requirements.write", "requirements.approve", "security.run", "knowledge.edit",
        "fleet.control"]),
    "ba": ("📋 BA", ["view", "review.run", "tracker.write", "requirements.write",
                    "requirements.approve"]),
    "qc": ("🧪 QC", ["view", "review.run", "tracker.write", "security.run"]),
    "viewer": ("👀 Viewer", ["view"]),
}

# (method, path regex under /dashboard, permission). First match wins. Paths are matched
# against the part AFTER "/dashboard".
_RULES: list[tuple[str, str, str]] = [
    ("*", r"/(login|logout|auth/.*)$", ""),                 # always reachable
    ("POST", r"/workspace/select$", "view"),                # a view filter, not a change
    ("GET", r"/settings/(reveal/.*|export-full)$", "secrets.view"),
    ("POST", r"/settings/import-full$", "secrets.view"),
    ("*", r"/users(/.*)?$", "users.manage"),
    ("GET", r".*", "view"),
    ("POST", r"/(board/(move|run)|inbox/.*|queue/resume|planning/.*|conflicts/.*|"
             r"sessions/.*/cancel|feedback/.*|history/.*)$", "runs.control"),
    ("POST", r"/(roles(/preset)?|flow|board-views|loops(/.*)?)$", "process.edit"),
    ("POST", r"/review/(start|upload|sweep|\d+/(ask|challenge|rerun))$", "review.run"),
    ("POST", r"/(review/\d+/(file|comment)|reports/\d+/file|security/\d+/file)$",
     "tracker.write"),
    ("POST", r"/requirements$", "requirements.write"),
    ("POST", r"/(requirements/review|specs/(decide|resolve))$", "requirements.approve"),
    ("POST", r"/security/(rescan|\d+/(verify|suppress|reopen))$", "security.run"),
    ("POST", r"/learning/.*$", "knowledge.edit"),
    ("POST", r"/(settings(/.*)?|workspaces|setup(/.*)?|security/settings)$", "settings.edit"),
    ("POST", r"/fleet/.*$", "fleet.control"),
    ("POST", r"/update(/check)?$", "system.update"),
]
_COMPILED = [(m, re.compile(p), perm) for m, p, perm in _RULES]


def required(method: str, path: str) -> str:
    """Permission a request needs. ``""`` = public; unmapped writes need ``admin``."""
    sub = path[len("/dashboard"):] if path.startswith("/dashboard") else path
    sub = sub or "/"
    for m, rx, perm in _COMPILED:
        if (m == "*" or m == method.upper()) and rx.match(sub):
            return perm
    return ADMIN


@dataclass
class Role:
    key: str
    label: str
    permissions: list[str]
    builtin: bool = True


def roles(custom: dict | None) -> dict[str, Role]:
    """Built-in roles overlaid with the admin's edits and additions.

    ``custom`` is ``{key: {"label": "...", "permissions": [...]}}``. ``admin`` is never
    overridden: the matrix editor must not be able to lock the last admin out.
    """
    out = {k: Role(k, lbl, list(p)) for k, (lbl, p) in BUILTIN_ROLES.items()}
    for key, spec in (custom or {}).items():
        k = re.sub(r"[^a-z0-9_-]", "", str(key).lower())[:40]
        if not k or k == ADMIN or not isinstance(spec, dict):
            continue
        perms = [p for p in spec.get("permissions") or [] if p in PERMISSIONS]
        label = str(spec.get("label") or (out[k].label if k in out else k))[:60]
        out[k] = Role(k, label, perms, builtin=k in BUILTIN_ROLES)
    return out


def permissions_of(user_roles: list[str], custom: dict | None) -> set[str]:
    table = roles(custom)
    perms: set[str] = set()
    for r in user_roles or []:
        role = table.get(r)
        if role:
            perms.update(role.permissions)
    return perms


def allowed(perms: set[str], needed: str) -> bool:
    if not needed:
        return True
    if "*" in perms:
        return True
    if needed == ADMIN:
        return False
    return needed in perms
