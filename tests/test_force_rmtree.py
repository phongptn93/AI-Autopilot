"""Tests for the Windows-safe scratch teardown (read-only git files, stale handles)."""

from __future__ import annotations

import os
import stat
from pathlib import Path

from ai_autopilot.execution.claude_executor import _force_rmtree


async def test_deletes_tree_with_read_only_files(tmp_path):
    # Mirror what actually breaks on Windows: git marks object/pack files read-only,
    # so a plain rmtree raises "Permission denied".
    wt = tmp_path / "Backend-Fresh"
    (wt / ".git" / "objects" / "pack").mkdir(parents=True)
    pack = wt / ".git" / "objects" / "pack" / "pack-abc.pack"
    pack.write_bytes(b"data")
    os.chmod(pack, stat.S_IREAD)
    (wt / "src.py").write_text("x = 1", encoding="utf-8")

    assert await _force_rmtree(wt) is True
    assert not wt.exists()


async def test_missing_path_is_a_noop(tmp_path):
    assert await _force_rmtree(tmp_path / "gone") is True


async def test_plain_tree_is_deleted(tmp_path):
    d = tmp_path / "scratch"
    (d / "sub").mkdir(parents=True)
    (d / "sub" / "f.txt").write_text("hi", encoding="utf-8")
    assert await _force_rmtree(d) is True
    assert not Path(d).exists()
