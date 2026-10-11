"""A 5-field cron expression in plain Vietnamese — "Thứ 2–6 lúc 18:07".

People read "7 18 * * 1-5" by counting fields; the loops page shows this sentence next
to the box so a wrong field is seen when it is typed, not when the job fails to fire.
Covers the shapes people actually write (fixed time, ranges, lists, */N); anything
else is returned as None and the page shows the raw expression.
"""

from __future__ import annotations

_DOW = {0: "CN", 1: "Thứ 2", 2: "Thứ 3", 3: "Thứ 4", 4: "Thứ 5", 5: "Thứ 6", 6: "Thứ 7", 7: "CN"}


def _days(field: str) -> str | None:
    if field == "*":
        return "Mỗi ngày"
    if field in ("1-5", "mon-fri"):
        return "Thứ 2–6"
    if field in ("0,6", "6,0", "sat,sun"):
        return "Cuối tuần"
    try:
        if "-" in field and "," not in field:
            a, b = (int(x) for x in field.split("-"))
            if 1 <= a <= 6 and 1 <= b <= 6:
                return f"Thứ {a + 1}–{b + 1}"
            return f"{_DOW[a]}–{_DOW[b]}"
        return ", ".join(_DOW[int(x)] for x in field.split(","))
    except (ValueError, KeyError):
        return None


def describe(expr: str) -> str | None:
    parts = (expr or "").split()
    if len(parts) != 5:
        return None
    minute, hour, dom, month, dow = parts
    if month != "*":
        return None
    days = _days(dow)
    if dom != "*":
        if dow != "*" or not dom.isdigit():
            return None
        days = f"Ngày {dom} hằng tháng"
    if days is None:
        return None
    if minute.startswith("*/") and hour == "*":
        return f"{days}, mỗi {minute[2:]} phút"
    if minute.isdigit() and hour.startswith("*/"):
        return f"{days}, mỗi {hour[2:]} giờ (phút {int(minute):02d})"
    if minute.isdigit() and hour == "*":
        return f"{days}, mỗi giờ vào phút {int(minute):02d}"
    if minute.isdigit() and all(h.isdigit() for h in hour.split(",")):
        times = ", ".join(f"{int(h):02d}:{int(minute):02d}" for h in hour.split(","))
        return f"{days} lúc {times}"
    return None
