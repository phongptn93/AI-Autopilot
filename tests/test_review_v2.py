"""Review Studio v2: the code version it reads, document quality, challenges, workspaces."""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

from starlette.testclient import TestClient

from ai_autopilot import review_snapshot
from ai_autopilot import review_studio as rs
from ai_autopilot.app import create_app
from ai_autopilot.config import Settings
from ai_autopilot.services import review_studio as svc_mod


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True,
                   env=None)


def _repo_with_stale_checkout(tmp_path: Path) -> Path:
    """origin has a commit the local checkout has fetched but not pulled."""
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "--bare", "-b", "main", str(origin))
    ws = tmp_path / "ws"
    ws.mkdir()
    _git(ws, "clone", str(origin), "Backend")
    be = ws / "Backend"
    for k, v in (("user.email", "t@t"), ("user.name", "t")):
        _git(be, "config", k, v)
    (be / "Old.cs").write_text("old", "utf-8")
    _git(be, "add", ".")
    _git(be, "commit", "-m", "old")
    _git(be, "push", "-u", "origin", "main")
    # someone else ships a feature
    other = tmp_path / "other"
    _git(tmp_path, "clone", str(origin), str(other))
    for k, v in (("user.email", "o@o"), ("user.name", "o")):
        _git(other, "config", k, v)
    (other / "CopyMachine.cs").write_text("[HttpPost(\"copy-machine\")]", "utf-8")
    _git(other, "add", ".")
    _git(other, "commit", "-m", "feat: copy machine")
    _git(other, "push")
    (ws / "CLAUDE.md").write_text("rules", "utf-8")
    return ws


def test_latest_reads_the_upstream_tip_without_touching_the_checkout(tmp_path):
    ws = _repo_with_stale_checkout(tmp_path)
    trees = tmp_path / "trees"
    cwd, states = review_snapshot.prepare(str(ws), trees, 7, latest=True)
    st = states[0]
    assert st.source == "upstream" and st.behind == 1 and st.subject == "feat: copy machine"
    assert (Path(cwd) / "Backend" / "CopyMachine.cs").exists()
    assert (Path(cwd) / "CLAUDE.md").exists()                         # team rules carried
    assert not (ws / "Backend" / "CopyMachine.cs").exists()           # checkout untouched
    review_snapshot.remove_tree(Path(cwd))
    assert not Path(cwd).exists()
    out = subprocess.run(["git", "worktree", "list"], cwd=ws / "Backend", capture_output=True,
                         text=True).stdout
    assert "review-7" not in out                                      # pruned


def test_checkout_mode_reads_the_disk_and_says_it_is_behind(tmp_path):
    ws = _repo_with_stale_checkout(tmp_path)
    _git(ws / "Backend", "fetch")
    cwd, states = review_snapshot.prepare(str(ws), tmp_path / "t", 8, latest=False)
    assert cwd == str(ws) and states[0].source == "checkout" and states[0].behind == 1


def test_inspect_lists_repos_for_the_hub(tmp_path):
    ws = _repo_with_stale_checkout(tmp_path)
    _git(ws / "Backend", "fetch")
    [st] = review_snapshot.inspect(str(ws))
    assert st.repo == "Backend" and st.branch == "main" and st.behind == 1


# ── document quality + "code is better" ──────────────────────────────────────

_REPLY = '''```json
{"verdict": "v", "traces": [
  {"ref": "FR-1", "status": "done"},
  {"ref": "FR-2", "status": "better", "code": "chặn sửa năm đã khoá", "side": "update_doc",
   "confidence": "high"},
  {"ref": "FR-3", "status": "missing", "searched": "route copy-machine, CopyMachine, controller",
   "confidence": "medium"},
  {"ref": "FR-4", "status": "partial"}],
 "doc_quality": {"score": 62, "verdict": "Thiếu tiêu chí nghiệm thu đo được",
   "strengths": ["Phân rã FR rõ"],
   "issues": [{"ref": "FR-2", "kind": "code_better", "text": "Tài liệu cho sửa năm đã khoá",
               "suggestion": "Ghi rõ: năm đã khoá chỉ xem"},
              {"ref": "AC-1", "kind": "nonsense", "text": "mơ hồ"}]}}
```'''


def test_better_counts_as_built_and_doc_quality_is_parsed():
    r, _ = rs.parse(_REPLY, rs.KIND_SPEC)
    assert r.score(rs.KIND_SPEC) == 62.5                      # (done + better + ½) / 4
    assert r.traces[1].side == "update_doc" and r.traces[2].searched.startswith("route")
    assert r.doc_quality["score"] == 62 and r.doc_quality["issues"][1]["kind"] == "other"
    assert rs.gaps(r, rs.KIND_SPEC) == [2, 3]                 # "better" is a doc fix, not a gap
    md = rs.render_markdown("T", rs.KIND_SPEC, r)
    assert "## Đánh giá tài liệu — 62/100" in md and "(Cập nhật tài liệu)" in md
    html = rs.render_ado_comment("T", r)
    assert "Đánh giá tài liệu" in html and "Code đã làm tốt hơn" in html


def test_prompt_is_critical_of_the_document_and_demands_a_search():
    p = rs.spec_prompt("x.md", "FR-1 ...")
    assert "KHÔNG mặc nhiên" in p and "better" in p and "searched" in p and ".aiwt" in p


