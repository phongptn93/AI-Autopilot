# ⚡ AI-Autopilot — Cài đặt nhanh

> Đọc 5 phút, chạy 15 phút. Chi tiết từng thiết lập: [`ai-autopilot-user-guide.html`](ai-autopilot-user-guide.html) · Fleet đầy đủ: [`fleet-guide.md`](fleet-guide.md).

## 0. Chọn mô hình triển khai

| Mô hình | Khi nào | Máy cần cài |
|---|---|---|
| 🖥 **Độc lập** (mặc định) | 1 người / 1 máy chạy autopilot | 1 máy |
| 🛰 **Fleet** | Nhiều máy dev cùng chạy, muốn **1 nơi** giữ cấu hình chung, theo dõi, điều khiển và giao việc | 1 **máy trung tâm** (VM) + N **máy trạm** |

Máy trạm luôn **tự gọi về** trung tâm — trung tâm không bao giờ gọi vào máy trạm, nên máy sau NAT/VPN/Wi-Fi văn phòng vẫn tham gia được. Chỉ **máy trung tâm** cần mở cổng.

---

## 1. Chuẩn bị (mọi máy)

- [ ] **Python 3.11–3.13** (`py -3.12 --version`)
- [ ] **Claude Code** đã cài và đăng nhập (`claude --version`)
- [ ] **Azure DevOps PAT**: Work Items R/W · Code R/W · Build Read
- [ ] **Workspace**: một thư mục có `.claude/` (skills, rules, MCP) và các repo nguồn là thư mục con

## 2. Cài đặt

**Windows (khuyến nghị):**

```bat
pip install https://github.com/phongptn93/AI-Autopilot/releases/latest/download/ai_autopilot-<version>-py3-none-any.whl
setx AUTOPILOT_ADO_PAT "your-pat"
ai-autopilot
```

hoặc từ mã nguồn: `run.bat` (tự tạo venv, cài, chép `config.yaml`, khởi động).

**Linux / macOS:**

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e .
export AUTOPILOT_ADO_PAT=...
python -m ai_autopilot
```

Mở **http://localhost:5080/dashboard** → trình **Setup** hướng dẫn từng bước (kết nối ADO, workspace, trigger, vai). Không cần khởi động lại sau khi lưu.

## 3. Kiểm tra

```bash
ai-autopilot doctor        # kiểm cấu hình, kết nối, quyền PAT, lệnh Claude
```

Tất cả ✅ là chạy được. Lỗi nào cũng có kèm cách sửa.

## 4. Cấu hình khởi đầu an toàn

```yaml
autonomy_level: assisted         # PR nháp — người review trước khi merge
use_worktrees: true              # bắt buộc khi chạy song song
max_concurrent: 1                # tăng dần khi đã tin
auto_review_enabled: true
dependency_scheduling_enabled: true
trigger_states: [Proposed]       # RỜI khỏi mọi state đầu ra
```

🔒 Secret luôn để **biến môi trường** (`AUTOPILOT_ADO_PAT`, `AUTOPILOT_FLEET_TOKEN`…), không để trong `config.yaml`. Đặt mật khẩu dashboard trước khi mở cổng ra ngoài localhost.

---

## 5. Bật Fleet (tuỳ chọn)

### 5.1 Máy trung tâm — 3 bước

1. **Settings → 🛰 Fleet → Vai của máy này = `central`**
2. **Token chung** → bấm ✨ sinh ngẫu nhiên → Lưu → bấm 👁 để copy
3. Cho máy trạm truy cập được: `health_host: 0.0.0.0` + **đặt mật khẩu dashboard** + mở cổng `5080` trên firewall

### 5.2 Mỗi máy trạm — 3 bước

1. **Settings → 🛰 Fleet → Vai = `worker`**
2. **URL trung tâm** = `http://<vm-trung-tam>:5080` · **Token chung** = token vừa copy (hoặc `setx AUTOPILOT_FLEET_TOKEN ...`)
3. Bấm **Thử kết nối** → ✅ → Lưu. Máy xuất hiện trên trang **Fleet** của trung tâm trong vài giây.

### 5.3 Dùng ngay được gì

| Ở đâu | Làm gì |
|---|---|
| **Trung tâm → Fleet** | Dải tổng quan (online/offline, đang chạy/sức chứa, lỗi hôm nay, config lệch, cần cập nhật, disk thấp) |
| | 🎯 **Giao work item** cho một máy cụ thể hoặc ⚡ máy rảnh nhất (ưu tiên đúng vai) |
| | ⏸ Tạm dừng · ▶ Tiếp tục · ⟳ Đồng bộ · ⬆ Cập nhật — **từng máy** hoặc **toàn fleet** |
| | Sức khỏe từng máy: disk, uptime, tracker, lỗi gần nhất, số run lỗi liên tiếp |
| | 🔔 Thông báo khi máy **offline** và khi **online lại** (qua kênh Teams/Email/Zalo đã cấu hình) |
| **Máy trạm → Fleet** | Trạng thái kết nối, lệnh vừa nhận, nút **Tạm dừng / Tiếp tục máy này** tại chỗ |

### 5.4 Giữ quyền tự chủ cho máy trạm

| Thiết lập (máy trạm) | Tác dụng |
|---|---|
| `fleet_local_keys: [sdlc_profile]` | Trung tâm không ghi đè các khoá này (vd giữ vai `qc` riêng) |
| `fleet_accept_commands: false` | Từ chối mọi lệnh từ xa (vẫn báo cáo + đồng bộ config) |
| `fleet_accept_remote_update: false` | Không cho trung tâm cài bản mới lên máy này |

---

## 6. Sự cố thường gặp

| Triệu chứng | Nguyên nhân & cách sửa |
|---|---|
| Máy trạm báo **401** | Token 2 phía chưa khớp — copy lại bằng nút 👁 trên trung tâm |
| Máy trạm **không gọi được** trung tâm | Firewall / `health_host` còn `127.0.0.1` trên trung tâm |
| Trang Fleet trung tâm **404** | `fleet_role` chưa là `central`, hoặc **token rỗng** (API fleet chỉ bật khi có token) |
| Lệnh ở trạng thái **hết hạn** | Máy tắt, hoặc chạy bản cũ chưa có hàng đợi lệnh → cập nhật máy đó |
| Máy "**config lệch**" mãi | Máy chạy bản cũ hơn trung tâm (bỏ qua khoá mới) → ⬆ Cập nhật |
| Giao việc báo **không máy nào nhận** | Tất cả offline / tạm dừng / đang cập nhật / từ chối lệnh |
| Lệnh **Nhận việc** thất bại "dry_run" | Máy trạm đang `dry_run: true` — không ghi gì lên tracker |
