# PRD nhẹ — [Tên tính năng]

> **Người đọc: khách / owner.** Một trang. Mục tiêu: khách nhìn và nói "đúng" hoặc "sai" trong vài phút.
> Không viết FR/edge case ở đây; phần đó thuộc SRS (`02-srs.md`) do agent soạn nháp.

| Mục | Nội dung |
|---|---|
| Work item | #____ |
| Tầng rủi ro dự kiến | Xanh / Vàng / Đỏ (xác nhận ở giai đoạn 2) |
| BA | ____ |
| Khách / Owner xác nhận | ____ (ngày ____) |
| Phiên bản | v0.1 |

## 1. Ý định (3-5 dòng)
- **Vì sao làm:** ____
- **Ai cần / ai dùng:** ____
- **Thước đo thành công:** ____
- **Ngoài phạm vi (không làm):** ____

## 2. Người dùng nhìn thấy gì
Mockup / ảnh chụp: `____` (đính kèm work item). Mô tả 3-6 câu bằng lời thường.

## 3. Kịch bản chính (Given / When / Then, ngôn ngữ thường)
1. **Khi** ____, **thì** ____.
2. **Khi** ____, **thì** ____.
3. **Khi** ____ (trường hợp bất thường), **thì** ____.

## 4. Bảo mật và phân quyền (bắt buộc nếu đụng quyền, tiền, dữ liệu cá nhân, tích hợp ngoài)
- Ai được làm gì: ____
- Dữ liệu nhạy cảm liên quan: ____

## 5. Giả định và câu hỏi mở
| # | Giả định / câu hỏi | Ai trả lời | Trạng thái |
|---|---|---|---|
| 1 | ____ | ____ | Mở / Đã chốt |

## 6. Xác nhận
- [ ] Khách / Owner xác nhận mục 1-4 (ghi tên, ngày, kênh: ____)
- [ ] **Tầng Đỏ:** khách ký **từng AC** ở mục 3

> Cổng G1 chấm: `customer_confirmed`, `security_in_spec`, `open_questions`. Xem `gates/gate-scorecard.json`.
