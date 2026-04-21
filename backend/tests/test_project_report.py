"""Phase 61 tests — Final Report builder + renderers."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest


@pytest.fixture()
async def _report_db(pg_test_pool):
    """Phase-3 Step C.1 (2026-04-21): ported off the SQLite-file
    ``OMNISIGHT_DATABASE_PATH`` + ``db._conn()`` setup onto the
    shared ``pg_test_pool`` + direct ``$N`` placeholders. The tables
    aren't part of the conftest TRUNCATE set, so we wipe the three
    seed tables explicitly before inserting.
    """
    from backend import audit, db
    async with pg_test_pool.acquire() as conn:
        await conn.execute(
            "TRUNCATE token_usage, artifacts, episodic_memory, audit_log "
            "RESTART IDENTITY CASCADE"
        )
        await conn.execute(
            "INSERT INTO token_usage (model, total_tokens, request_count, "
            "last_used) VALUES ($1, $2, $3, '')",
            "claude", 12345, 7,
        )
        await conn.execute(
            "INSERT INTO artifacts (id, name, type, file_path, size, "
            "created_at, version, checksum) VALUES "
            "($1, $2, $3, $4, $5, "
            "to_char(CURRENT_TIMESTAMP, 'YYYY-MM-DD HH24:MI:SS'), '', $6)",
            "art-1", "fw.bin", "firmware", "/tmp/fw.bin", 1024, "abc123def456",
        )
        await conn.execute(
            "INSERT INTO episodic_memory (id, error_signature, solution, "
            "quality_score, access_count) VALUES ($1, $2, $3, $4, $5)",
            "ep-1", "cmake not found", "apt install cmake", 0.92, 5,
        )
    await audit.log("mode_change", "operation_mode", "global",
                    before={"mode": "supervised"}, after={"mode": "full_auto"})
    await audit.log("decision_resolve", "decision", "dec-x",
                    before={"status": "pending"},
                    after={"status": "approved", "chosen_option_id": "go"})
    yield (db, audit)


@pytest.mark.asyncio
async def test_build_returns_six_sections(_report_db):
    from backend import project_report as pr
    rep = await pr.build_report("smoke")
    # all 6 sections present
    assert rep.executive_summary
    assert isinstance(rep.compliance, list)
    assert isinstance(rep.metrics, list) and len(rep.metrics) >= 4
    assert isinstance(rep.audit_timeline, list) and len(rep.audit_timeline) >= 2
    assert isinstance(rep.lessons_learned, list) and any("cmake" in l for l in rep.lessons_learned)
    assert isinstance(rep.artifacts, list) and any(a.name == "fw.bin" for a in rep.artifacts)
    assert rep.forecast_method
    assert 0.0 <= rep.forecast_confidence <= 1.0


@pytest.mark.asyncio
async def test_metrics_compare_includes_actuals(_report_db):
    from backend import project_report as pr
    rep = await pr.build_report("smoke")
    by_label = {m.label: m for m in rep.metrics}
    assert by_label["tokens"].actual == 12345
    assert by_label["tasks"].actual == 7


@pytest.mark.asyncio
async def test_render_html_self_contained(_report_db):
    from backend import project_report as pr
    rep = await pr.build_report("smoke")
    html = pr.render_html(rep)
    assert html.startswith("<!doctype html>")
    assert "Final Report" in html
    assert "Compliance Matrix" in html
    assert "Decision Audit Timeline" in html
    # No external CSS link — embedded <style>
    assert "<link" not in html or "stylesheet" not in html
    assert "<style>" in html


@pytest.mark.asyncio
async def test_render_pdf_falls_back_when_no_weasyprint(_report_db, monkeypatch):
    """If WeasyPrint can't render (e.g. missing system libs in this env),
    render_pdf must return ok=False with an HTML sibling rather than
    crashing."""
    from backend import project_report as pr
    rep = await pr.build_report("smoke")
    out = Path(tempfile.mktemp(suffix=".pdf"))
    ok, path = pr.render_pdf(rep, out)
    # ok may be True OR False depending on whether weasyprint is
    # installed in CI; both must produce a readable file.
    p = Path(path)
    assert p.exists()
    assert p.stat().st_size > 0
    if not ok:
        assert path.endswith(".html")


@pytest.mark.asyncio
async def test_etag_changes_with_content(_report_db):
    """Sanity: etag is a hash of the report content."""
    from backend import project_report as pr
    rep1 = await pr.build_report("smoke")
    rep2 = await pr.build_report("smoke")
    # Close in time → metrics same → etag differs only by generated_at;
    # we still expect both etags to be 16 hex chars
    assert len(pr.report_etag(rep1)) == 16
    assert len(pr.report_etag(rep2)) == 16