def test_challenge_updates_the_row_and_keeps_history():
    r, _ = rs.parse(_REPLY, rs.KIND_SPEC)
    reply = ('Đã tìm lại, có ở nhánh mới.\n```json\n{"verdict": "accept", "reason": "Có endpoint",'
             ' "status": "done", "code": "POST copy-machine", "evidence": [{"file": '
             '"B/MachineServicingController.cs", "line": 39}], "confidence": "high"}\n```')
    prose, entry = rs.apply_challenge(r, 2, reply, rs.KIND_SPEC, note="đã có rồi", by="ba")
    t = r.traces[2]
    assert t.status == "done" and t.evidence[0].line == 39 and t.confidence == "high"
    assert (entry["before"], entry["after"], entry["verdict"]) == ("missing", "done", "accept")
    assert t.challenges[0]["note"] == "đã có rồi" and prose.startswith("Đã tìm lại")
    # a plain-text answer changes nothing but is still recorded
    rs.apply_challenge(r, 3, "không chắc", rs.KIND_SPEC, note="?")
    assert r.traces[3].status == "partial" and len(r.traces[3].challenges) == 1


# ── end to end: workspace choice + challenge ─────────────────────────────────

def test_review_reads_the_chosen_workspace_and_a_challenge_rechecks(tmp_path, monkeypatch):
    a, b = tmp_path / "wsA", tmp_path / "wsB"
    for d, name in ((a, "Spec A"), (b, "Spec B")):
        (d / "specs" / "f").mkdir(parents=True)
        (d / "specs" / "f" / "s.md").write_text(f"# {name}\nFR-1 ...", "utf-8")
    cfg = Settings(dry_run=True, workspace_directory=str(a), review_code_source="checkout",
                   workspaces=[{"name": "Team B", "workspace_directory": str(b),
                                "ado_projects": ["PB"]}],
                   database_url=f"sqlite+aiosqlite:///{tmp_path / 'w.db'}")
    seen = []

    async def fake(self, prompt, *, resume=None, turns=None, cwd=None):
        seen.append((prompt, cwd, resume))
        if "PHẢN BIỆN" in prompt:
            text = '```json\n{"verdict": "accept", "reason": "ok", "status": "done"}\n```'
        else:
            text = _REPLY
        return SimpleNamespace(text=text, session_id="s", cost_usd=0.1, is_error=False)

    monkeypatch.setattr(svc_mod.ReviewStudio, "_agent", fake)
    with TestClient(create_app(cfg)) as client:
        ws_b = next(w.id for w in svc_mod.studio_for(client.app.state.container).workspaces()
                    if w.directory == str(b))
        hub = client.get("/dashboard/review").text            # first workspace by default
        assert "Team B" in hub and "Spec A" in hub and "Spec B" not in hub
        hub_b = client.get(f"/dashboard/review?ws={ws_b}").text
        assert "Spec B" in hub_b and "Spec A" not in hub_b
        assert f'name="workspace" value="{ws_b}"' in hub_b      # forms carry the page's ws
        r = client.post("/dashboard/review/start", data={
            "kind": "spec", "target": f"{ws_b}|specs/f/s.md"}, follow_redirects=False)
        rid = int(r.headers["location"].rsplit("/", 1)[1])
        for _ in range(100):
            if client.get(f"/dashboard/review/{rid}/status").json()["status"] == "done":
                break
            asyncio.run(asyncio.sleep(0.02))
        assert "Spec B" in seen[0][0] and seen[0][1] == str(b)        # read workspace B
        page = client.get(f"/dashboard/review/{rid}").text
        assert "Code tốt hơn tài liệu" in page and "Đánh giá tài liệu" in page
        assert "⚖️ Phản biện" in page and "Team B" in page
        # history is per workspace too
        assert f"/dashboard/review/{rid}" in client.get(f"/dashboard/review?ws={ws_b}").text
        assert f"/dashboard/review/{rid}\"" not in client.get("/dashboard/review").text

        client.post(f"/dashboard/review/{rid}/challenge",
                    data={"challenge_row": "2", "note_2": "đã có ở nhánh development",
                          "row": ["1", "3"]})
        for _ in range(100):
            if not client.get(f"/dashboard/review/{rid}/status").json()["pending"]:
                break
            asyncio.run(asyncio.sleep(0.02))
        assert seen[-1][2] == "s" and seen[-1][1] == str(b)           # same session, same ws
        stored = json.loads(client.get(f"/dashboard/review/{rid}/result.json").text)["result"]
        assert stored["traces"][2]["status"] == "done"
        assert stored["traces"][2]["challenges"][0]["note"] == "đã có ở nhánh development"
        assert "Chấp nhận phản biện" in client.get(f"/dashboard/review/{rid}").text


def test_an_accepted_challenge_clears_the_stale_fix():
    r = rs.ReviewResult(traces=[rs.Trace(ref="FR-4", status="missing", fix="Thêm gửi mail")])
    rs.apply_challenge(r, 0, '```json\n{"verdict": "accept", "status": "done"}\n```',
                       rs.KIND_SPEC, note="đã có")
    assert r.traces[0].status == "done" and r.traces[0].fix == ""
