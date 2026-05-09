"""OP-822 — tests for the SDK runner failure-pattern detector.

The acceptance gate is the synthetic-fixture test: 100 fixture run-log
rows are generated with 3 planted failure patterns (a backend/security
+ max_iterations cluster, a tier:X + max_tokens cluster, a frontend/
components/* error cluster) plus a noise band of healthy runs. The
detector MUST surface all 3 in its top-3 output for the audit to be
trustworthy in the field.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from scripts.sdk_failure_pattern_audit import (
    DEFAULT_MIN_FAILURE_RATE,
    RunLogEntry,
    detect_patterns,
    load_entries,
    parse_entry,
    render_markdown,
    _path_prefixes,
    _stop_reason_from_error,
    _suggest_mitigation,
)


# ─── Fixture builders ────────────────────────────────────────────────


def _row(
    ticket_key: str,
    *,
    status: str = "ok",
    stop_reason: str | None = None,
    tier: str | None = None,
    area: list[str] | None = None,
    files_touched: list[str] | None = None,
    agent_class: str | None = "subscription-claude",
    error: str | None = None,
    iterations: int = 5,
    cost_usd: float = 0.10,
) -> dict:
    """Build one JSONL row in the launcher's TicketOutcome shape (extended)."""
    return {
        "ticket_key": ticket_key,
        "status": status,
        "stop_reason": stop_reason,
        "tier": tier,
        "area": area or [],
        "files_touched": files_touched or [],
        "agent_class": agent_class,
        "error": error,
        "iterations": iterations,
        "cost_usd": cost_usd,
        "input_tokens": 1000,
        "output_tokens": 200,
        "started_at": "2026-05-09T00:00:00+00:00",
        "finished_at": "2026-05-09T00:01:00+00:00",
    }


def _planted_dataset() -> list[dict]:
    """100 rows with three planted patterns + noise.

    Pattern A (40 rows): backend/security/* + max_iterations_exceeded.
        - 36 fail, 4 succeed → 90% failure rate.
    Pattern B (20 rows): tier=X + max_tokens.
        - 18 fail, 2 succeed → 90% failure rate.
    Pattern C (15 rows): frontend/components/* + status=failed.
        - 14 fail, 1 succeed → 93% failure rate.
    Noise (25 rows): mixed clean runs.
    """
    rows: list[dict] = []
    next_id = 100

    # Pattern A — backend/security + max_iterations_exceeded.
    for i in range(40):
        next_id += 1
        is_fail = i < 36  # 36/40 = 90%
        rows.append(_row(
            f"OP-{next_id}",
            status="failed" if is_fail else "ok",
            stop_reason="max_iterations_exceeded" if is_fail else "end_turn",
            tier="L",
            area=["backend", "security"],
            files_touched=[
                "backend/security/auth.py",
                "backend/security/rbac.py",
            ],
            iterations=40 if is_fail else 12,
        ))

    # Pattern B — tier:X + max_tokens.
    for i in range(20):
        next_id += 1
        is_fail = i < 18  # 18/20 = 90%
        rows.append(_row(
            f"OP-{next_id}",
            status="failed" if is_fail else "ok",
            stop_reason="max_tokens" if is_fail else "end_turn",
            tier="X",
            area=["backend"],
            files_touched=["backend/agents/dispatcher.py"],
        ))

    # Pattern C — frontend/components/* failures (ticket_capped + error).
    for i in range(15):
        next_id += 1
        is_fail = i < 14  # 14/15 ≈ 93%
        rows.append(_row(
            f"OP-{next_id}",
            status="ticket_capped" if is_fail else "ok",
            stop_reason=None,
            tier="M",
            area=["frontend"],
            files_touched=[
                "frontend/components/Dashboard.tsx",
                "frontend/components/Sidebar.tsx",
            ],
            error="per-ticket cap $5.00 exceeded" if is_fail else None,
        ))

    # Noise — clean runs across miscellaneous areas, no clusters.
    noise_specs = [
        ("docs", ["docs/sop/foo.md"]),
        ("backend", ["backend/api/routes.py"]),
        ("tests", ["backend/tests/test_misc.py"]),
        ("frontend", ["frontend/utils/helpers.ts"]),
        ("backend", ["backend/db/models.py"]),
    ]
    for i in range(25):
        next_id += 1
        area, files = noise_specs[i % len(noise_specs)]
        rows.append(_row(
            f"OP-{next_id}",
            status="ok",
            stop_reason="end_turn",
            tier="S" if i % 2 == 0 else "M",
            area=[area],
            files_touched=files,
        ))

    assert len(rows) == 100, f"expected 100 rows, got {len(rows)}"
    return rows


