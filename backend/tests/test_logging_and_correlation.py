"""Observability tests (specification §21)."""

from __future__ import annotations

import json

import pytest

from app.core.config import Settings
from app.core.correlation import (
    CORRELATION_ID_HEADER,
    bind_log_context,
    clear_correlation_id,
    correlation_scope,
    get_correlation_id,
    new_correlation_id,
    set_correlation_id,
)
from app.core.logging import configure_logging, get_logger


def _log_lines(captured: str) -> list[dict[str, object]]:
    return [json.loads(line) for line in captured.strip().splitlines() if line.strip()]


class TestCorrelationId:
    def test_generates_when_absent(self) -> None:
        correlation_id = set_correlation_id()
        assert correlation_id
        assert get_correlation_id() == correlation_id
        clear_correlation_id()

    def test_adopts_provided_value(self) -> None:
        set_correlation_id("abc123")
        assert get_correlation_id() == "abc123"
        clear_correlation_id()

    def test_ids_are_unique(self) -> None:
        assert new_correlation_id() != new_correlation_id()

    def test_clear_removes_value(self) -> None:
        set_correlation_id("abc123")
        clear_correlation_id()
        assert get_correlation_id() is None


class TestCorrelationScope:
    def test_scope_isolates_and_restores(self) -> None:
        set_correlation_id("outer")
        with correlation_scope("inner") as inner:
            assert inner == "inner"
            assert get_correlation_id() == "inner"
        assert get_correlation_id() == "outer"
        clear_correlation_id()

    def test_scope_generates_when_no_value_given(self) -> None:
        with correlation_scope() as correlation_id:
            assert correlation_id


class TestStructuredLogging:
    def test_emits_json_with_correlation_id(
        self, settings: Settings, capsys: pytest.CaptureFixture[str]
    ) -> None:
        configure_logging(settings, force=True)
        set_correlation_id("trace-me")
        get_logger("test").info("something_happened", campaign_id="c-1")
        clear_correlation_id()

        lines = _log_lines(capsys.readouterr().out)
        assert len(lines) == 1
        record = lines[0]
        assert record["event"] == "something_happened"
        assert record["correlation_id"] == "trace-me"
        assert record["campaign_id"] == "c-1"
        assert record["level"] == "info"
        assert "timestamp" in record

    def test_binds_all_traceable_identifiers(
        self, settings: Settings, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """§21 lists the identifiers the platform must be traceable by."""
        configure_logging(settings, force=True)
        with correlation_scope("trace"):
            bind_log_context(
                task_id="t-1",
                lead_id="l-1",
                campaign_id="c-1",
                conversation_id="cv-1",
                booking_id="b-1",
            )
            get_logger("test").info("traced")

        record = _log_lines(capsys.readouterr().out)[0]
        for key in ("task_id", "lead_id", "campaign_id", "conversation_id", "booking_id"):
            assert key in record

    def test_none_values_are_not_bound(
        self, settings: Settings, capsys: pytest.CaptureFixture[str]
    ) -> None:
        configure_logging(settings, force=True)
        with correlation_scope("trace"):
            bind_log_context(lead_id=None, campaign_id="c-1")
            get_logger("test").info("partial")

        record = _log_lines(capsys.readouterr().out)[0]
        assert "lead_id" not in record
        assert record["campaign_id"] == "c-1"

    def test_console_format_is_not_json(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("LOG_FORMAT", "console")
        configure_logging(Settings(), force=True)
        get_logger("test").info("human_readable")
        captured = capsys.readouterr().out
        assert "human_readable" in captured
        with pytest.raises(json.JSONDecodeError):
            json.loads(captured.strip().splitlines()[0])

    def test_exception_includes_traceback(
        self, settings: Settings, capsys: pytest.CaptureFixture[str]
    ) -> None:
        configure_logging(settings, force=True)
        try:
            raise RuntimeError("boom")
        except RuntimeError:
            get_logger("test").exception("it_broke")

        record = _log_lines(capsys.readouterr().out)[0]
        assert record["level"] == "error"
        assert "RuntimeError: boom" in str(record.get("exception", ""))


class TestHeaderName:
    def test_header_constant(self) -> None:
        assert CORRELATION_ID_HEADER == "X-Correlation-ID"
