"""OP-794 contract tests for the docs-site operator runbook.

The runbook at ``docs-site/docs/runbook/docs-site-workflow.md`` is the
canonical operator playbook for editing the docs corpus: local preview,
grep, git history, the lessons-learned.md migration FAQ, and the
end-to-end dry-run procedure with a recorded sign-off.

These tests pin the structural contract so the page cannot silently rot
into a stub:

  (1) The runbook exists at the canonical path the OP-794 ticket and
      the mkdocs nav point at.
  (2) Each Acceptance Criteria item from the ticket Spec is covered by
      a section that mentions the exact command operators will run
      (``make docs-serve``, the lessons grep, the per-lesson git log).
  (3) The lessons-learned.md migration FAQ is present, names the OP-790
      deletion commit so a reader can recover the historical snapshot,
      and explains where to look now.
  (4) The dry-run section spells out the 5-minute envelope the publish
      pipeline (OP-792) commits to, and records a dated sign-off.
  (5) The MkDocs nav surfaces the page, so ``make docs-serve``
      operators see it under the Runbooks section.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]
RUNBOOK = (
    PROJECT_ROOT
    / "docs-site"
    / "docs"
    / "runbook"
    / "docs-site-workflow.md"
)
MKDOCS_CONFIG = PROJECT_ROOT / "docs-site" / "mkdocs.yml"
RUNBOOK_INDEX = PROJECT_ROOT / "docs-site" / "docs" / "runbook" / "index.md"


@pytest.fixture(scope="module")
def runbook_text() -> str:
    assert RUNBOOK.exists(), f"runbook missing at {RUNBOOK}"
    return RUNBOOK.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def mkdocs_config() -> dict:
    assert MKDOCS_CONFIG.exists(), f"mkdocs.yml missing at {MKDOCS_CONFIG}"
    return yaml.safe_load(MKDOCS_CONFIG.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# (1) File exists at canonical path with a pinned title.
# ---------------------------------------------------------------------------


def test_runbook_exists_at_canonical_path() -> None:
    assert RUNBOOK.exists(), (
        "OP-794 deliverable missing — operators editing docs have no "
        "runbook to follow"
    )


def test_runbook_has_op794_titled_h1(runbook_text: str) -> None:
    first_heading = next(
        line for line in runbook_text.splitlines() if line.startswith("# ")
    )

    assert "OP-794" in first_heading, (
        "H1 must anchor to OP-794 so a future grep `# .*OP-794` lands "
        "here"
    )
    assert "operator" in first_heading.lower(), (
        "H1 must call this an operator workflow runbook"
    )


def test_runbook_substantive(runbook_text: str) -> None:
    assert len(runbook_text) > 3500, (
        "runbook suspiciously short — a stub will not satisfy the "
        "OP-794 acceptance criteria"
    )


# ---------------------------------------------------------------------------
# (2) Each AC item from the OP-794 spec is covered with the literal
#     command the operator runs.
# ---------------------------------------------------------------------------


def test_local_preview_section_uses_make_docs_serve(runbook_text: str) -> None:
    assert "make docs-serve" in runbook_text, (
        "local preview AC requires the literal `make docs-serve` "
        "command so an operator can copy-paste"
    )
    assert "127.0.0.1:8765" in runbook_text or "DOCS_PORT" in runbook_text, (
        "local preview section must document the default port or how "
        "to override it"
    )


def test_search_section_uses_grep_against_lessons_dir(runbook_text: str) -> None:
    assert "grep -r" in runbook_text and "docs/sop/lessons/" in runbook_text, (
        "search AC requires the exact `grep -r` against "
        "docs/sop/lessons/ pattern named in the ticket Spec"
    )


def test_git_history_section_documents_per_lesson_log(runbook_text: str) -> None:
    assert "git log" in runbook_text, "git history AC requires `git log`"
    assert "docs/sop/lessons/L-" in runbook_text, (
        "git history section must show the per-lesson path glob the "
        "ticket Spec calls out"
    )


# ---------------------------------------------------------------------------
# (3) Migration FAQ — answer + redirect for the legacy aggregate file.
# ---------------------------------------------------------------------------


def test_migration_faq_answers_where_is_lessons_learned(runbook_text: str) -> None:
    assert "lessons-learned.md" in runbook_text, (
        "migration FAQ must name the legacy file path operators will "
        "search for"
    )
    # OP-790 is the commit that deleted the aggregate.
    assert "OP-790" in runbook_text, (
        "migration FAQ must cite OP-790 so a reader can find the "
        "deletion commit and the final snapshot"
    )
    assert "docs.sora-dev.app" in runbook_text, (
        "migration FAQ must redirect operators to the live docs site"
    )


# ---------------------------------------------------------------------------
# (4) Dry-run procedure with recorded sign-off and the 5-minute envelope.
# ---------------------------------------------------------------------------


def test_dry_run_section_pins_5_minute_envelope(runbook_text: str) -> None:
    assert "5-minute" in runbook_text or "5 min" in runbook_text, (
        "dry-run AC requires the 5-minute envelope the OP-792 publish "
        "pipeline commits to"
    )


def test_dry_run_section_invokes_both_builders(runbook_text: str) -> None:
    # MkDocs preview path.
    assert "make docs-serve" in runbook_text
    # Production builder path — the one CI publishes through.
    assert "backend.docs_static_site" in runbook_text, (
        "dry-run must cross-check the production builder, not just the "
        "MkDocs preview, otherwise an edit can pass preview but never "
        "ship"
    )


def test_dry_run_signoff_block_present_with_evidence(runbook_text: str) -> None:
    assert "sign-off" in runbook_text.lower(), (
        "dry-run sign-off block missing — AC #2 explicitly requires it"
    )
    # The sign-off table cites the two builder commands plus an
    # observed timing. Pin the timing presence so a future edit cannot
    # silently strip the evidence and leave a vague claim.
    assert "0.06s" in runbook_text or "0.26s" in runbook_text, (
        "sign-off block must record the observed builder timings, not "
        "just claim the dry-run was performed"
    )


# ---------------------------------------------------------------------------
# (5) MkDocs nav and runbook index surface the page.
# ---------------------------------------------------------------------------


def _flatten_nav(nav: list) -> list[str]:
    flat: list[str] = []
    for entry in nav or []:
        if isinstance(entry, str):
            flat.append(entry)
        elif isinstance(entry, dict):
            for value in entry.values():
                if isinstance(value, str):
                    flat.append(value)
                elif isinstance(value, list):
                    flat.extend(_flatten_nav(value))
    return flat


def test_mkdocs_nav_includes_operator_workflow_runbook(mkdocs_config: dict) -> None:
    paths = _flatten_nav(mkdocs_config.get("nav", []))

    assert "runbook/docs-site-workflow.md" in paths, (
        "mkdocs nav must surface the operator workflow runbook so it "
        "shows up under Runbooks in `make docs-serve`"
    )


def test_runbook_index_links_to_operator_workflow() -> None:
    body = RUNBOOK_INDEX.read_text(encoding="utf-8")

    assert "docs-site-workflow.md" in body, (
        "runbook section index must link to the operator workflow "
        "page so readers landing on /runbook/ can find it"
    )
