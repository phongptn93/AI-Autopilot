# Eval suite — regression tests for the agent's configuration

This repo has over a thousand tests for its Python and, until this directory existed,
none at all for the thing that decides what the agent actually produces: the skills,
rules and `CLAUDE.md` under `.claude/`. Editing one of those changes the product's
behaviour, and the only way to find out whether it changed for the better was to ship it.

An eval is a real task plus the checks that say what an acceptable answer looks like.

```bash
ai-autopilot evals                       # run every case, demand 100%
ai-autopilot evals evals --min-pass-rate 0.9
```

Non-zero exit when the pass rate is under the threshold, so CI can gate a change to
`.claude/` the way it gates a change to code.

## Writing a case

One YAML file per case (or a list of cases in one file):

```yaml
name: bugfix-workflow-writes-the-failing-test-first
prompt: |
  Read .claude/skills/bugfix-workflow/SKILL.md and answer in plain text:
  at which step does it tell you to write the failing test?
checks:
  - kind: contains
    value: reproduce
  - kind: not_contains
    value: I could not find
```

| Field | Meaning |
|---|---|
| `name` | Shown in the report. Defaults to the filename. |
| `prompt` | The task, exactly as a person would ask it. |
| `cwd` | Where the agent works. Blank = the case file's own directory. |
| `allowed_tools` | Restrict the tool surface. Omit for the default. |
| `timeout_seconds` | Per case. Default 900. |
| `checks` | What must hold. **A case with no checks fails.** |

### Check kinds

| Kind | Holds when |
|---|---|
| `contains` / `not_contains` | `value` appears (or does not) in the output |
| `regex` | `value` matches the output |
| `file_exists` / `file_absent` | `value` is a path under the case's cwd |
| `file_contains` | `path` exists and holds `value` |
| `shell` | `value` runs in the cwd and exits zero |

Text comparison is case-insensitive.

## The rule that keeps the suite worth running

**Assert facts, never style.** Text that must appear, a file that must exist, a command
that must exit zero — those survive a model upgrade. "Is the wording good" does not, and
an eval that fails for the wrong reason is worse than no eval, because the team learns
to ignore it.

Two consequences worth stating:

- **A case with no checks fails.** "The agent said something" is not a standard, and a
  case asserting nothing would quietly lift the pass rate while testing nothing.
- **An empty suite scores 0%, not 100%.** A suite that loaded nothing has proved
  nothing, and reporting that as a clean run is how a bad path becomes a green gate
  checking air.

## Where cases come from

The playbook's advice, which holds here: collect real tasks from recent work, and give
every production incident an eval that stays in the suite as a regression test. A case
written the day something broke is the one that stops it breaking twice.

## Replay evals — đề thi từ việc thật

Các case ở trên hỏi agent về chính rule của nó — bắt được một skill *không còn nói đúng*,
nhưng không bắt được một skill vẫn nói đúng mà *code ra không chạy*. Replay chấm thẳng
bằng công việc: lấy một thay đổi người thật đã merge, quay repo về commit ngay trước đó,
giao lại đúng task cho agent, rồi so kết quả.

Mọi thứ dùng để chấm đều là sự thật repo đã có sẵn — commit bắt đầu, các file người đã
sửa, lệnh test — nên không phải bịa ra tiêu chí "thế nào là tốt".

```bash
# 1. Sinh case từ git history (commit/merge có nhắc #1234 hoặc AB#1234)
ai-autopilot evals harvest --repo Backend-Fresh --limit 20 --out evals/replay/

# 2. Chạy lại toàn bộ (hoặc một case), yêu cầu ≥ 70%, xuất JSON cho CI
ai-autopilot evals replay evals/replay --min-pass-rate 0.7 --json out/replay.json
ai-autopilot evals replay --case backend-fresh-wi1234-1a2b3c4d
```

### Harvest

- Đi theo **first-parent history** (mainline đúng như lúc merge), giữ commit nào có
  message nhắc tới work item (`#1234`, `AB#1234`) — merge commit hay squash đều được.
