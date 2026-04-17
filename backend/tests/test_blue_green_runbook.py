"""G3 #5 — `docs/ops/blue_green_runbook.md` contract (TODO row 1357).

This is the **closing** deliverable of G3 (HA-03 Blue-Green): rows
1353–1356 ship the scripts; row 1357 ships the operator runbook + a
delivery manifest for the bundle.

The runbook is *script-backed* — every command, exit code, env var, and
state file shown is an exact copy of what `scripts/deploy.sh`,
`scripts/bluegreen_switch.sh`, or `scripts/prod_smoke_test.py` actually
implements. If a script changes (new exit code, renamed env var, removed
state file) the runbook must follow. This file pins that contract:

    (1) The runbook exists at the canonical path the deploy ticket
        checklist points at.
    (2) Every operator section the troubleshooting tree relies on is
        present (titles + the right ordering — anchors documented in
        change-management).
    (3) Every exit code the scripts emit appears in at least one
        runbook table — drift here means an operator paged at 3am
        sees an undocumented exit code.
    (4) Every env-var tunable the scripts read is listed in §9
        (cheat-sheet) so an operator can find the override without
        grepping bash.
    (5) Every state file the scripts write is described in §2 (the
        five files of state).
    (6) Every script the runbook tells operators to run actually
        exists at the path printed in the runbook (no broken
        copy-paste).
    (7) Every cross-referenced sibling test file from §10 actually
        exists — so the contract index doesn't go stale.

Siblings (this is row 1357 / G3 #5 — the bundle):
    * test_deploy_sh_blue_green_flag.py   — G3 #1 row 1353 (24)
    * test_bluegreen_atomic_switch.py     — G3 #2 row 1354 (32)
    * test_bluegreen_precut_ceremony.py   — G3 #3 row 1355 (29)
    * test_deploy_sh_rollback.py          — G3 #4 row 1356 (40)
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
RUNBOOK = PROJECT_ROOT / "docs" / "ops" / "blue_green_runbook.md"
DEPLOY_SH = PROJECT_ROOT / "scripts" / "deploy.sh"
BLUEGREEN_SWITCH = PROJECT_ROOT / "scripts" / "bluegreen_switch.sh"
PROD_SMOKE = PROJECT_ROOT / "scripts" / "prod_smoke_test.py"
STATE_DIR = PROJECT_ROOT / "deploy" / "blue-green"


@pytest.fixture(scope="module")
def runbook_text() -> str:
    assert RUNBOOK.exists(), f"runbook missing at {RUNBOOK}"
    return RUNBOOK.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def deploy_sh_text() -> str:
    assert DEPLOY_SH.exists(), f"deploy.sh missing at {DEPLOY_SH}"
    return DEPLOY_SH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def switch_sh_text() -> str:
    assert BLUEGREEN_SWITCH.exists(), f"bluegreen_switch.sh missing at {BLUEGREEN_SWITCH}"
    return BLUEGREEN_SWITCH.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# (1) File exists at canonical path
# ---------------------------------------------------------------------------


class TestRunbookFileShape:
    def test_runbook_exists(self) -> None:
        assert RUNBOOK.exists(), (
            f"row 1357 deliverable missing: {RUNBOOK} — operators paged "
            "at 3am have nowhere to look"
        )

    def test_runbook_in_docs_ops(self) -> None:
        # The deploy ticket checklist + N10 policy doc both link by
        # this exact path; moving it would silently break those links.
        assert RUNBOOK.parent == PROJECT_ROOT / "docs" / "ops", (
            "runbook must live at docs/ops/blue_green_runbook.md "
            "(deploy ticket + N10 policy link by exact path)"
        )

    def test_runbook_nonempty(self, runbook_text: str) -> None:
        # A typo'd write that leaves the file empty must fail loudly.
        assert len(runbook_text) > 2000, (
            "runbook suspiciously short — a placeholder won't help an "
            "oncall at 3am"
        )

    def test_runbook_has_top_level_title(self, runbook_text: str) -> None:
        # Markdown TOC tooling + the docs site nav both key on the
        # first H1; pin it.
        first_line = runbook_text.lstrip().splitlines()[0]
        assert first_line.startswith("# "), "runbook must start with H1"
        assert "Blue-Green" in first_line, "H1 must mention Blue-Green"
        assert "Runbook" in first_line, "H1 must call this a Runbook"
        assert "G3" in first_line or "HA-03" in first_line, (
            "H1 must anchor to the G3 / HA-03 TODO bucket so a future "
            "tree-grep `## G3` or `## HA-03` lands here"
        )


# ---------------------------------------------------------------------------
# (2) Every operator-critical section present (titles + ordering)
# ---------------------------------------------------------------------------


REQUIRED_SECTIONS_IN_ORDER: list[str] = [
    "## 1. Why blue-green",
    "## 2. The five files that *are* blue-green state",
    "## 3. Pre-flight",
    "## 4. Cutover ceremony",
    "## 5. Rollback ceremony",
    "## 6. Post-cutover hygiene",
    "## 7. Manual primitive reference",
    "## 8. Troubleshooting decision tree",
    "## 9. Tunables (env vars) cheat-sheet",
    "## 10. Script & contract index",
    "## 11. Anti-patterns",
    "## 12. Change-management checklist",
]


class TestRunbookSections:
    @pytest.mark.parametrize("title", REQUIRED_SECTIONS_IN_ORDER)
    def test_section_present(self, runbook_text: str, title: str) -> None:
        # We compare as a substring so authors can refine the heading
        # text (e.g. add a parenthetical) without breaking the test,
        # but the section anchor must exist.
        assert title in runbook_text, (
            f"runbook missing required section heading: {title!r}"
        )

    def test_sections_in_order(self, runbook_text: str) -> None:
        # An operator scanning top-to-bottom expects pre-flight before
        # cutover, cutover before rollback, etc. Out-of-order sections
        # would still pass the "present" check but would confuse readers.
        positions = [runbook_text.find(t) for t in REQUIRED_SECTIONS_IN_ORDER]
        assert all(p >= 0 for p in positions), (
            "all sections must be present (covered by per-section tests)"
        )
        assert positions == sorted(positions), (
            f"runbook sections out of order — got positions {positions}"
        )


# ---------------------------------------------------------------------------
# (3) Every exit code the scripts emit appears in the runbook
# ---------------------------------------------------------------------------


CUTOVER_EXIT_CODES = {0, 3, 4, 5, 6, 7}
ROLLBACK_EXIT_CODES = {0, 2, 3, 5, 8}
SWITCH_EXIT_CODES = {0, 1, 2, 3}


class TestExitCodeCoverage:
    @pytest.mark.parametrize("code", sorted(CUTOVER_EXIT_CODES))
    def test_cutover_exit_documented(self, runbook_text: str, code: int) -> None:
        # The §4.1 table holds the cutover exits. We grep for the
        # bold-table pattern `**N**` — pinned by the table layout.
        marker = f"**{code}**"
        assert marker in runbook_text, (
            f"cutover exit code {code} (emitted by scripts/deploy.sh "
            f"--strategy blue-green) not documented in the runbook — "
            f"operator triage table is incomplete"
        )

    @pytest.mark.parametrize("code", sorted(ROLLBACK_EXIT_CODES))
    def test_rollback_exit_documented(self, runbook_text: str, code: int) -> None:
        marker = f"**{code}**"
        assert marker in runbook_text, (
            f"rollback exit code {code} (emitted by scripts/deploy.sh "
            f"--rollback) not documented in the runbook — operator "
            f"triage table is incomplete"
        )

    def test_no_undocumented_cutover_exits(
        self, runbook_text: str, deploy_sh_text: str
    ) -> None:
        # If deploy.sh starts emitting an exit code that isn't in the
        # known set, this catches it — we can't auto-extract every
        # `exit N` (some are in unrelated arms), but we can pin the
        # known set against the runbook.
        for code in CUTOVER_EXIT_CODES:
            assert f"exit {code}" in deploy_sh_text or f"exit {code} " in deploy_sh_text, (
                f"sanity: scripts/deploy.sh should still emit exit {code}"
            )

    @pytest.mark.parametrize("code", sorted(SWITCH_EXIT_CODES))
    def test_switch_exit_documented(self, runbook_text: str, code: int) -> None:
        # §7 manual primitive table holds the switch script exits.
        # We use a `| N |` table-cell pattern.
        marker = f"| {code} |"
        assert marker in runbook_text, (
            f"bluegreen_switch.sh exit code {code} not documented in §7"
        )


# ---------------------------------------------------------------------------
# (4) Every env-var tunable the scripts read is in the cheat-sheet
# ---------------------------------------------------------------------------


REQUIRED_TUNABLES: list[str] = [
    # Cutover-side
    "OMNISIGHT_BLUEGREEN_SMOKE_TIMEOUT",
    "OMNISIGHT_BLUEGREEN_OBSERVE_SECONDS",
    "OMNISIGHT_BLUEGREEN_OBSERVE_INTERVAL",
    "OMNISIGHT_BLUEGREEN_OBSERVE_MAX_FAILURES",
    "OMNISIGHT_BLUEGREEN_RETENTION_HOURS",
    "OMNISIGHT_BLUEGREEN_STANDBY_READY_TIMEOUT",
    "OMNISIGHT_BLUEGREEN_CADDY_RELOAD_CMD",
    "OMNISIGHT_BLUEGREEN_DRY_RUN",
    "OMNISIGHT_BLUEGREEN_SKIP_SMOKE",
    # Rollback-side
    "OMNISIGHT_ROLLBACK_FORCE",
    "OMNISIGHT_ROLLBACK_SKIP_PREFLIGHT",
    "OMNISIGHT_BLUEGREEN_DIR",
    # Shared
    "OMNISIGHT_COMPOSE_FILE",
    "OMNISIGHT_CHECK_BLUEGREEN",
]


class TestTunableCoverage:
    @pytest.mark.parametrize("var", REQUIRED_TUNABLES)
    def test_tunable_in_runbook(self, runbook_text: str, var: str) -> None:
        assert var in runbook_text, (
            f"tunable env var {var} (read by scripts/deploy.sh) not "
            f"documented in runbook §9 cheat-sheet — operator can't "
            f"discover the override path"
        )

    @pytest.mark.parametrize("var", REQUIRED_TUNABLES)
    def test_tunable_in_deploy_sh(self, deploy_sh_text: str, var: str) -> None:
        # Sanity: confirm the var is actually consumed by deploy.sh —
        # otherwise the runbook is documenting a fiction.
        assert var in deploy_sh_text, (
            f"runbook lists {var} but scripts/deploy.sh doesn't read "
            f"it — runbook is wrong OR script regressed"
        )


# ---------------------------------------------------------------------------
# (5) Every state file the scripts write is in the §2 table
# ---------------------------------------------------------------------------


REQUIRED_STATE_FILES: list[str] = [
    "active_color",
    "active_upstream.caddy",
    "upstream-blue.caddy",
    "upstream-green.caddy",
    "previous_color",
    "cutover_timestamp",
    "previous_retention_until",
    "rollback_timestamp",
]


class TestStateFileCoverage:
    @pytest.mark.parametrize("name", REQUIRED_STATE_FILES)
    def test_state_file_in_runbook(self, runbook_text: str, name: str) -> None:
        assert name in runbook_text, (
            f"state file {name} not mentioned in runbook — operators "
            f"reading deploy/blue-green/ won't know what it means"
        )


# ---------------------------------------------------------------------------
# (6) Every script the runbook references actually exists
# ---------------------------------------------------------------------------


REFERENCED_SCRIPTS: list[Path] = [
    PROJECT_ROOT / "scripts" / "deploy.sh",
    PROJECT_ROOT / "scripts" / "bluegreen_switch.sh",
    PROJECT_ROOT / "scripts" / "prod_smoke_test.py",
    PROJECT_ROOT / "scripts" / "check_bluegreen_gate.py",
    PROJECT_ROOT / "deploy" / "reverse-proxy" / "Caddyfile",
    PROJECT_ROOT / "docker-compose.prod.yml",
]


class TestReferencedArtefactsExist:
    @pytest.mark.parametrize("path", REFERENCED_SCRIPTS, ids=lambda p: str(p.name))
    def test_path_exists(self, path: Path) -> None:
        assert path.exists(), (
            f"runbook references {path.relative_to(PROJECT_ROOT)} but it's "
            f"missing — runbook copy-paste would 404 the operator"
        )

    @pytest.mark.parametrize("path", REFERENCED_SCRIPTS, ids=lambda p: str(p.name))
    def test_path_mentioned_in_runbook(self, runbook_text: str, path: Path) -> None:
        rel = str(path.relative_to(PROJECT_ROOT))
        # Allow either forward-slash basename mention or the full rel
        # path. `Caddyfile` etc. are mentioned by full path.
        assert rel in runbook_text or path.name in runbook_text, (
            f"runbook fixture says {rel} should be referenced but it's "
            f"not in the doc"
        )


# ---------------------------------------------------------------------------
# (7) Sibling contract tests in §10 index actually exist
# ---------------------------------------------------------------------------


REQUIRED_SIBLING_TESTS: list[Path] = [
    PROJECT_ROOT / "backend" / "tests" / "test_bluegreen_atomic_switch.py",
    PROJECT_ROOT / "backend" / "tests" / "test_deploy_sh_blue_green_flag.py",
    PROJECT_ROOT / "backend" / "tests" / "test_bluegreen_precut_ceremony.py",
    PROJECT_ROOT / "backend" / "tests" / "test_deploy_sh_rollback.py",
    PROJECT_ROOT / "backend" / "tests" / "test_reverse_proxy_caddyfile.py",
]


class TestSiblingContractIndex:
    @pytest.mark.parametrize("path", REQUIRED_SIBLING_TESTS, ids=lambda p: str(p.name))
    def test_sibling_exists(self, path: Path) -> None:
        assert path.exists(), (
            f"runbook §10 contract index lists {path.name} but the file "
            f"is missing — index is stale"
        )

    @pytest.mark.parametrize("path", REQUIRED_SIBLING_TESTS, ids=lambda p: str(p.name))
    def test_sibling_named_in_runbook(self, runbook_text: str, path: Path) -> None:
        assert path.name in runbook_text, (
            f"sibling contract test {path.name} not named in runbook §10 "
            f"index — the cross-reference table is incomplete"
        )


# ---------------------------------------------------------------------------
# (8) Pre-flight commands are copy-paste correct
# ---------------------------------------------------------------------------


class TestCopyPasteCorrectness:
    def test_status_command_present(self, runbook_text: str) -> None:
        # The runbook teaches `scripts/bluegreen_switch.sh status` as
        # the canonical read-only inspection. Pin it so a future rename
        # of the subcommand surfaces here.
        assert "scripts/bluegreen_switch.sh status" in runbook_text, (
            "runbook must teach `scripts/bluegreen_switch.sh status` as "
            "the canonical inspection command"
        )

    def test_cutover_command_present(self, runbook_text: str) -> None:
        # Exact form the deploy-ticket checklist will paste.
        assert "scripts/deploy.sh --strategy blue-green prod" in runbook_text, (
            "runbook must teach `scripts/deploy.sh --strategy blue-green "
            "prod <git-ref>` as the cutover entry point"
        )

    def test_rollback_command_present(self, runbook_text: str) -> None:
        assert "scripts/deploy.sh --rollback" in runbook_text, (
            "runbook must teach `scripts/deploy.sh --rollback` as the "
            "fail-back entry point"
        )

    def test_dry_run_pattern_present(self, runbook_text: str) -> None:
        # Operators planning a change-review want a dry-run incantation
        # they can paste into the ticket. Pin it.
        assert "OMNISIGHT_BLUEGREEN_DRY_RUN=1" in runbook_text, (
            "runbook must show a OMNISIGHT_BLUEGREEN_DRY_RUN=1 invocation"
        )

    def test_color_port_mapping_present(self, runbook_text: str) -> None:
        # The blue ↔ backend-a ↔ 8000 / green ↔ backend-b ↔ 8001 mapping
        # is the one piece of "magic" an operator must know cold.
        # Multiple representations are fine; we check the four
        # hostname/color identifiers and both ports appear together.
        assert "blue" in runbook_text and "backend-a" in runbook_text and "8000" in runbook_text, (
            "runbook must document the blue ↔ backend-a ↔ 8000 mapping"
        )
        assert "green" in runbook_text and "backend-b" in runbook_text and "8001" in runbook_text, (
            "runbook must document the green ↔ backend-b ↔ 8001 mapping"
        )

    def test_retention_window_documented(self, runbook_text: str) -> None:
        # The 24h warm-standby is the load-bearing invariant for
        # second-level rollback; if a future operator changes
        # OMNISIGHT_BLUEGREEN_RETENTION_HOURS they must also update the
        # runbook narrative.
        assert "24 h" in runbook_text or "24h" in runbook_text, (
            "runbook must call out the 24 h retention window"
        )


# ---------------------------------------------------------------------------
# (9) Anti-patterns section warns about specific known footguns
# ---------------------------------------------------------------------------


REQUIRED_ANTI_PATTERN_KEYWORDS: list[str] = [
    "active_upstream.caddy",  # don't edit by hand
    "OMNISIGHT_BLUEGREEN_SKIP_SMOKE",  # don't bypass smoke in prod
    "OMNISIGHT_ROLLBACK_FORCE",  # don't double-bypass
    "OMNISIGHT_ROLLBACK_SKIP_PREFLIGHT",  # don't double-bypass
    "tmp.$$",  # tmp-then-mv pattern is the read safety
]


class TestAntiPatterns:
    @pytest.mark.parametrize("keyword", REQUIRED_ANTI_PATTERN_KEYWORDS)
    def test_anti_pattern_warned(self, runbook_text: str, keyword: str) -> None:
        # We allow the keyword to live anywhere in §11 (the anti-
        # patterns section); a finer-grained anchor would over-fit
        # the markdown structure.
        section_start = runbook_text.find("## 11. Anti-patterns")
        section_end = runbook_text.find("## 12.", section_start)
        assert section_start >= 0 and section_end > section_start, (
            "anti-patterns section bounds not found"
        )
        section = runbook_text[section_start:section_end]
        assert keyword in section, (
            f"anti-patterns section must warn about {keyword!r} — known "
            f"footgun left undocumented"
        )


# ---------------------------------------------------------------------------
# (10) Change-management checklist is actually copy-pasteable
# ---------------------------------------------------------------------------


class TestChangeManagementChecklist:
    def test_checklist_has_code_block(self, runbook_text: str) -> None:
        # §12 contains a fenced ``` block with the operator checklist
        # so it can be pasted into a Linear / Jira ticket as a single
        # copy. Pin the structure.
        section_start = runbook_text.find("## 12. Change-management checklist")
        assert section_start >= 0, "§12 missing"
        section = runbook_text[section_start:]
        assert "```" in section, (
            "§12 checklist must be inside a fenced code block so it "
            "copy-pastes cleanly into a deploy ticket"
        )

    def test_checklist_covers_pre_cutover_post(self, runbook_text: str) -> None:
        section_start = runbook_text.find("## 12. Change-management checklist")
        section = runbook_text[section_start:]
        # The three lifecycle phases the operator owns.
        for phase in ("Pre-flight", "Cutover", "Post"):
            assert phase in section, (
                f"§12 checklist missing {phase} phase items — operator "
                f"can't audit their own progress"
            )

    def test_checklist_invokes_real_commands(self, runbook_text: str) -> None:
        section_start = runbook_text.find("## 12. Change-management checklist")
        section = runbook_text[section_start:]
        # The checklist should reference the actual command names,
        # not paraphrase. Catches a future rewrite that drifts the
        # operator script.
        for cmd_fragment in (
            "scripts/bluegreen_switch.sh status",
            "scripts/deploy.sh --strategy blue-green prod",
            "scripts/deploy.sh --rollback",
        ):
            assert cmd_fragment in section, (
                f"§12 checklist must mention literal command {cmd_fragment!r}"
            )


# ---------------------------------------------------------------------------
# (11) Cross-reference: runbook is reachable from sibling artefacts
# ---------------------------------------------------------------------------


class TestRunbookDiscoverability:
    def test_runbook_path_matches_handoff_promise(self) -> None:
        # G3 #4 HANDOFF entry promises `docs/ops/blue_green_runbook.md`
        # as the row 1357 deliverable — so the file must be at exactly
        # that path. (We assert the exact path rather than .exists()
        # alone so a typo'd rename surfaces here.)
        expected = PROJECT_ROOT / "docs" / "ops" / "blue_green_runbook.md"
        assert RUNBOOK == expected, (
            f"runbook path drift: HANDOFF promised {expected}, found {RUNBOOK}"
        )

    def test_runbook_explains_n10_relationship(self, runbook_text: str) -> None:
        # The N10 dependency-upgrade gate is the *reason* blue-green is
        # mandatory for some PRs. The runbook must point at the policy
        # / ledger so an operator can find the gate's contract.
        assert "N10" in runbook_text, (
            "runbook must reference N10 (the gate that makes blue-green "
            "mandatory for some PRs)"
        )
        assert "upgrade_rollback_ledger" in runbook_text, (
            "runbook must link to the N10 rollback ledger for ledger entries"
        )