# ─── Unit tests ──────────────────────────────────────────────────────


def test_path_prefixes_includes_one_two_three_segments() -> None:
    out = _path_prefixes("backend/security/auth/jwt.py")
    assert "backend/" in out
    assert "backend/security/" in out
    assert "backend/security/auth/" in out
    # The 3-segment prefix ends with '/' because the path has more parts;
    # leaf-equal depths get no trailing slash. Path length 4, depth 3 < 4 → trailing slash.
    assert any(p.startswith("backend/security/auth") for p in out)


def test_path_prefixes_short_path() -> None:
    # 2-segment path — only depth 1 and 2 produce prefixes.
    out = _path_prefixes("a/b.py")
    assert "a/" in out
    # depth 2 == path length → no trailing slash on the deepest prefix
    assert "a/b.py" in out


def test_stop_reason_from_error_extracts_max_iterations() -> None:
    assert _stop_reason_from_error("stop=max_iterations_exceeded after 40 turns") == "max_iterations_exceeded"
    assert _stop_reason_from_error("max iterations reached") == "max_iterations_exceeded"


def test_stop_reason_from_error_extracts_max_tokens() -> None:
    assert _stop_reason_from_error("response truncated by max_tokens") == "max_tokens"


def test_stop_reason_from_error_returns_none_for_unrecognised() -> None:
    assert _stop_reason_from_error("network timeout") is None
    assert _stop_reason_from_error(None) is None


def test_parse_entry_normalises_area_and_files() -> None:
    raw = {
        "ticket_key": "OP-1",
        "status": "failed",
        "area": ["backend", "security"],
        "files_touched": ["backend/security/foo.py", "backend/security/bar.py"],
    }
    entry = parse_entry(raw)
    assert entry is not None
    assert entry.area == ("backend", "security")
    assert entry.files_touched == ("backend/security/foo.py", "backend/security/bar.py")


def test_parse_entry_missing_ticket_key_returns_none() -> None:
    assert parse_entry({"status": "ok"}) is None


def test_parse_entry_accepts_string_area_csv() -> None:
    entry = parse_entry({"ticket_key": "OP-1", "status": "ok", "area": "backend, security"})
    assert entry is not None
    assert entry.area == ("backend", "security")


def test_runlogentry_is_failure_classifies_correctly() -> None:
    # Clean success
    e = RunLogEntry(ticket_key="OP-1", status="ok", stop_reason="end_turn")
    assert not e.is_failure
    # Skipped (idempotent re-run) is not a failure
    e = RunLogEntry(ticket_key="OP-2", status="skipped_existing_ps")
    assert not e.is_failure
    # Explicit failure status
    e = RunLogEntry(ticket_key="OP-3", status="failed")
    assert e.is_failure
    # status=ok but max_iterations stop_reason → still a failure (W14.5)
    e = RunLogEntry(ticket_key="OP-4", status="ok", stop_reason="max_iterations_exceeded")
    assert e.is_failure


def test_suggest_mitigation_security_routes_to_human() -> None:
    sig = (("area", "security"), ("stop_reason", "max_iterations_exceeded"))
    text = _suggest_mitigation(sig)
    assert "non-AI" in text or "human" in text.lower()


def test_suggest_mitigation_max_iterations_recommends_class_move() -> None:
    sig = (("stop_reason", "max_iterations_exceeded"),)
    text = _suggest_mitigation(sig)
    assert "subscription-claude" in text or "claude class" in text.lower() or "decompose" in text.lower()


def test_suggest_mitigation_max_tokens_recommends_plan() -> None:
    sig = (("stop_reason", "max_tokens"),)
    text = _suggest_mitigation(sig)
    assert "Plan" in text or "decomposition" in text.lower()


