"""The sidebar, as data.

Twenty-six links in two flat lists ("Operate" / "Configure") had stopped being a
menu: three entries were views of one config store, two were both called Audit, and
the board was described by four pages filed in two different places. Grouping by the
question a person arrives with — what is happening, what is the work, how good is it,
is it safe, how does it flow, how is this machine set up — puts every page next to the
ones it is usually read with.

Data rather than markup so a page cannot be added to the router and forgotten here
without a test noticing (tests/test_nav.py walks every entry), and so each link can
carry the one-line description shown on hover.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class NavItem:
    key: str        # the page's ``active`` value
    href: str
    icon: str
    label: str
    hint: str       # one line: what the page answers
    only_roles: tuple[str, ...] = ()   # fleet roles it shows for; () = always


@dataclass(frozen=True)
class NavGroup:
    key: str
    label: str
    items: tuple[NavItem, ...]


GROUPS: tuple[NavGroup, ...] = (
    NavGroup("overview", "🏠 Tổng quan", (
        NavItem("overview", "/dashboard", "📊", "Tổng quan",
                "Hôm nay autopilot làm được gì, cái gì cần chú ý."),
        NavItem("now", "/dashboard/now", "⏱", "Đang chạy",
                "Các run đang chạy, tiến độ so với bình thường, phiên bị treo."),
        NavItem("queue", "/dashboard/queue", "🙋", "Cần người xử lý",
                "Item đang chờ con người: thiếu thông tin, test đỏ, cần duyệt."),
        NavItem("delivery", "/dashboard/delivery", "🚚", "Bàn giao",
                "Góc nhìn PM: việc chờ merge, chờ review, đứng yên quá lâu."),
    )),
    NavGroup("work", "📋 Công việc", (
        NavItem("board", "/dashboard/board", "🗂️", "Board",
                "Bảng việc theo vai — bấm ▶ để chạy một item ngay."),
        NavItem("requirements", "/dashboard/requirements", "📋", "Yêu cầu & Spec",
                "BA viết yêu cầu, đọc spec do BA agent viết, góp ý hoặc duyệt."),
        NavItem("planning", "/dashboard/planning", "🧭", "Lập kế hoạch",
                "Chấm điểm, xếp thứ tự và hẹn giờ chạy backlog."),
        NavItem("reviews", "/dashboard/reviews", "👀", "PR Reviews",
                "PR đang chờ ai review, ai chưa vote, nhắc người review."),
        NavItem("conflicts", "/dashboard/conflicts", "⚔️", "Xung đột PR",
                "PR bị conflict và phiên tự giải conflict."),
        NavItem("history", "/dashboard/history", "📜", "Lịch sử chạy",
                "Mọi run đã xong: kết quả, chi phí, PR, file đã đổi."),
    )),
    NavGroup("quality", "📈 Chất lượng & báo cáo", (
        NavItem("analytics", "/dashboard/analytics", "📈", "Phân tích & ROI",
                "Tỉ lệ thành công, thời gian, chi phí, giờ công tiết kiệm."),
        NavItem("quality", "/dashboard/quality", "🔁", "Chất lượng",
                "Số vòng sửa, số lần retry, điểm PR theo thời gian."),
        NavItem("specs", "/dashboard/specs", "📐", "Lệch spec",
                "Chỗ code đã làm khác spec — cần BA cập nhật tài liệu."),
        NavItem("learning", "/dashboard/learning", "🧠", "Tri thức",
                "Bài học autopilot rút ra và chia sẻ cho cả đội."),
    )),
    NavGroup("security", "🔐 Bảo mật & kiểm toán", (
        NavItem("security", "/dashboard/security", "🔐", "Bảo mật",
                "Quét lỗ hổng mã nguồn & thư viện, CVE đang bị khai thác."),
        NavItem("reports", "/dashboard/reports", "🛡️", "Báo cáo rà soát",
                "Kết quả các agent rà soát định kỳ — tạo work item từ phát hiện."),
        NavItem("audit", "/dashboard/audit", "🧾", "Nhật ký thao tác",
                "Ai đổi gì, khi nào: cấu hình, lệnh, nút bấm trên dashboard."),
    )),
    NavGroup("process", "🔄 Quy trình", (
        NavItem("roles", "/dashboard/roles", "🔗", "Vai trò & preset",
                "Chuỗi vai BA → Dev → QC → Review và preset quy trình mẫu."),
        NavItem("flow", "/dashboard/flow", "🔀", "Luồng trạng thái",
                "Sơ đồ state ADO: item vào đâu, ra đâu sau mỗi kết quả."),
        NavItem("board-views", "/dashboard/board-views", "🧩", "Quy trình board",
                "Định nghĩa các board theo vai và cột của chúng."),
        NavItem("loops", "/dashboard/loops", "⏰", "Tác vụ định kỳ",
                "Agent chạy theo lịch: review code, quét bảo mật, rà spec."),
        NavItem("capabilities", "/dashboard/capabilities", "✨", "Năng lực",
                "Skill, agent và lệnh mà autopilot dùng được trên máy này."),
    )),
    NavGroup("system", "🛠 Hệ thống", (
        NavItem("setup", "/dashboard/setup", "🚀", "Cài đặt nhanh",
                "Trình hướng dẫn từng bước cho máy mới."),
        NavItem("settings", "/dashboard/settings", "🛠️", "Thiết lập",
                "Mọi thiết lập, sửa trực tiếp, áp dụng ngay."),
        NavItem("config", "/dashboard/config", "⚙️", "Cấu hình hiện tại",
                "Máy này khác cài đặt mặc định ở đâu — chỉ đọc."),
        NavItem("workspaces", "/dashboard/workspaces", "🗂", "Workspaces",
                "Nhiều dự án / repo trên cùng một kết nối."),
        NavItem("fleet", "/dashboard/fleet", "🛰", "Fleet",
                "Máy trung tâm & máy trạm: theo dõi, điều khiển, giao việc.",
                only_roles=("central", "worker")),
    )),
)


def groups(fleet_role: str = "") -> list[dict]:
    """The menu for this machine, as plain dicts the template iterates."""
    role = (fleet_role or "").strip()
    out = []
    for group in GROUPS:
        items = [i for i in group.items if not i.only_roles or role in i.only_roles]
        if items:
            out.append({"key": group.key, "label": group.label, "items": items})
    return out


def find(active: str) -> NavItem | None:
    """The entry a page's ``active`` key names — for its title and description."""
    for group in GROUPS:
        for item in group.items:
            if item.key == active:
                return item
    return None


def page_title(active: str) -> str:
    """A page's heading — the menu's own icon and label, so the two can never disagree.

    Every page used to type its own: "Overview", "Execution history", "What can AI
    Autopilot do?" under a Vietnamese menu, in h1 on some pages and h2 on others.
    """
    item = find(active)
    return f"{item.icon} {item.label}" if item else ""


def group_of(active: str) -> str:
    """The menu group a page sits in — the first half of its breadcrumb."""
    for group in GROUPS:
        if any(item.key == active for item in group.items):
            return group.label
    return ""


def page_hint(active: str) -> str:
    item = find(active)
    return item.hint if item else ""
