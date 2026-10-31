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
    "runs.start": ("Chạy theo vai", "Vận hành",
                   "Bấm Chạy trên Board, huỷ phiên đang chạy, chấm kết quả run — đủ để "
                   "QC/BA tự chạy chặng của mình."),
    "runs.control": ("Điều phối run", "Vận hành",
                     "Kéo thả Board (đổi state), duyệt kế hoạch, lập kế hoạch, xử lý xung đột, "
                     "gỡ dừng máy / hàng đợi, xoá lịch sử. Bao gồm Chạy theo vai."),
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

# Grants that can leak a credential or take the dashboard away from its admins — the
# matrix flags them so nobody ticks one in passing.
SENSITIVE = frozenset({"settings.edit", "secrets.view", "system.update", "users.manage"})

# Holding the left key means holding the right ones: steering runs without being able to
# start one is not a role anybody has.
_IMPLIES: dict[str, tuple[str, ...]] = {"runs.control": ("runs.start",)}

ADMIN = "admin"
BUILTIN_ROLES: dict[str, tuple[str, list[str]]] = {
    ADMIN: ("👑 Admin", ["*"]),
    "lead": ("🧑‍💻 Lead / vận hành", [
        "view", "runs.start", "runs.control", "process.edit", "review.run", "tracker.write",
        "requirements.write", "requirements.approve", "security.run", "knowledge.edit",
        "fleet.control"]),
    "ba": ("📋 BA", ["view", "runs.start", "review.run", "tracker.write",
                    "requirements.write", "requirements.approve"]),
    "qc": ("🧪 QC", ["view", "runs.start", "review.run", "tracker.write", "security.run"]),
    "viewer": ("👀 Viewer", ["view"]),
}
ROLE_HINTS: dict[str, str] = {
    ADMIN: "Toàn quyền, kể cả secret và người dùng.",
    "lead": "Điều phối máy & quy trình; không đụng secret/thiết lập.",
    "ba": "Viết, duyệt spec; tự chạy chặng BA.",
    "qc": "Rà soát, quét bảo mật, báo bug; tự chạy chặng QC.",
    "viewer": "Chỉ xem.",
}

# Every permission key that existed before saved roles started recording ``known``. A
# built-in role saved back then gets any key added since at its built-in default — an
# unticked box can only mean "denied" for a key the admin was actually shown.
_LEGACY_KNOWN = frozenset({
    "view", "runs.control", "process.edit", "review.run", "tracker.write",
    "requirements.write", "requirements.approve", "security.run", "knowledge.edit",
    "settings.edit", "secrets.view", "fleet.control", "system.update", "users.manage"})

# (method, path regex under /dashboard, permission). First match wins. Paths are matched
# against the part AFTER "/dashboard".
_RULES: list[tuple[str, str, str]] = [
    ("*", r"/(login|logout|auth/.*)$", ""),                 # always reachable
    ("POST", r"/workspace/select$", "view"),                # a view filter, not a change
    ("GET", r"/settings/(reveal/.*|export-full)$", "secrets.view"),
    # Restoring a full config rewrites every setting, roles and auth included — only an
    # admin may. (It used to need just secrets.view: a viewer of secrets could grant
    # itself users.manage, or set test_command and have the next run execute it.)
    ("POST", r"/settings/import-full$", ADMIN),
    ("*", r"/users(/.*)?$", "users.manage"),
    ("GET", r"/audit/export\.ocsf$", "users.manage"),       # bulk history + source IPs
    ("GET", r".*", "view"),
    ("POST", r"/(board/run|sessions/[^/]+/cancel|feedback/[^/]+)$", "runs.start"),
    ("POST", r"/(board/move|inbox/.*|queue/resume|planning/.*|conflicts/.*|history/.*)$",
     "runs.control"),
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
    customized: bool = False

    @property
    def hint(self) -> str:
        return ROLE_HINTS.get(self.key, "")


def roles(custom: dict | None) -> dict[str, Role]:
    """Built-in roles overlaid with the admin's edits and additions.

    ``custom`` is ``{key: {"label": "...", "permissions": [...], "known": [...]}}`` —
    ``known`` is the permission list the admin was shown when saving. ``admin`` is never
    overridden: the matrix editor must not be able to lock the last admin out.
    """
    out = {k: Role(k, lbl, _implied(p)) for k, (lbl, p) in BUILTIN_ROLES.items()}
    for key, spec in (custom or {}).items():
        k = re.sub(r"[^a-z0-9_-]", "", str(key).lower())[:40]
        if not k or k == ADMIN or not isinstance(spec, dict):
            continue
        perms = [p for p in spec.get("permissions") or [] if p in PERMISSIONS]
        if k in BUILTIN_ROLES:
            known = set(spec.get("known") or _LEGACY_KNOWN)
            perms += [p for p in BUILTIN_ROLES[k][1] if p not in known and p not in perms]
        label = str(spec.get("label") or (out[k].label if k in out else k))[:60]
        builtin = k in BUILTIN_ROLES
        out[k] = Role(k, label, _implied(perms), builtin=builtin,
                      customized=builtin and _implied(perms) != out[k].permissions)
    return out


def _implied(perms: list[str]) -> list[str]:
    out = list(perms)
    for p in perms:
        out += [q for q in _IMPLIES.get(p, ()) if q not in out]
    return [p for p in PERMISSIONS if p in out] if "*" not in out else ["*"]


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