def test_suggest_mitigation_tier_x_recommends_split() -> None:
    sig = (("tier", "X"),)
    text = _suggest_mitigation(sig)
    assert "split" in text.lower() or "decompose" in text.lower()


# ─── Integration / acceptance test (the AC gate) ─────────────────────


def test_audit_detects_all_three_planted_patterns() -> None:
    """OP-822 AC: 100 fixture run logs with planted patterns → audit detects all 3.

    The exact signatures are flexible (the detector may surface
    ``area=security`` or ``file_prefix=backend/security/`` for the same
    cluster), but at most one pattern slot should map to each plant —
    so the test asserts we get one slot per plant in the top-3.
    """
    rows = _planted_dataset()
    entries = [parse_entry(r) for r in rows]
    entries = [e for e in entries if e is not None]
    assert len(entries) == 100

    result = detect_patterns(
        entries,
        min_support=5,
        min_failure_rate=DEFAULT_MIN_FAILURE_RATE,
        top_n=3,
    )

    assert result.total_runs == 100
    # 36 + 18 + 14 = 68 failures planted.
    assert result.total_failures == 68
    assert len(result.patterns) == 3, (
        f"expected 3 patterns, got {len(result.patterns)}: "
        f"{[p.signature_str for p in result.patterns]}"
    )

    # Map each of the 3 plants to at least one returned pattern by signature.
    matched_a = matched_b = matched_c = False
    for pat in result.patterns:
        sig_dict = dict(pat.signature)
        sig_str = pat.signature_str
        # Pattern A — backend/security + max_iterations
        if (
            "max_iterations_exceeded" in sig_str
            and ("security" in sig_str or "backend/security" in sig_str)
        ):
            matched_a = True
        # Pattern B — tier:X + max_tokens
        elif sig_dict.get("tier") == "X" or sig_dict.get("stop_reason") == "max_tokens":
            matched_b = True
        # Pattern C — frontend/components/* ticket_capped failures
        elif (
            "frontend/components" in sig_str
            or sig_dict.get("status") == "ticket_capped"
            or sig_dict.get("stop_reason") == "ticket_capped"
        ):
            matched_c = True

    assert matched_a, f"Pattern A (backend/security + max_iter) not in top 3: {[p.signature_str for p in result.patterns]}"
    assert matched_b, f"Pattern B (tier:X / max_tokens) not in top 3: {[p.signature_str for p in result.patterns]}"
    assert matched_c, f"Pattern C (frontend/components ticket_capped) not in top 3: {[p.signature_str for p in result.patterns]}"


def test_audit_attaches_mitigation_to_every_pattern() -> None:
    rows = _planted_dataset()
    entries = [e for e in (parse_entry(r) for r in rows) if e is not None]
    result = detect_patterns(entries, min_support=5, top_n=3)
    for pat in result.patterns:
        assert pat.mitigation
        assert len(pat.mitigation) > 30, f"mitigation suspiciously short: {pat.mitigation!r}"


def test_audit_returns_empty_pattern_list_for_clean_runs() -> None:
    """If no clusters cross thresholds, the detector returns an empty list,
    but still reports total_runs/total_failures/baseline correctly."""
    entries = [
        RunLogEntry(ticket_key=f"OP-{i}", status="ok", stop_reason="end_turn")
        for i in range(50)
    ]
    result = detect_patterns(entries, min_support=5)
    assert result.total_runs == 50
    assert result.total_failures == 0
    assert result.patterns == []


def test_audit_handles_empty_input() -> None:
    result = detect_patterns([], min_support=5)
    assert result.total_runs == 0
    assert result.total_failures == 0
    assert result.baseline_failure_rate == 0.0
    assert result.patterns == []


def test_load_entries_reads_jsonl_from_disk(tmp_path: Path) -> None:
    log_dir = tmp_path / "sdk-launcher"
    log_dir.mkdir()
    log_file = log_dir / "run-1.log"
    rows = _planted_dataset()
    log_file.write_text("\n".join(json.dumps(r) for r in rows) + "\n")

    entries = load_entries([log_dir])
    assert len(entries) == 100


