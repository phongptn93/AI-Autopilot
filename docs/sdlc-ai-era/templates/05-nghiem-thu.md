# Biên bản nghiệm thu — [Tên tính năng]

> **Người chấp nhận cuối: khách / owner**, không phải BA đã viết spec.
> BA nghiệm thu nội bộ trước (kiểm tra bằng chứng); khách nghiệm thu chính thức sau.

| Mục | Nội dung |
|---|---|
| Work item | #____ |
| Bản build / PR | ____ |
| Tầng rủi ro | Xanh / Vàng / Đỏ |
| Điểm cổng G1–G4 | G1 ___ · G2 ___ · G3 ___ · G4 ___ |

## 1. Đối chiếu AC
| AC | Kết quả | Bằng chứng | Khách xác nhận |
|---|---|---|---|
| AC-01 | Đạt / Không đạt | ____ | ☐ |
| AC-02 | | | ☐ |

## 2. Lệch spec đã ghi nhận
| Mục | Hiện tại | Điều chỉnh | Đã comment work item? |
|---|---|---|---|
| ____ | ____ | ____ | ☐ |

(Không có lệch: ghi "Không có".)

## 3. Ngoại lệ bảo mật còn lại
| Phát hiện | Lý do chấp nhận | Người duyệt | Hạn xử lý |
|---|---|---|---|
| ____ | ____ | ____ | ____ |

## 4. Triển khai
- Đảo ngược được? ☐ Có ☐ Không (lưu ý: ____)
- Lệnh / kế hoạch rollback: ____

## 5. Quyết định
- [ ] BA nội bộ: ☐ Đạt ☐ Trả lại — ____ (ngày ____)
- [ ] **Khách / Owner:** ☐ Chấp nhận ☐ Từ chối — ____ (ngày ____)

Chữ ký / xác nhận: ____

> Cổng G5 chấm: `ac_signed`, `demo_real`, `exceptions_ok`, `spec_synced`, `rollback_ready`.
