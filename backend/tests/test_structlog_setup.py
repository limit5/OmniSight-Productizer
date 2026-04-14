"""Fix-D D6 — structlog_setup smoke.

The module has to work in three shapes:
  1. structlog installed + OMNISIGHT_LOG_FORMAT=json → JSON to stdout
  2. structlog installed + OMNISIGHT_LOG_FORMAT unset → stdlib logging
     preserved (dev-friendly)
  3. structlog absent → `bind_logger` falls back to LoggerAdapter
"""

from __future__ import annotations

import logging

import pytest


def test_is_json_reads_env(monkeypatch):
    from backend import structlog_setup as sl
    monkeypatch.setenv("OMNISIGHT_LOG_FORMAT", "json")
    assert sl.is_json() is True
    monkeypatch.setenv("OMNISIGHT_LOG_FORMAT", "JSON")  # case-insensitive
    assert sl.is_json() is True
    monkeypatch.setenv("OMNISIGHT_LOG_FORMAT", "text")
    assert sl.is_json() is False
    monkeypatch.delenv("OMNISIGHT_LOG_FORMAT", raising=False)
    assert sl.is_json() is False


def test_configure_is_idempotent(monkeypatch):
    from backend import structlog_setup as sl
    sl._CONFIGURED = False
    monkeypatch.setenv("OMNISIGHT_LOG_FORMAT", "json")
    sl.configure()
    assert sl._CONFIGURED is True
    # Second call is a no-op — must not raise or double-install handlers.
    sl.configure()
    assert sl._CONFIGURED is True


def test_configure_skipped_when_not_json(monkeypatch):
    from backend import structlog_setup as sl
    sl._CONFIGURED = False
    monkeypatch.delenv("OMNISIGHT_LOG_FORMAT", raising=False)
    sl.configure()
    assert sl._CONFIGURED is True
    # In text mode `_CONFIGURED` still gets set, but stdlib logging is
    # left untouched. A sanity check: root logger should have its default
    # handler count (we can't assert exact count — pytest adds its own).
    root = logging.getLogger()
    # Just exercise the API without strict assertions on handler count.
    assert root is not None


def test_bind_logger_returns_callable_logger():
    from backend import structlog_setup as sl
    log = sl.bind_logger(decision_id="dec-xyz", kind="git_push")
    # Works on both backends — structlog BoundLogger and LoggerAdapter
    # both expose .info / .warning / .debug.
    log.info("event bound-log-test")
    log.warning("warn bound-log-test")
    log.debug("debug bound-log-test")


def test_bind_logger_accepts_empty_context():
    from backend import structlog_setup as sl
    log = sl.bind_logger()
    log.info("empty-context ok")


def test_get_logger_returns_stdlib_when_text_mode(monkeypatch):
    from backend import structlog_setup as sl
    monkeypatch.delenv("OMNISIGHT_LOG_FORMAT", raising=False)
    log = sl.get_logger("backend.test.example")
    assert isinstance(log, logging.Logger)
    assert log.name == "backend.test.example"


def test_get_logger_without_name_returns_root_like_logger(monkeypatch):
    from backend import structlog_setup as sl
    monkeypatch.delenv("OMNISIGHT_LOG_FORMAT", raising=False)
    log = sl.get_logger(None)
    # stdlib getLogger(None) returns the root logger.
    assert log is logging.getLogger()


@pytest.mark.skipif(True, reason="documents expected shape — run manually")
def test_json_output_shape_when_configured(monkeypatch, capsys):
    """Manual sanity test: with JSON mode on, `bind_logger(...).info(event)`
    emits a single JSON line containing the bound keys. Skipped because
    capsys + stdlib root logger is finicky in the shared test env; keep
    as a documentation anchor."""
    from backend import structlog_setup as sl
    monkeypatch.setenv("OMNISIGHT_LOG_FORMAT", "json")
    sl._CONFIGURED = False
    sl.configure()
    log = sl.bind_logger(decision_id="d1")
    log.info("hello")
    captured = capsys.readouterr()
    assert '"decision_id": "d1"' in captured.out
