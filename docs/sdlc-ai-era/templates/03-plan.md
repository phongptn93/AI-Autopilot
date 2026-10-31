# Plan — [Tên tính năng]

> **Người duyệt: Operator.** Việc Vàng/Đỏ: **SA review giải pháp**, độc lập với người thiết kế.
> Agent đề xuất; người duyệt trước khi agent code.

| Mục | Nội dung |
|---|---|
| Work item | #____ |
| SRS | `02-srs.md` v____ |
| Operator (chịu trách nhiệm) | ____ |
| SA review | ____ (bắt buộc Vàng/Đỏ) |

## 1. Phân loại rủi ro
| Câu hỏi | Có | Không |
|---|---|---|
| Đụng migration / schema? | ☐ | ☐ |
| Đụng auth / phân quyền? | ☐ | ☐ |
| Đụng tiền / dữ liệu cá nhân / tích hợp ngoài? | ☐ | ☐ |
| Đụng deploy / CI / manifest phụ thuộc? | ☐ | ☐ |
| Số file ước lượng vượt ngưỡng (mặc định 25)? | ☐ | ☐ |

→ Có bất kỳ ô "Có" thì **Đỏ**. Feature mới trong module có sẵn: **Vàng**. Còn lại: **Xanh**.
**Kết luận:** ☐ Xanh ☐ Vàng ☐ Đỏ

> Đối chiếu với kết quả tự động: AI-Autopilot `risk.classify` trên danh sách file thực tế. Nếu máy báo cao hơn người, **theo máy**; nếu máy báo thấp hơn, người giải thích.

## 2. Các bước (mỗi bước trỏ về FR)
| # | Bước | FR | File dự kiến |
|---|---|---|---|
| 1 | ____ | FR-01 | ____ |

## 3. Giải pháp và phương án đã loại
- **Chọn:** ____
- **Đã cân nhắc và loại:** ____ (vì ____)

## 4. Ghi chú đe doạ (bắt buộc Vàng/Đỏ nếu đụng auth hoặc dữ liệu)
| Mối đe doạ | Biện pháp |
|---|---|
| ____ | ____ |

## 5. Rollback
- Đảo ngược được không? ☐ Có ☐ Không → nếu **Không**, ghi rõ lưu ý triển khai: ____

## 6. Duyệt
- [ ] Operator duyệt (ngày ____)
- [ ] SA review (Vàng/Đỏ): ☐ Đồng ý ☐ Yêu cầu sửa: ____

> Cổng G2 chấm: `plan_traces_fr`, `risk_classified`, `scope_bounded`, `sa_review`, `threat_note`.
