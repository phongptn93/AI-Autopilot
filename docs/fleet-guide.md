# 🛰 Fleet — hướng dẫn sử dụng máy trung tâm & máy trạm

> Cài đặt nhanh: [`QUICK-SETUP.md`](QUICK-SETUP.md) §5. Tài liệu này giải thích **cách hoạt động**, **từng tính năng** và **từng thiết lập**.

## 1. Mô hình

```
┌──────────── máy trạm (worker) ────────────┐         ┌────────── máy trung tâm (central) ──────────┐
│ poller · executor · dashboard             │ ──────▶ │ POST /api/fleet/heartbeat  (mỗi 10 phút)    │
│ FleetAgentService                         │ ◀────── │   ← cấu hình chung (khi khác hash)          │
│   • heartbeat + sức khỏe                  │ ──────▶ │ POST /api/fleet/commands   (mỗi 60 giây)    │
│   • hỏi lệnh, chạy lệnh, báo kết quả      │ ◀────── │   ← lệnh đang chờ cho đúng máy này          │
│   • góp / nhận tri thức                   │ ──────▶ │ POST /api/fleet/knowledge                   │
└───────────────────────────────────────────┘         │ FleetWatchService: báo offline, expire lệnh │
                                                      └─────────────────────────────────────────────┘
```

**Hai nguyên tắc không đổi**

1. **Kết nối một chiều.** Chỉ máy trạm gọi đi. Lệnh từ trung tâm là một **hàng đợi** mà máy trạm tự đến lấy — máy sau NAT, máy ngủ, máy đổi IP đều điều khiển được mà không phải mở cổng nào trên máy trạm.
2. **Không bí mật nào đi qua fleet.** PAT, mật khẩu dashboard, `trigger_tag`, `workspaces`, `repos`, `database_url` và toàn bộ `fleet_*` bị lọc ở **cả hai đầu**. Một trung tâm cấu hình sai cũng không ghi được PAT hay đường dẫn lên máy trạm.

## 2. Trang Fleet trên máy trung tâm

### 2.1 Dải tổng quan

| Ô | Ý nghĩa | Màu cảnh báo khi |
|---|---|---|
| Online | số máy gọi về trong ngưỡng `fleet_offline_after_minutes` | — (xanh khi không máy nào offline) |
| Offline | máy im lặng quá ngưỡng | > 0 |
| Đang chạy / sức chứa | tổng run đang chạy / tổng `max_concurrent` các máy online | — |
| Tạm dừng | máy đang tạm dừng hoặc đang cập nhật | > 0 |
| Xong / Lỗi hôm nay | chỉ tính máy có số liệu **của hôm nay** | lỗi > 0 |
| Config lệch | máy chưa áp dụng bản cấu hình đang phát | > 0 |
| Cần cập nhật | máy chạy bản **cũ hơn** trung tâm | > 0 |
| Disk thấp | disk trống < `fleet_disk_warn_gb` | > 0 |

### 2.2 Giao việc (🎯)

Nhập mã work item → chọn máy (hoặc **⚡ Tự chọn máy rảnh nhất**) → tuỳ chọn **Ưu tiên vai** → **Giao việc**.

Cách chọn máy tự động:

1. Loại máy **offline**, **tạm dừng**, **đang cập nhật**, **từ chối lệnh**, **không chạy poller**.
2. Ưu tiên máy đang chạy **đúng vai** được chọn.
3. Rồi máy có **tải thấp nhất** = run đang chạy ÷ sức chứa (máy 1/4 rảnh hơn máy 1/1).

Máy nhận lệnh sẽ gắn **trigger tag của chính nó** lên work item (và đưa item về trigger state nếu cần) — y như có người gắn tag tay cho máy đó, nên quyền sở hữu, pipeline vai, PR… chạy đúng luồng sẵn có. Máy `dry_run` từ chối việc này.

### 2.3 Điều khiển

| Nút | Phạm vi | Máy trạm làm gì |
|---|---|---|
| ⏸ Tạm dừng | 1 máy / toàn fleet | Ngừng nhận việc **mới**; run đang chạy vẫn chạy hết |
| ▶ Tiếp tục | 1 máy / toàn fleet | Nhận việc lại |
| ⟳ Đồng bộ ngay | 1 máy / toàn fleet | Heartbeat ngay: báo cáo + kéo cấu hình |
| ⬆ Cập nhật | máy cũ hơn trung tâm | Cài bản bằng version trung tâm: **đợi run xong → cài → kiểm version → khởi động lại** |

Mỗi lệnh có trạng thái: `chờ máy nhận` → `máy đã nhận` → `xong` / `thất bại` (kèm lời giải thích của máy trạm), hoặc `hết hạn` (không ai trả lời trong `fleet_command_expire_minutes`) / `đã huỷ` (huỷ khi còn chờ). Bấm cùng một nút hai lần chỉ xếp **một** lệnh. Mọi lệnh được ghi **Audit trail** ở cả hai phía.

