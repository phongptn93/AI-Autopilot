# Kế hoạch pilot — kiểm chứng quy trình trước khi áp dụng rộng

> Quy trình trong gói này là **đề xuất có lập luận, chưa được kiểm chứng bằng dữ liệu chạy thật** (xem mục "Kiểm chứng" trong handbook). Pilot là cách biến nó thành bằng chứng.

| Mục | Nội dung |
|---|---|
| Phạm vi | 1 đội, ____ work item, ____ tuần |
| Chủ pilot | ____ |
| Dự án / repo | ____ |
| Preset AI-Autopilot | `safe` hoặc `scrum` (xem handbook mục "Đối chiếu AI-Autopilot") |

## 1. Giả thuyết cần kiểm chứng
| # | Giả thuyết | Đo bằng | Ngưỡng ban đầu (hiệu chỉnh sau tuần 2) | Bác bỏ nếu |
|---|---|---|---|---|
| H1 | Với spec có AC kiểm chứng được, agent thực thi việc Xanh mà không tăng lỗi lọt | Tỷ lệ lỗi lọt tầng Xanh | ≤ ____ % | Cao hơn baseline thủ công |
| H2 | Cổng điểm bắt đúng việc cần người xem | Tỷ lệ cổng PASS/CONFIRM mà sau đó bị trả lại | ≤ ____ % | Có lỗi nghiêm trọng lọt qua cổng PASS |
| H3 | Spec hai lớp giảm thời gian chốt với khách | Thời gian từ yêu cầu đến khách xác nhận | Giảm ≥ ____ % | Không giảm hoặc tăng số lần làm lại |
| H4 | Phân tầng rủi ro đặt đúng việc nào cần người | Số việc bị hạ/nâng tầng sau khi làm | ≤ ____ % | Thường xuyên phân loại sai tầng Đỏ |
| H5 | Hàng đợi chờ người duyệt không thành nút cổ chai | Thời gian chờ người / việc | ≤ ____ giờ | Hàng đợi tăng liên tục |

## 2. Baseline (đo trước pilot, cùng loại việc)
| Chỉ số | Giá trị baseline | Nguồn |
|---|---|---|
| Lead time | ____ | ADO / `process_health` |
| Tỷ lệ làm lại | ____ | |
| Lỗi lọt sau nghiệm thu | ____ | |
| Thời gian chờ người | ____ | |

## 3. Lịch
| Tuần | Làm gì | Tầng |
|---|---|---|
| 0 | Gán vai trò, bật cấu hình, tập huấn mẫu | — |
| 1–2 | Chạy việc **Xanh**; ghi điểm cổng và kết quả thật | Xanh |
| 3–4 | Thêm việc **Vàng** | Xanh + Vàng |
| 5 | Rà soát: so điểm cổng với kết quả; chỉnh ngưỡng; quyết định tiếp | — |

## 4. Điều kiện dừng
- Bất kỳ sự cố bảo mật hoặc mất dữ liệu liên quan agent → **dừng ngay**, rà soát cổng và quyền.
- Lỗi lọt tầng Xanh vượt ngưỡng 2 tuần liên tiếp → siết (chuyển cổng sang `confirm`/`full`).

## 5. Ghi kết quả
| Work item | Tầng | Điểm G1–G5 | Quyết định cổng | Kết quả thật | Cổng đúng? |
|---|---|---|---|---|---|
| #____ | | | | | ☐ |

> Quyết định nới hay siết dựa trên bảng này, không dựa trên cảm giác.