- `base_commit` = first parent; `expected_files` = các file thay đổi so với nó;
  `expected_diff_lines` = số dòng +/- (chỉ để tham khảo quy mô).
- Một case cho mỗi work item (commit mới nhất thắng — commit sau cùng item là follow-up).
- Task text = commit message (bỏ tiền tố `Merged PR 123:`). **Chỉ khi** đã cấu hình
  ADO (`ado_organization` + `ado_pat`) mới lấy thêm title/description/AC của work item;
  lỗi mạng hay thiếu quyền thì quay về commit message, không bao giờ làm harvest fail.
  `--no-tracker` để tắt hẳn.
- File đã có **được giữ nguyên** (case harvest ra là bản nháp để người sửa); dùng
  `--overwrite` nếu muốn ghi đè.

### Định dạng case

| Field | Ý nghĩa |
|---|---|
| `kind` | `replay` — phân biệt với case cấu hình (hai loại có thể chung thư mục). |
| `name` | Tên hiển thị trong report; `--case` lọc theo tên này. |
| `repo` | Tên thư mục repo dưới workspace (hoặc đường dẫn tuyệt đối). |
| `base_commit` | Commit để dựng worktree — trước khi người làm task. |
| `task.title` / `task.description` | Nội dung task (title + mô tả/AC của work item gốc). Có thể viết `task:` là một khối text. |
| `test_command` | Tuỳ chọn. Để trống = lệnh test cấu hình cho repo (`test_commands`), nếu không có thì tự nhận diện như test gate. |
| `expected_files` | Các file PR của người đã sửa. |
| `timeout_minutes` | Trần thời gian cho agent (và cho lệnh test). Mặc định 30. |
| `example` | `true` = chỉ là ví dụ, bị bỏ qua trừ khi chạy với `--include-examples`. |

Xem [`replay/example.yaml`](replay/example.yaml).

### Cách chạy một case

1. Tạo **worktree tạm** (detached) tại `base_commit` trong thư mục temp — luôn bị xoá
   trong `finally`, kể cả khi agent crash hay timeout.
2. Chạy agent headless (cùng entry với eval cấu hình, có nạp `.claude` của project) với
   brief nêu task và cấm push / tạo branch / tạo PR; `git push` còn bị chặn ở tool.
3. `git add -A` rồi diff index với `base_commit` — tính cả file mới và thứ agent đã commit.
4. Chấm điểm bằng hàm thuần `replay_eval.score_case(...)`.

### Tiêu chí PASS

Một case PASS khi **tất cả** đều đúng:

| Tiêu chí | Ghi chú |
|---|---|
| Agent có sửa ít nhất một file | Không sửa gì = không làm task. |
| Test không `failed` / `timeout` | `skipped` (không có runner trên máy) **không** tính là fail — máy thiếu .NET không nói gì về thay đổi. |
| Không còn conflict marker | Dòng bắt đầu bằng `<<<<<<<` / `>>>>>>>`. |
| Có ít nhất một file trùng với PR của người | Trùng 0 file = giải một bài khác. |

**Overlap** (Jaccard file agent sửa vs `expected_files`) và **diff size** được báo cáo nhưng
không chặn: hai cách sửa đúng cho cùng một bug thường đụng các file phụ khác nhau, và
một ngưỡng kích thước sẽ phạt agent vì viết thêm test mà người đã bỏ qua.

Replay rỗng (không case nào chạy) = 0%, exit khác 0 — giống suite cấu hình.

### Lưu ý

- Mỗi case là một phiên agent đầy đủ trên cả repo: tốn token và thời gian. Chạy tuần tự.
- Repo phải có sẵn `base_commit` (fetch đủ history). Worktree dùng chung object store,
  không clone lại.
- Một repo mà test cần dependency (node_modules, NuGet restore) thì đặt `test_command`
  gồm cả bước cài đặt.
