# Runbook vận hành sau go-live — "phần chìm"

Mục đích: những sự cố **không phải do viết code sai** mà do không ai nhìn mực nước. Mỗi mục có: dấu hiệu sớm, cách đo, ngưỡng, ai hành động, hành động đầu tiên.

> **Phạm vi.** Tài liệu này là checklist và một probe nhỏ (`scripts/ops_probe.py`). AI-Autopilot **không** thay cho hệ thống giám sát. Nếu khách đã có Prometheus/Grafana/Azure Monitor, hãy đặt cùng các ngưỡng này ở đó và dùng probe chỉ như lưới an toàn.

## 0. Trước khi go-live: có người đứng tên chưa?

Không có tên ở đây thì phần còn lại vô nghĩa. Điền vào `templates/05-nghiem-thu.md`, mục 6.

| Việc | Ghi rõ |
|---|---|
| Người chịu trách nhiệm vận hành (một người, không phải "đội") | |
| Người thay thế khi vắng | |
| Kênh nhận cảnh báo | |
| Thời gian phản hồi cam kết | |
| Ai được quyền rollback | |

## 1. Bảng các rủi ro "phần chìm"

| # | Rủi ro | Dấu hiệu sớm | Đo bằng | Ngưỡng | Hành động đầu tiên |
|---|---|---|---|---|---|
| 1 | **Đĩa/PVC của DB đầy** → DB chuyển chỉ đọc, nhiều hệ thống đứng cùng lúc | Dung lượng tăng đều, log/backup phình | `python scripts/ops_probe.py --disk /var/opt/mssql` hoặc `kubectl exec <pod> -- df -h` | cảnh báo ≥ 80%, khẩn ≥ 90% | Dọn log/backup cũ, mở rộng PVC; **kiểm tra mọi DB trên máy đó**, không chỉ DB đang xem |
| 2 | **Chứng chỉ TLS sắp hết hạn** | Không có dấu hiệu, đến hạn là báo "không an toàn" | `python scripts/ops_probe.py --cert host:443` | cảnh báo ≤ 30 ngày, khẩn ≤ 7 ngày | Gia hạn; kiểm tra cơ chế tự gia hạn có chạy không (cert-manager, ACME) |
| 3 | **Cụm tự nâng cấp ban đêm**, dịch vụ không còn chỗ chạy lại | Pod `Pending` sau cửa sổ bảo trì | `kubectl get pods -A --field-selector=status.phase=Pending` | bất kỳ pod Pending > 10 phút | Kiểm tra tài nguyên node còn lại; đặt PodDisruptionBudget và cửa sổ bảo trì; xem `k8s-sre.md` |
| 4 | **Hai tiến trình cùng chạy một job cập nhật dữ liệu** (xung đột, "chạy ổn 100 lần, lỗi lần 101") | Lỗi khóa/xung đột lẻ tẻ, không tái hiện được | Log lỗi trùng khóa; số replica của worker | worker nền > 1 replica mà job không có khóa phân tán | Dùng khóa phân tán hoặc leader election; đặt replica = 1 cho đến khi có khóa |
| 5 | **Đổi phiên bản model hoặc tham số mặc định** → câu trả lời rỗng, không báo lỗi | Điểm run bỗng không đổi, review rỗng | `ai-autopilot canary` | bất kỳ probe nào rỗng/sai | Không triển khai; xem mục 3 |
| 6 | **CVE mới công bố cho thứ đang chạy** | Không có dấu hiệu nếu không ai nhìn | `ai-autopilot scan --audit-sbom <file>` | bất kỳ KEV, hoặc ≥ high | Xem mục 2 |

## 2. Kiểm tra lỗ hổng định kỳ trên bản đã phát hành

1. **Khi phát hành:** `ai-autopilot scan --sbom -` lưu SBOM CycloneDX vào `<workspace>/.autopilot/sbom/`. Đính kèm vào release.
2. **Hằng tuần (hoặc nightly):** `ai-autopilot scan --audit-sbom <file>`. Không cần checkout code của bản đó. Mã thoát `1` khi có CVE ≥ `fail_on` hoặc nằm trong CISA KEV; mã `3` khi **không kiểm tra được** (mất mạng) — coi đó là "chưa biết", không phải "sạch".
3. **Khi có KEV:** coi là sự cố, không phải backlog. CISA đặt hạn xử lý trong chính bản ghi; dùng hạn đó.
4. Thành phần **không có phiên bản chính xác** (đếm trong dòng `without a version`) không được kiểm tra. Ghim phiên bản bằng lockfile để chúng được tính.

## 3. Đổi model hoặc tham số

1. Đổi `claude_model` hoặc `claude_effort_*`.
2. Chạy `ai-autopilot canary`. Năm câu hỏi có đáp án xác định; câu trả lời rỗng, bị cắt hoặc sai đều làm fail.
3. Chỉ khi pass mới triển khai. `ai-autopilot doctor` cảnh báo nếu cấu hình đã đổi mà chưa có canary pass.
4. Canary chỉ bắt lỗi "không trả lời / trả lời hỏng". Chất lượng thật của review thì cần `evals replay` trên việc đã merge.

## 4. Mỗi khi có sự cố: ghi lại

| Mục | Nội dung |
|---|---|
| Phát hiện lúc nào, bởi ai/cái gì | |
| Nguyên nhân gốc (không phải triệu chứng) | |
| Đã có dấu hiệu sớm nào bị bỏ qua? | |
| Thêm probe/ngưỡng nào để lần sau thấy sớm hơn? | |

Mục cuối là mục quan trọng nhất: mỗi sự cố nên để lại thêm một dòng trong bảng ở mục 1.

## 5. Chạy probe định kỳ

```bash
# đĩa + chứng chỉ, trả mã thoát 1 khi vượt ngưỡng cảnh báo, 2 khi khẩn
python scripts/ops_probe.py --disk /var/opt/mssql --cert app.example.com:443 --cert api.example.com:443
```

Đặt vào cron / Azure Pipelines schedule / `CronJob` của k8s. Probe chỉ đọc, không sửa gì.