def test_load_entries_skips_malformed_jsonl_rows(tmp_path: Path) -> None:
    log_dir = tmp_path / "sdk-launcher"
    log_dir.mkdir()
    log_file = log_dir / "run-1.log"
    log_file.write_text(
        json.dumps({"ticket_key": "OP-1", "status": "ok"}) + "\n"
        + "not-valid-json\n"
        + json.dumps({"status": "ok"}) + "\n"  # missing ticket_key → skipped
        + json.dumps({"ticket_key": "OP-2", "status": "failed"}) + "\n"
    )
    entries = load_entries([log_dir])
    assert {e.ticket_key for e in entries} == {"OP-1", "OP-2"}


def test_load_entries_returns_empty_when_dir_missing(tmp_path: Path) -> None:
    assert load_entries([tmp_path / "does-not-exist"]) == []


# ─── Markdown rendering ──────────────────────────────────────────────


def test_markdown_report_includes_all_top_patterns() -> None:
    rows = _planted_dataset()
    entries = [e for e in (parse_entry(r) for r in rows) if e is not None]
    result = detect_patterns(entries, min_support=5, top_n=3)
    md = render_markdown(result, generated_at=datetime(2026, 5, 9, tzinfo=timezone.utc))

    assert "# SDK Runner Failure-Pattern Audit" in md
    assert "OP-822" in md
    assert "Total runs analysed: **100**" in md
    assert "Total failures: **68**" in md
    assert "Top 3 Failure Patterns" in md
    # Mitigation text lands on every pattern block.
    assert md.count("**Suggested mitigation:**") == 3
    # Per-dimension appendix lands.
    assert "Per-dimension breakdown" in md


def test_markdown_report_handles_no_data() -> None:
    from scripts.sdk_failure_pattern_audit import AuditResult
    result = AuditResult(
        total_runs=0, total_failures=0, baseline_failure_rate=0.0,
        patterns=[], by_dimension_summary={},
    )
    md = render_markdown(result, generated_at=datetime(2026, 5, 9, tzinfo=timezone.utc))
    assert "No run log entries found" in md


def test_markdown_report_handles_no_patterns_above_threshold() -> None:
    """Many clean runs + a couple of one-off failures → no clusters."""
    entries = [RunLogEntry(ticket_key=f"OP-{i}", status="ok") for i in range(50)]
    entries.extend([
        RunLogEntry(ticket_key="OP-X1", status="failed"),
        RunLogEntry(ticket_key="OP-X2", status="failed"),
    ])
    result = detect_patterns(entries, min_support=5, top_n=3)
    md = render_markdown(result, generated_at=datetime(2026, 5, 9, tzinfo=timezone.utc))
    assert "No clusters cleared the support" in md or "No clusters" in md


# ─── CLI smoke test ──────────────────────────────────────────────────


def test_cli_main_writes_markdown_to_output(tmp_path: Path) -> None:
    from scripts import sdk_failure_pattern_audit as audit

    log_dir = tmp_path / "sdk-launcher"
    log_dir.mkdir()
    (log_dir / "run.log").write_text(
        "\n".join(json.dumps(r) for r in _planted_dataset()) + "\n"
    )
    out = tmp_path / "report.md"
    rc = audit.main([
        "--log-dir", str(log_dir),
        "--output", str(out),
        "--min-support", "5",
        "--top", "3",
    ])
    assert rc == 0
    text = out.read_text(encoding="utf-8")
    assert "Top 3 Failure Patterns" in text
    assert text.count("**Suggested mitigation:**") == 3


def test_cli_stdout_mode_prints_report(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from scripts import sdk_failure_pattern_audit as audit

    log_dir = tmp_path / "sdk-launcher"
    log_dir.mkdir()
    (log_dir / "run.log").write_text(
        "\n".join(json.dumps(r) for r in _planted_dataset()) + "\n"
    )
    rc = audit.main([
        "--log-dir", str(log_dir),
        "--stdout",
        "--min-support", "5",
        "--top", "3",
    ])
    assert rc == 0
    captured = capsys.readouterr()
    assert "SDK Runner Failure-Pattern Audit" in captured.out
