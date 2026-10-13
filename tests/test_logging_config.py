"""configure_logging: where the file sink goes, and when there is none."""

from __future__ import annotations

import logging
import logging.handlers

from ai_autopilot.logging_config import configure_logging


def _file_handlers() -> list[logging.Handler]:
    return [h for h in logging.getLogger().handlers
            if isinstance(h, logging.handlers.TimedRotatingFileHandler)]


def test_empty_log_dir_env_turns_the_file_sink_off(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AUTOPILOT_LOG_DIR", "")
    configure_logging()
    assert _file_handlers() == []
    assert not (tmp_path / "logs").exists()


def test_log_dir_env_picks_the_folder(monkeypatch, tmp_path):
    monkeypatch.setenv("AUTOPILOT_LOG_DIR", str(tmp_path / "out"))
    configure_logging()
    try:
        assert [h.baseFilename for h in _file_handlers()] == [
            str(tmp_path / "out" / "autopilot.log")]
    finally:
        monkeypatch.setenv("AUTOPILOT_LOG_DIR", "")
        configure_logging()  # close the handle so tmp_path can be removed on Windows


def test_reconfiguring_does_not_stack_file_handlers(tmp_path):
    configure_logging(log_dir=str(tmp_path))
    configure_logging(log_dir=str(tmp_path))
    try:
        assert len(_file_handlers()) == 1
    finally:
        configure_logging(log_dir="")
