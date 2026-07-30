# AI Autopilot — Teams app package

Template sideload bot hai chiều vào Microsoft Teams (xem `ai_autopilot/teams_agent.py`).

## 1. Thay các placeholder trong `manifest.json`

| Placeholder | Thay bằng | Bắt buộc |
|---|---|---|
| `<TEAMS_AGENT_APP_ID>` (**2 chỗ**: `id` và `bots[0].botId`) | Application (client) ID của Azure Bot — cùng giá trị `teams_agent_app_id` trong `config.yaml` | ✅ |
| `<PUBLIC_HOST>` (**7 chỗ**: mỗi tab có `contentUrl` + `websiteUrl` = 6, cộng `validDomains`) | host công khai của autopilot, vd `autopilot.nois.vn` — **không** kèm `https://`, không kèm đường dẫn | chỉ khi dùng tab |
| `<WEBSITE_URL>` / `<PRIVACY_URL>` / `<TERMS_URL>` | trang thật của bạn | ✅ khi publish; sideload nội bộ thì URL tạm cũng qua |

Không dùng tab (chỉ cần chat) → xoá cả khối `staticTabs` và để `validDomains: []`.

## 2. Zip

```bash
cd teams-app
zip ai-autopilot-teams-app.zip manifest.json color.png outline.png
```

Zip phải **phẳng** — 3 file ở gốc, không nằm trong thư mục con, nếu không Teams từ chối.

## 3. Sideload

Teams → **Apps → Manage your apps → Upload a custom app** → chọn zip → add vào team/channel cần nhận nhắc và chat.

## 4. Điều kiện tiên quyết

- `teams_agent_enabled: true` + `teams_agent_app_id` / `teams_agent_app_secret` / `teams_agent_tenant_id` (config.yaml hoặc `.env`)
- `pip install .[teams-bot]`
- Azure Bot resource → **Messaging endpoint** trỏ tới `https://<PUBLIC_HOST>/api/messages`
- Bật kênh **Microsoft Teams** trên Azure Bot resource
- Dùng tab: dashboard phải truy cập được từ ngoài qua HTTPS. Dashboard có auth riêng (`dashboard_auth_password_hash` / `dashboard_auth_token`) và **không** có Teams SSO, nên tab sẽ hỏi đăng nhập một lần.

## Lệnh sau khi add

| Lệnh | Việc |
|---|---|
| `/help` | bảng lệnh |
| `/items` | work item của bạn |
| `/prs` | PR bạn là author/reviewer, kèm tình trạng vote |
| `/team` | tổng quan PR cả team, cũ nhất trước |
| `/queue` | việc autopilot đang chờ người xử lý |
| `/pr <repo> <pr-id>` | chi tiết bất kỳ PR |
| `/item <id>` | chi tiết bất kỳ work item |
| `/review <repo> <pr-id>` | bot review lại PR đó ngay |
| `/resume <id>` | tiếp tục một việc đang chờ (có xác nhận) |
| `/log <mô tả>` | tạo nhanh Requirement (có xác nhận) |
| `/status` | tình trạng hoạt động |

Ngoài ra: hỏi tiếng Việt tự nhiên, dán link PR (hoặc reply kèm quote một PR) rồi nói "review". Bot **chỉ đọc** qua chat — sửa code / vote / merge phải thao tác trên PR trong Azure DevOps.

> Danh sách này khớp `_COMMANDS` trong `ai_autopilot/teams_agent.py`. `commandLists` trong `manifest.json` là menu Teams hiển thị — nó **chỉ chứa 10 lệnh** vì schema Teams giới hạn `commands` tối đa 10 phần tử mỗi list (vượt là Teams từ chối cả package). Lệnh bị để ngoài menu là `/status`; `/help` vẫn liệt kê đủ 11. Thêm lệnh mới → sửa `_COMMANDS`, bảng này, và cân nhắc phải bỏ lệnh nào khỏi menu.

## Validate manifest trước khi upload

Bắt buộc — Teams từ chối cả package nếu manifest sai một chi tiết, và thông báo lỗi không luôn chỉ rõ chỗ:

```bash
curl -sL -o /tmp/teams.json \
  "https://developer.microsoft.com/en-us/json-schemas/teams/v1.19/MicrosoftTeams.schema.json"
python -c "
import json, jsonschema
jsonschema.validate(json.load(open('teams-app/manifest.json',encoding='utf-8')),
                    json.load(open('/tmp/teams.json',encoding='utf-8')))
print('manifest hợp lệ')"
```

Chạy trên **manifest đã điền placeholder** (bản template vẫn còn `<...>` sẽ báo lỗi ở `id` vì không phải GUID).

## Ghi chú kỹ thuật

- **Title lệnh phải có `/`.** Teams chèn nguyên văn title vào ô chat; bot dispatch theo `/lệnh`, nên title thiếu `/` sẽ không chạy lệnh nào (bản manifest trước bị lỗi này ở mọi lệnh trừ `help`).
- **`version`** đi theo version package (hiện `2.5.0`). Teams chỉ nhận bản mới khi số này tăng — sửa manifest mà quên bump thì upload sẽ bị coi là trùng.
- **Không có `webApplicationInfo`/SSO.** Bot chỉ giữ credential app-only, không bao giờ có token của từng người dùng — nên nó hành động với danh nghĩa CHÍNH NÓ (vd tự vote), không thể vote hộ người bấm nút. Khai báo SSO ở đây sẽ là sai sự thật.
- **`showLoadingIndicator`** cố ý không bật: bật thì trang tab phải gọi `notifySuccess()` của Teams JS SDK, mà dashboard không nhúng SDK → tab sẽ treo ở màn hình loading.
- **`permissions: ["identity", "messageTeamMembers"]`** là field legacy nhưng vẫn hợp lệ ở manifest 1.19. Cố ý giữ: `identity` là thứ cho phép `TeamsInfo.get_member` tra email để lọc `/items` `/prs` theo đúng người. Đổi sang RSC (`authorization.permissions`) mà chưa kiểm chứng có thể làm mất khả năng tra danh tính.
- `color.png` 192×192, `outline.png` 32×32 trong suốt — đúng chuẩn Teams. Đổi sang branding của bạn trước khi publish ra ngoài phạm vi sideload nội bộ.
