# Danh mục bằng chứng theo cổng

> **Nguyên tắc 3:** bằng chứng chạy thật, không phải lời khẳng định. Một tiêu chí chỉ được ghi `pass`/`partial` khi có **đường dẫn bằng chứng** (log, id pipeline, ảnh, báo cáo).
> Không có bằng chứng → bộ tính điểm tự hạ xuống `unknown`.

Điền một bảng cho mỗi cổng cần chấm, rồi chuyển sang JSON input cho `gates/gate_score.py` (xem `gates/examples/`).

## G1 — Định nghĩa
| Tiêu chí | Trạng thái | Bằng chứng |
|---|---|---|
| ac_verifiable | pass / partial / fail / na | Link work item + danh sách AC |
| customer_confirmed | | Ảnh xác nhận / email / chat có tên và ngày |
| srs_traceable | | Bảng truy vết FR ↔ PRD |
| open_questions | | Bảng giả định, cột trạng thái |
| security_in_spec | | Mục "Bảo mật và phân quyền" |

## G2 — Kế hoạch
| Tiêu chí | Trạng thái | Bằng chứng |
|---|---|---|
| plan_traces_fr | | Bảng bước ↔ FR |
| risk_classified | | Kết quả `risk.classify` + kết luận Xanh/Vàng/Đỏ |
| scope_bounded | | Số file ước lượng |
| sa_review | | Comment duyệt của SA (Vàng/Đỏ) |
| threat_note | | Mục đe doạ trong plan |

## G3 — Thực thi
| Tiêu chí | Trạng thái | Bằng chứng |
|---|---|---|
| build_tests | | Id pipeline / log test |
| secrets_clean | | Kết quả quét bí mật |
| sast_deps | | Báo cáo SAST + CVE phụ thuộc (có KEV/EPSS) |
| deps_verified | | Kết quả `pkgguard`: mọi phụ thuộc mới có trên registry, đủ tuổi, phiên bản ghim có thật |
| auto_review | | Kết luận auto-review (số critical/warning) |
| diff_bounded | | Số file trong PR; không chạm đường dẫn bảo vệ |
| sandbox_least_priv | | Worktree / sandbox, quyền đã cấp |

## G4 — Kiểm chứng
| Tiêu chí | Trạng thái | Bằng chứng |
|---|---|---|
| tc_pass | | Báo cáo TC (không fail, không blocked) |
| ac_coverage | | Bảng AC ↔ TC |
| exploratory | | Ghi chú phiên exploratory |
| security_verify | | Báo cáo review + DAST/PoC |
| no_open_drift | | Truy vấn `SPEC-DRIFT` = 0 mở |

## G5 — Nghiệm thu
| Tiêu chí | Trạng thái | Bằng chứng |
|---|---|---|
| ac_signed | | Biên bản nghiệm thu (`05-nghiem-thu.md`) |
| demo_real | | Video / ảnh demo trên bản build |
| exceptions_ok | | Danh sách ngoại lệ: lý do, người duyệt, hạn |
| spec_synced | | Version Control của spec khớp bản giao |
| rollback_ready | | Ghi chú triển khai / lệnh rollback |
