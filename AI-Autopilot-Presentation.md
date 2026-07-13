# AI Autopilot — Nội dung slide giới thiệu

> Từ Work Item đến Pull Request, tự động — con người vẫn kiểm soát.

---

## Slide 1 — Bìa
**AI Autopilot**
*Từ Work Item đến Pull Request, tự động*

- Người trình bày: Phong Pham
- [Tên công ty / Team] · [Ngày]

**Speaker note:** Chào mọi người, hôm nay mình giới thiệu một hệ thống mình đang xây: một "kỹ sư AI" tự nhận việc trên Azure DevOps và tạo Pull Request — nhưng luôn dưới sự kiểm soát của con người.

---

## Slide 2 — Vấn đề
**Chúng ta đang mất thời gian ở đâu?**

- Nhiều task nhỏ, lặp lại: sửa bug đơn giản, CRUD, config, chỉnh UI…
- Kỹ sư giỏi bị "kẹt" vào việc cơ bản → chậm việc quan trọng
- Task nằm chờ trong backlog, phản hồi chậm cho stakeholder

> ❓ *Nếu có một "kỹ sư ảo" nhận việc 24/7, làm phần cơ bản, để người tập trung việc khó — thì sao?*

**Speaker note:** Đặt vấn đề gần gũi với đội. Nhấn "phần cơ bản" — không phải thay thế, mà giải phóng thời gian.

---

## Slide 3 — AI Autopilot là gì
**Một trợ lý kỹ thuật tự động, có giám sát**

Vòng đời 1 câu:
> Gắn tag → Autopilot nhận → Claude phân tích & code → mở PR → **bạn review & merge**

- Tích hợp thẳng **Azure DevOps** (work item + repo + PR)
- Chạy bằng **Claude** (Anthropic) — mô hình mạnh nhất hiện nay
- **Con người luôn ở giữa vòng lặp** (human-in-the-loop)

**Speaker note:** Thông điệp cốt lõi của cả buổi. Lặp lại "con người kiểm soát" để tránh lo ngại.

---

## Slide 4 — Cách hoạt động (luồng)
**Từ tag đến PR — 5 bước**

1. **Poll** — quét ADO tìm work item gắn tag trigger
2. **Phân loại & ưu tiên** — BE/FE/Bug/DB… + độ ưu tiên
3. **Thực thi** — Claude làm việc trong **worktree cô lập** (không đụng code chính)
4. **Cập nhật trạng thái** — tự chuyển state theo từng giai đoạn
5. **Kết quả** — tạo Pull Request, hoặc **báo cần người** nếu mơ hồ

**Speaker note:** Dùng 1 sơ đồ mũi tên ngang. Nhấn bước 3 (cô lập) và bước 5 (biết dừng lại khi không chắc).

---

## Slide 5 — Kiến trúc (đơn giản hoá)
**Các thành phần chính**

- **Poller** — nhịp tim, quét & điều phối task (retry, lịch chạy, RBAC)
- **Executor** — chạy Claude, quản lý git/worktree, tạo PR
- **Dashboard** — Board realtime, Overview, Settings, activity feed
- **ADO Client / Notifier** — đọc/ghi work item, comment, state
- **Notifications** — Teams / Email / Zalo

> Chạy được cả **headless** (tự động hoàn toàn) và **interactive** (bạn attach vào steer).

**Speaker note:** Không đi sâu kỹ thuật với management; với kỹ sư thì đây là chỗ trả lời "nó gồm gì".

---

## Slide 6 — 🔴 DEMO TRỰC TIẾP
**Xem nó chạy thật**

Kịch bản demo:
1. Tạo/chọn 1 work item → gắn tag `autopilot`
2. Mở **Dashboard → Board**: card di chuyển realtime qua các cột
3. Mở **activity feed**: xem agent suy nghĩ & thao tác live
4. Kết quả: **Pull Request** được tạo + state ADO tự chuyển sang *In review*

> 💡 Chuẩn bị sẵn 1 task "chạy nhanh" + video/GIF backup phòng lỗi mạng.

**Speaker note:** Đây là điểm nhấn. Nói ít, cho xem nhiều. Nếu live rủi ro → chạy clip đã quay.

---

## Slide 7 — Điểm mạnh nổi bật
**Vì sao đáng dùng**

- 🔒 **Cô lập theo task (git worktree)** — nhiều task chạy song song, *không đụng checkout chính của bạn*
- 🎛 **Cấu hình theo board của bạn** — chọn trigger state / tag / state từng giai đoạn ngay trên UI
- 🏷 **Nhiều luồng (tag)** — mỗi team/máy một stream riêng, lọc trên Board/Overview
- 📤 **Export/Import cấu hình** — nhân rộng cho cả team trong 1 phút (không kèm mật khẩu)
- 🎚 **3 mức tự chủ** — Report / Assisted (draft PR) / Unattended (auto PR)

**Speaker note:** Chọn 2–3 điểm khán giả quan tâm nhất mà nhấn, đừng đọc hết.

---

## Slide 8 — Kiểm soát & An toàn
**AI hỗ trợ, không thay quyết định**

- ✅ **Draft PR** — luôn chờ người review trước khi merge
- 🙋 **Tự escalate** — mơ hồ/thiếu thông tin thì *dừng và gắn "cần người"*, không đoán bừa
- 🔍 **Auto security review** trước khi mở PR
- 🚫 **Không tự merge**, **không xoá dữ liệu**
- 🧪 **Dry-run** — chạy thử, chỉ log, không ghi gì
- 👤 **RBAC** — chỉ xử lý việc từ người được phép

**Speaker note:** Slide "trấn an". Rất quan trọng với management và security. Nói chậm, rõ.

---

## Slide 9 — Kết quả kỳ vọng
**Giá trị mang lại**

- ⏱ Rút ngắn thời gian cho task nhỏ/lặp lại
- 🚀 Phản hồi nhanh hơn cho backlog & stakeholder
- 🧠 Giải phóng kỹ sư giỏi cho việc phức tạp
- 📈 Chuẩn hoá quy trình (branch, PR, review) tự động

> *(Điền số liệu thật nếu có: thời gian TB/task, số PR đã tạo, tỉ lệ cần người.)*

**Speaker note:** Nếu chưa có số liệu, nói thẳng "đang đo" — trung thực tạo uy tín hơn phóng đại.

---

## Slide 10 — Lộ trình
**Tiếp theo là gì**

- **Ngắn hạn:** chạy thử thực tế trên vài task/loại việc chọn lọc
- **Trung hạn:** mở rộng loại task, nhiều team/tag, tinh chỉnh chất lượng
- **Dài hạn:** tối ưu chi phí, đo lường hiệu quả, tích hợp sâu quy trình

**Speaker note:** Kêu gọi hành động: xin 1–2 team pilot, xin loại task cụ thể để bắt đầu.

---

## Slide 11 — Q&A
**Câu hỏi & Thảo luận**

Chuẩn bị sẵn:
- *Nếu AI làm sai thì sao?* → Draft PR + review + auto security review; không tự merge.
- *Dữ liệu/PAT bảo mật thế nào?* → Token không bao giờ export; chạy nội bộ.
- *Chạy được bao nhiêu task cùng lúc?* → Cấu hình được; mỗi task cô lập worktree.
- *Áp dụng loại task nào trước?* → Bắt đầu từ task nhỏ, rõ acceptance criteria.

**Cảm ơn! — Phong Pham**

**Speaker note:** Kết bằng lời mời pilot cụ thể, để lại contact.
