# SRS — [Tên tính năng]

> **Người đọc: agent, QC, Operator.** Agent soạn nháp từ PRD nhẹ đã được khách xác nhận; **BA rà soát** (+ SA nếu tầng Đỏ).
> Mỗi FR phải **trỏ về một mục PRD**. FR mồ côi (không có mục PRD) là lỗi của cổng G1.

| Mục | Nội dung |
|---|---|
| Work item | #____ |
| PRD nguồn | `01-prd-nhe.md` v____ |
| Người soạn nháp | Agent (phiên ____) |
| Người rà soát | BA ____ / SA ____ (Đỏ) |
| Phiên bản | v0.1 |

## 1. Functional Requirements
| FR | Mô tả (kiểm chứng được) | Trỏ về PRD |
|---|---|---|
| FR-01 | ____ | PRD §3.1 |
| FR-02 | ____ | PRD §3.2 |

## 2. Acceptance Criteria
| AC | Given / When / Then | FR | Có TC? |
|---|---|---|---|
| AC-01 | Cho ____, khi ____, thì ____ | FR-01 | ☐ |
| AC-02 | ____ | FR-02 | ☐ |

> Quy tắc: AC phải **kiểm chứng được bằng máy hoặc bằng một người không biết gì về code**. "Hệ thống hoạt động tốt" không phải AC.

## 3. Edge cases
| Edge | Hành vi mong đợi | AC |
|---|---|---|
| E-01 | ____ | AC-__ |

## 4. Data Definition
| Trường | Kiểu | Bắt buộc | Ràng buộc |
|---|---|---|---|
| ____ | ____ | ☐ | ____ |

## 5. Validation & Constraints
- ____

## 6. Bảo mật và phân quyền
| Vai trò người dùng | Được làm | Không được làm |
|---|---|---|
| ____ | ____ | ____ |

## 7. Giả định agent đã tự đưa ra (BA phải hỏi lại khách)
| # | Giả định | Lý do | BA đã xác nhận? |
|---|---|---|---|
| 1 | ____ | ____ | ☐ |

## 8. Version Control
| Ngày | Phiên bản | Thay đổi | Ai |
|---|---|---|---|
| ____ | v0.1 | Agent soạn nháp | Agent |

> Khi code lệch spec trong lúc làm: **comment lên work item** (xem `.claude/rules/spec-drift-reporting.md`), rồi cập nhật bảng này.
> Cổng G1 chấm: `ac_verifiable`, `srs_traceable`. Cổng G4 chấm: `ac_coverage`.
