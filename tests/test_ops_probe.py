"""ops_probe — thresholds and the "could not check is not fine" exit code."""

from __future__ import annotations

import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "ops_probe", Path(__file__).parent.parent / "scripts" / "ops_probe.py")
probe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(probe)


def test_disk_thresholds():
    assert [probe.classify_disk(p) for p in (10, 79.9, 80, 89.9, 90, 100)] == [
        "ok", "ok", "warn", "warn", "critical", "critical"]


def test_cert_thresholds():
    assert [probe.classify_cert(d) for d in (90, 31, 30, 8, 7, -1)] == [
        "ok", "ok", "warn", "warn", "critical", "critical"]


def test_hostport():
    assert probe.parse_hostport("a.b") == ("a.b", 443)
    assert probe.parse_hostport("a.b:8443") == ("a.b", 8443)


def test_exit_codes(monkeypatch, capsys):
    monkeypatch.setattr(probe, "disk_used_pct", lambda _p: 95.0)
    assert probe.run(["--disk", "/x"]) == 2
    monkeypatch.setattr(probe, "disk_used_pct", lambda _p: 85.0)
    assert probe.run(["--disk", "/x"]) == 1
    monkeypatch.setattr(probe, "disk_used_pct", lambda _p: 10.0)
    assert probe.run(["--disk", "/x"]) == 0

    def boom(*_a, **_k):
        raise OSError("unreachable")

    monkeypatch.setattr(probe, "cert_days_left", boom)
    assert probe.run(["--cert", "h:443"]) == 3          # could not check is not "fine"
    assert "cannot check" in capsys.readouterr().out


def test_a_critical_is_not_downgraded_by_a_later_unknown(monkeypatch):
    monkeypatch.setattr(probe, "disk_used_pct", lambda _p: 99.0)
    monkeypatch.setattr(probe, "cert_days_left", lambda *_a, **_k: (_ for _ in ()).throw(OSError("x")))
    assert probe.run(["--disk", "/x", "--cert", "h"]) == 2
