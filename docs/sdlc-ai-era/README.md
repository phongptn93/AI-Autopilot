# Gói quy trình phát triển phần mềm thời đại AI (v1.2)

Gói tài liệu và công cụ để áp dụng một quy trình phát triển có agent tham gia: **BA → Operator + Đội Agent → SA review → QC → nghiệm thu**, có cổng chấm điểm, bằng chứng bắt buộc và bảo mật ở mọi giai đoạn.

> **Trạng thái:** đề xuất có lập luận, **chưa được kiểm chứng bằng dữ liệu chạy thật**. Đối chiếu với AI-Autopilot dựa trên đọc mã, cấu hình và 92 test của các mô-đun cổng (đều pass); hệ thống chưa được chạy đầu cuối và `autopilot.db` trên máy rà soát không có lượt chạy nào. Dùng pilot (`templates/06-ke-hoach-pilot.md`) để biến nó thành bằng chứng.

## Bắt đầu từ đâu

| Bạn là | Đọc |
|---|---|
| Người quyết định (PM, lãnh đạo) | `handbook.html` mục 1 (giả thuyết), 14 (AI-Autopilot có phù hợp không), 15 (đã kiểm chứng đến đâu), 18 (áp dụng) |
| BA | `templates/01-prd-nhe.md`, `02-srs.md`, `05-nghiem-thu.md`; handbook mục 6 (spec) |
| Operator / Dev | `templates/03-plan.md`, `04-bang-chung.md`; `gates/`; handbook mục 10 |
| SA | handbook mục 4, 9, 14; `templates/03-plan.md` (phần review giải pháp) |
| QC | handbook mục 7 (artifact), cổng G4 trong `gates/gate-scorecard.json` |

## Nội dung

```
sdlc-ai-era/
├── README.md                    (tệp này)
├── handbook.html                Tài liệu chính, 18 mục, mở bằng trình duyệt
├── diagrams/                    4 sơ đồ A3 (SVG, in khổ 420×297 mm)
│   ├── flow-A3.svg              Luồng 5 giai đoạn + hàng bảo mật + cổng
│   ├── roles-A3.svg             Phân cấp vai trò: quyết định / làm / chịu trách nhiệm
│   ├── artifacts-A3.svg         Bản đồ artifact, spec hai lớp ở trung tâm
│   └── gates-A3.svg             Cơ chế chấm điểm cổng (sinh từ scorecard)
├── gates/
│   ├── gate-scorecard.json      Cấu hình: tiêu chí, trọng số, hard-fail, ngưỡng theo tầng
│   ├── gate_score.py            Bộ tính điểm (thư viện chuẩn Python)
│   ├── test_gate_score.py       17 test
│   └── examples/                4 ví dụ: CONFIRM, REVIEW, BLOCK
└── templates/
    ├── 01-prd-nhe.md            Lớp 1 của spec: khách xác nhận
    ├── 02-srs.md                Lớp 2 của spec: agent soạn nháp, BA rà
    ├── 03-plan.md               Plan + phân loại rủi ro + SA review
    ├── 04-bang-chung.md         Danh mục bằng chứng theo cổng
    ├── 05-nghiem-thu.md         Biên bản nghiệm thu
    └── 06-ke-hoach-pilot.md     Kế hoạch pilot có đối chứng
```

## Chạy bộ tính điểm cổng

Chỉ cần Python 3.10 trở lên, không cần cài thêm gói.

```bash
cd gates
python gate_score.py gate-scorecard.json examples/g3-yellow-review.json
# G3 | tầng yellow | điểm 78/100 | quyết định: REVIEW

python -m unittest test_gate_score -v
```

Mã thoát: `0` = PASS hoặc CONFIRM, `1` = REVIEW, `2` = BLOCK.

Input là JSON: `{"gate": "G3", "tier": "yellow", "criteria": {"build_tests": {"status": "pass", "evidence": "pipeline#4821"}, ...}}`. Tiêu chí `pass` / `partial` thiếu `evidence` tự bị hạ xuống `unknown`.

## Tái tạo sơ đồ

`diagrams/gates-A3.svg` được sinh từ `gates/gate-scorecard.json`, nên khi đổi trọng số hoặc ngưỡng, sơ đồ phải sinh lại để không lệch. Ba sơ đồ còn lại được vẽ tay bằng script riêng (không nằm trong gói); sửa trực tiếp SVG khi cần.

## Giới hạn đã biết

- Ngưỡng 90/70 (Vàng) và 80 (Đỏ) là đề xuất ban đầu; chỉ ngưỡng 85/60 của tầng Xanh khớp cấu hình hiện có của AI-Autopilot (`pr_score_auto_min`, `pr_score_review_min`).
- Scorecard là **đặc tả** cho phần điểm cổng mà `pr_scorer.py` chưa có (tuân thủ spec, bảo mật). Gói không sửa mã AI-Autopilot.
- Chưa kiểm chứng: nguồn test case (từ spec hay từ code), phòng thủ prompt injection, hành vi hệ thống đầu cuối.