> ⚠️ Tạm dừng lưu trong bộ nhớ: **khởi động lại** tiến trình máy trạm sẽ bỏ tạm dừng — chủ ý, để máy không bị "ngủ quên" mà không ai nhớ lý do.

### 2.4 Sức khỏe từng máy

Disk trống / tổng · uptime · sức chứa · tracker đã đăng nhập chưa · số run lỗi liên tiếp · lỗi gần nhất (kèm mã item) · hệ điều hành · `dry-run` · máy có đang từ chối lệnh. Máy bản cũ chưa gửi thông tin này sẽ hiện "chưa gửi thông tin sức khỏe" thay vì số 0 giả.

### 2.5 Cảnh báo

Khi `fleet_alert_offline: true`, trung tâm gửi qua các kênh thông báo đã cấu hình (Teams/Email/Zalo, theo đúng alert policy và quiet hours):

- 🔴 **Máy trạm X offline** — một lần cho mỗi đợt im lặng (mức WARNING)
- 🟢 **Máy trạm X đã online lại** — khi heartbeat đầu tiên quay lại (mức INFO)

## 3. Trang Fleet trên máy trạm

- Kết nối tới trung tâm, lần gọi gần nhất, vân tay cấu hình chung, thiết lập vừa nhận
- **Lệnh từ trung tâm**: đang nhận hay từ chối, chu kỳ hỏi, lần hỏi cuối có lỗi không
- **Lệnh vừa xử lý**: ✅/⛔ + lý do
- **⏸ Tạm dừng máy này / ▶ Tiếp tục nhận việc** — người ngồi tại máy luôn tự lấy lại được máy, không cần trung tâm
- Banner vàng khi máy chạy bản cũ hơn trung tâm, kèm lệnh cập nhật

## 4. Thiết lập

### Máy trung tâm

| Khoá | Mặc định | Ý nghĩa |
|---|---|---|
| `fleet_role` | `""` | `central` |
| `fleet_token` | `""` | Bí mật chung. **Rỗng = API fleet tắt** |
| `fleet_offline_after_minutes` | `30` | Ngưỡng coi là offline |
| `fleet_alert_offline` | `true` | Báo offline / online lại |
| `fleet_command_expire_minutes` | `60` | Lệnh không được trả lời sẽ hết hạn |
| `fleet_disk_warn_gb` | `5.0` | Ngưỡng disk thấp |
| `fleet_knowledge_auto_promote` | `2` | Số máy độc lập cùng báo một tri thức để tự duyệt (0 = duyệt tay) |

### Máy trạm

| Khoá | Mặc định | Ý nghĩa |
|---|---|---|
| `fleet_role` | `""` | `worker` |
| `fleet_central_url` | `""` | `http://<vm>:5080` |
| `fleet_token` | `""` | Khớp với trung tâm (khuyến nghị ENV `AUTOPILOT_FLEET_TOKEN`) |
| `fleet_worker_name` | hostname | Tên hiển thị — giữ cố định để lịch sử gom về một dòng |
| `fleet_sync_interval_minutes` | `10` | Chu kỳ heartbeat + đồng bộ cấu hình |
| `fleet_command_poll_seconds` | `60` | Chu kỳ hỏi lệnh (tối thiểu 15) |
| `fleet_accept_commands` | `true` | Tắt = từ chối mọi lệnh từ xa |
| `fleet_accept_remote_update` | `true` | Tắt = không cho trung tâm cài bản mới |
| `fleet_local_keys` | `[]` | Khoá trung tâm không được ghi đè trên máy này |
| `fleet_knowledge_sync` | `true` | Góp/nhận tri thức với đội |
| `fleet_knowledge_accept` | `auto` | `manual` = xếp hàng, bấm Nhận tay |

## 5. Vận hành chuyên nghiệp — khuyến nghị

1. **Trước deploy rủi ro**: ⏸ Tạm dừng tất cả → deploy → kiểm → ▶ Tiếp tục tất cả.
2. **Nâng cấp**: cập nhật **trung tâm trước**, rồi bấm **⬆ Cập nhật máy cũ** — máy trạm tự xả hàng rồi mới cài.
3. **Phân vai theo máy**: đặt `sdlc_profile` + `fleet_local_keys: [sdlc_profile]` trên từng máy (dev/qc/review), rồi dùng **Ưu tiên vai** khi giao việc.
4. **Máy cá nhân nhạy cảm**: `fleet_accept_remote_update: false`, giữ lệnh tạm dừng/đồng bộ.
5. **Theo dõi**: bật một kênh Teams cho cảnh báo fleet; ô **Disk thấp** và **run lỗi liên tiếp ≥ 3** là hai dấu hiệu nên xử lý trong ngày.

## 6. Tương thích phiên bản

| Trung tâm | Máy trạm | Kết quả |
|---|---|---|
| mới | cũ | Heartbeat + config vẫn chạy; không có sức khỏe; lệnh **hết hạn** → bấm cập nhật máy (bằng tay lần đầu) |
| cũ | mới | Máy trạm nhận 404 ở hàng đợi lệnh và im lặng bỏ qua — không lỗi |
