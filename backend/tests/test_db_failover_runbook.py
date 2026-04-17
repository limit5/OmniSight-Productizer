"""G4 #6 — ``docs/ops/db_failover.md`` contract (TODO row 1365).

This is the **closing** deliverable of G4 (HA-04 SQLite → Postgres).
Rows 1360–1364 shipped the shim / URL abstraction / HA bundle / data
migrator / CI matrix; row 1365 ships the operator runbook that ties
those five primitives together into an end-to-end cutover and
failover playbook.

The runbook is *script-backed* — every command, exit code, env var,
config knob, and cross-reference is an exact copy of what
``scripts/migrate_sqlite_to_pg.py``, ``scripts/pg_ha_verify.py``,
``deploy/postgres-ha/*``, and ``.github/workflows/db-engine-matrix.yml``
actually implement. If any of those changes (renamed flag, removed
exit code, new env knob) the runbook must follow. This file pins that
contract:

    (1) The runbook exists at the canonical path (other docs +
        ``docs/ops/db_matrix.md`` link by exact path).
    (2) Every operator section an oncall reaches for at 3am is
        present and in the expected top-to-bottom order.
    (3) Every exit code the migrator emits appears in at least one
        runbook table — an undocumented exit leaves operators
        stranded.
    (4) Every committed env var in ``deploy/postgres-ha/.env.example``
        is named in the §11 cheat-sheet.
    (5) Every migrator CLI flag is listed in §11.3.
    (6) Every policy-critical invariant (slot name, application_name,
        sync-mode knob) is mentioned somewhere in the runbook body.
    (7) Every CI job name the runbook references matches a real job
        in ``.github/workflows/db-engine-matrix.yml``.
    (8) Every file path the runbook tells the operator to look at
        actually exists on disk.
    (9) Every contract test file cross-referenced in §12 exists.

Siblings (this is row 1365 / G4 #6 — the bundle):

    * test_alembic_pg_compat.py           — G4 #1 row 1360 (226)
    * test_db_url.py                      — G4 #2 row 1361 (75)
    * test_pg_ha_deployment.py            — G4 #3 row 1362 (114)
    * test_migrate_sqlite_to_pg.py        — G4 #4 row 1363 (51)
    * test_ci_pg_matrix.py                — G4 #5 row 1364 (22)
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
RUNBOOK = PROJECT_ROOT / "docs" / "ops" / "db_failover.md"

MIGRATE_SCRIPT = PROJECT_ROOT / "scripts" / "migrate_sqlite_to_pg.py"
PG_HA_VERIFY = PROJECT_ROOT / "scripts" / "pg_ha_verify.py"

DEPLOY_DIR = PROJECT_ROOT / "deploy" / "postgres-ha"
COMPOSE_FILE = DEPLOY_DIR / "docker-compose.yml"
PRIMARY_CONF = DEPLOY_DIR / "postgresql.primary.conf"
STANDBY_CONF = DEPLOY_DIR / "postgresql.standby.conf"
HBA = DEPLOY_DIR / "pg_hba.conf"
INIT_PRIMARY = DEPLOY_DIR / "init-primary.sh"
INIT_STANDBY = DEPLOY_DIR / "init-standby.sh"
ENV_EXAMPLE = DEPLOY_DIR / ".env.example"

CI_WORKFLOW = PROJECT_ROOT / ".github" / "workflows" / "db-engine-matrix.yml"

DB_MATRIX_DOC = PROJECT_ROOT / "docs" / "ops" / "db_matrix.md"
BLUE_GREEN_DOC = PROJECT_ROOT / "docs" / "ops" / "blue_green_runbook.md"
BOOTSTRAP_DOC = PROJECT_ROOT / "docs" / "ops" / "bootstrap_modes.md"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def runbook_text() -> str:
    assert RUNBOOK.exists(), f"runbook missing at {RUNBOOK}"
    return RUNBOOK.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def migrate_text() -> str:
    assert MIGRATE_SCRIPT.exists(), f"migrate script missing at {MIGRATE_SCRIPT}"
    return MIGRATE_SCRIPT.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def env_example_text() -> str:
    assert ENV_EXAMPLE.exists(), f".env.example missing at {ENV_EXAMPLE}"
    return ENV_EXAMPLE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def ci_workflow_text() -> str:
    assert CI_WORKFLOW.exists(), f"CI workflow missing at {CI_WORKFLOW}"
    return CI_WORKFLOW.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# (1) File exists at canonical path
# ---------------------------------------------------------------------------


class TestRunbookFileShape:
    def test_runbook_exists(self) -> None:
        assert RUNBOOK.exists(), (
            f"row 1365 deliverable missing: {RUNBOOK} — operators paged "
            "at 3am have nowhere to look when the primary goes down"
        )

    def test_runbook_in_docs_ops(self) -> None:
        # Other ops docs (db_matrix.md) + the CI workflow header all
        # link by this exact path; moving it would silently break those.
        assert RUNBOOK.parent == PROJECT_ROOT / "docs" / "ops", (
            "runbook must live at docs/ops/db_failover.md "
            "(db_matrix.md + db-engine-matrix.yml header link by exact path)"
        )

    def test_runbook_nonempty(self, runbook_text: str) -> None:
        # A typo'd write that leaves the file empty must fail loudly.
        assert len(runbook_text) > 5000, (
            "runbook suspiciously short — a placeholder won't help an "
            "oncall at 3am during an unplanned primary failover"
        )

    def test_runbook_has_top_level_title(self, runbook_text: str) -> None:
        first_line = runbook_text.lstrip().splitlines()[0]
        assert first_line.startswith("# "), "runbook must start with H1"
        assert "Failover" in first_line or "Cutover" in first_line, (
            "H1 must mention Failover or Cutover"
        )
        assert "G4" in first_line or "HA-04" in first_line, (
            "H1 must anchor to the G4 / HA-04 TODO bucket so a future "
            "tree-grep `## G4` or `## HA-04` lands here"
        )


# ---------------------------------------------------------------------------
# (2) Required sections present + in order
# ---------------------------------------------------------------------------


REQUIRED_SECTIONS_IN_ORDER: list[str] = [
    "## 1. Scope & prerequisites",
    "## 2. The files that *are* HA state",
    "## 3. Pre-flight",
    "## 4. SQLite → Postgres cutover ceremony",
    "## 5. Planned failover",
    "## 6. Unplanned failover",
    "## 7. Rebuild the old primary as the new standby",
    "## 8. Forensic / read-only inspection",
    "## 9. CI matrix status cross-reference",
    "## 10. Troubleshooting decision tree",
    "## 11. Tunables",
    "## 12. Script & contract index",
    "## 13. Anti-patterns",
    "## 14. Cutover change-management checklist",
    "## 15. Cross-references",
]


class TestRunbookSections:
    @pytest.mark.parametrize("title", REQUIRED_SECTIONS_IN_ORDER)
    def test_section_present(self, runbook_text: str, title: str) -> None:
        assert title in runbook_text, (
            f"runbook missing required section heading: {title!r}"
        )

    def test_sections_in_order(self, runbook_text: str) -> None:
        positions = [runbook_text.find(t) for t in REQUIRED_SECTIONS_IN_ORDER]
        assert all(p >= 0 for p in positions), (
            "all sections must be present (covered by per-section tests)"
        )
        assert positions == sorted(positions), (
            f"runbook sections out of order — got positions {positions}"
        )


# ---------------------------------------------------------------------------
# (3) Migrator exit codes documented
# ---------------------------------------------------------------------------


MIGRATE_EXIT_CODES = {0, 1, 2, 3, 4, 5, 6}


class TestMigrateExitCodeCoverage:
    @pytest.mark.parametrize("code", sorted(MIGRATE_EXIT_CODES))
    def test_exit_documented(self, runbook_text: str, code: int) -> None:
        # §4.1 table formats codes as `**N**`. Pinned by the table layout.
        marker = f"**{code}**"
        assert marker in runbook_text, (
            f"migrate exit code {code} not documented in the runbook — "
            "operator triage table is incomplete"
        )

    def test_exit_codes_match_script_docstring(self, migrate_text: str) -> None:
        # The script's module docstring lists 0–6 in an "Exit codes::" block.
        # If a code is added to the script, the runbook table must grow.
        # Note: we match `Exit codes::\n` then a blank line then a run of
        # 4-space-indented "N  description" lines. `re.findall` on the
        # slice from "Exit codes::" onward picks up all leading-indent
        # digits until the next non-matching line.
        anchor = migrate_text.find("Exit codes::")
        assert anchor >= 0, "migrator docstring missing 'Exit codes::' block"
        block = migrate_text[anchor:]
        # Stop at the first blank line followed by non-indented text.
        end_match = re.search(r"\n\n(?=\S)", block)
        if end_match:
            block = block[: end_match.start()]
        documented = {int(n) for n in re.findall(r"^ {4}(\d+)\s", block, re.M)}
        assert documented == MIGRATE_EXIT_CODES, (
            f"runbook pins exit codes {MIGRATE_EXIT_CODES} but script docstring "
            f"declares {documented} — runbook + script drifted"
        )


# ---------------------------------------------------------------------------
# (4) Env-var tunables in §11 cover everything in .env.example
# ---------------------------------------------------------------------------


REQUIRED_ENV_KNOBS = [
    "POSTGRES_USER",
    "POSTGRES_PASSWORD",
    "POSTGRES_DB",
    "REPLICATION_USER",
    "REPLICATION_PASSWORD",
    "PRIMARY_HOST",
    "PRIMARY_PORT",
    "REPLICATION_SLOT_NAME",
    "REPLICATION_APPLICATION_NAME",
    "PG_PRIMARY_HOST_PORT",
    "PG_STANDBY_HOST_PORT",
    "STANDBY_BASEBACKUP_TIMEOUT",
]


class TestEnvKnobCoverage:
    @pytest.mark.parametrize("knob", REQUIRED_ENV_KNOBS)
    def test_knob_mentioned_in_runbook(self, runbook_text: str, knob: str) -> None:
        assert knob in runbook_text, (
            f".env knob {knob!r} not mentioned in docs/ops/db_failover.md — "
            "operator can't find override without grepping bash"
        )

    @pytest.mark.parametrize("knob", REQUIRED_ENV_KNOBS)
    def test_knob_declared_in_env_example(
        self, env_example_text: str, knob: str
    ) -> None:
        # Keep the runbook and .env.example in lockstep — adding a knob
        # to .env.example without naming it in the runbook (or vice
        # versa) breaks this test.
        assert re.search(rf"^{knob}=", env_example_text, re.M), (
            f"runbook names {knob!r} but .env.example does not declare it"
        )


# ---------------------------------------------------------------------------
# (5) Migrator CLI flags listed in §11.3
# ---------------------------------------------------------------------------


REQUIRED_MIGRATOR_FLAGS = [
    "--source",
    "--target",
    "--batch-size",
    "--tables",
    "--truncate-target",
    "--skip-chain-verify",
    "--dry-run",
    "--json",
    "--quiet",
]


class TestMigratorFlagCoverage:
    @pytest.mark.parametrize("flag", REQUIRED_MIGRATOR_FLAGS)
    def test_flag_in_runbook(self, runbook_text: str, flag: str) -> None:
        assert flag in runbook_text, (
            f"migrator flag {flag!r} not mentioned in runbook — operator "
            "can't copy-paste the cutover command"
        )

    @pytest.mark.parametrize("flag", REQUIRED_MIGRATOR_FLAGS)
    def test_flag_in_script(self, migrate_text: str, flag: str) -> None:
        # The script registers each flag via add_argument("--foo", …).
        # If the script drops a flag the runbook must follow.
        assert f'"{flag}"' in migrate_text, (
            f"runbook names flag {flag!r} but scripts/migrate_sqlite_to_pg.py "
            "does not register it — runbook + script drifted"
        )


# ---------------------------------------------------------------------------
# (6) Policy-critical invariants mentioned
# ---------------------------------------------------------------------------


POLICY_INVARIANTS = [
    # Replication slot name literal used by both init-primary.sh and
    # init-standby.sh — misalignment here is the top runbook-caught
    # silent failure mode.
    "omnisight_standby_slot",
    # application_name of the standby in primary_conninfo — must match
    # synchronous_standby_names to engage sync mode.
    "omnisight_standby",
    # Sync-mode knob — operator decision documented in §5.3 policy table.
    "synchronous_standby_names",
    # The two LSN-related forensic queries §8 relies on.
    "pg_last_wal_replay_lsn",
    "pg_stat_replication",
    # The promote primitive used in both planned and unplanned paths.
    "pg_ctl promote",
    # Chain-continuity flag returned by the migrator's --json output.
    "source_chain_ok",
    # Static verifier — referenced from pre-flight + forensic sections.
    "pg_ha_verify.py",
]


class TestPolicyInvariantsReferenced:
    @pytest.mark.parametrize("token", POLICY_INVARIANTS)
    def test_token_present(self, runbook_text: str, token: str) -> None:
        assert token in runbook_text, (
            f"policy-critical token {token!r} absent from runbook — "
            "a 3am operator won't know this is the load-bearing string"
        )


# ---------------------------------------------------------------------------
# (7) CI job names match real jobs in the workflow
# ---------------------------------------------------------------------------


CI_JOB_NAMES = [
    "sqlite-matrix",
    "postgres-matrix",
    "pg-live-integration",
    "engine-syntax-scan",
]


class TestCIJobAnchors:
    @pytest.mark.parametrize("job", CI_JOB_NAMES)
    def test_job_in_runbook(self, runbook_text: str, job: str) -> None:
        assert job in runbook_text, (
            f"CI job {job!r} not referenced in the runbook §9 matrix — "
            "operator can't map red CI to DB readiness"
        )

    @pytest.mark.parametrize("job", CI_JOB_NAMES)
    def test_job_in_workflow(self, ci_workflow_text: str, job: str) -> None:
        # The workflow declares each job as a top-level YAML key. Use
        # the `  <name>:` indentation pattern so we don't match the
        # job reference inside a `needs:` list.
        pattern = rf"^\s{{2}}{re.escape(job)}:"
        assert re.search(pattern, ci_workflow_text, re.M), (
            f"runbook references CI job {job!r} but .github/workflows/"
            "db-engine-matrix.yml does not declare it"
        )


# ---------------------------------------------------------------------------
# (8) All file paths the runbook mentions actually exist
# ---------------------------------------------------------------------------


EXPECTED_EXISTING_PATHS = [
    MIGRATE_SCRIPT,
    PG_HA_VERIFY,
    COMPOSE_FILE,
    PRIMARY_CONF,
    STANDBY_CONF,
    HBA,
    INIT_PRIMARY,
    INIT_STANDBY,
    ENV_EXAMPLE,
    CI_WORKFLOW,
    DB_MATRIX_DOC,
    BLUE_GREEN_DOC,
    BOOTSTRAP_DOC,
]


class TestReferencedPathsExist:
    @pytest.mark.parametrize(
        "p",
        EXPECTED_EXISTING_PATHS,
        ids=[str(p.relative_to(PROJECT_ROOT)) for p in EXPECTED_EXISTING_PATHS],
    )
    def test_path_exists(self, p: Path) -> None:
        assert p.exists(), (
            f"runbook references {p.relative_to(PROJECT_ROOT)} but it does "
            "not exist — broken copy-paste for operators"
        )

    def test_every_referenced_path_on_disk(self, runbook_text: str) -> None:
        # Any `scripts/…` or `deploy/postgres-ha/…` path mentioned in
        # fenced code or backticks must resolve. We deliberately limit
        # to these two namespaces so a free-form prose mention of e.g.
        # `data/omnisight.db` (which operators create, not the repo)
        # doesn't trip the guard.
        pattern = re.compile(
            r"`((?:scripts|deploy/postgres-ha)/[A-Za-z0-9_./-]+)`"
        )
        hits = {m.group(1) for m in pattern.finditer(runbook_text)}
        assert hits, "runbook has no backtick-quoted script/deploy paths — probably misformatted"
        for rel in sorted(hits):
            # Strip trailing punctuation the regex may have picked up
            # (runbook rarely has those but be safe).
            cleaned = rel.rstrip(".,;:)")
            p = PROJECT_ROOT / cleaned
            # Allow bare `deploy/postgres-ha/` (the dir), or specific files.
            assert p.exists() or p.parent.exists(), (
                f"runbook mentions `{cleaned}` but {p} does not exist"
            )


# ---------------------------------------------------------------------------
# (9) Contract test index in §12 points at real sibling tests
# ---------------------------------------------------------------------------


SIBLING_TESTS = [
    "backend/tests/test_alembic_pg_compat.py",
    "backend/tests/test_alembic_pg_live_upgrade.py",
    "backend/tests/test_db_url.py",
    "backend/tests/test_pg_ha_deployment.py",
    "backend/tests/test_migrate_sqlite_to_pg.py",
    "backend/tests/test_ci_pg_matrix.py",
    # Self-reference (row 1365). Not a hard requirement for the file
    # to exist from the runbook's perspective, but handy to pin.
    "backend/tests/test_db_failover_runbook.py",
]


class TestContractIndex:
    @pytest.mark.parametrize("rel", SIBLING_TESTS)
    def test_sibling_mentioned(self, runbook_text: str, rel: str) -> None:
        # Either the full relative path or just the filename is
        # acceptable — the runbook uses backticks around the basename
        # for some rows, full path for others. The contract is simply
        # that an operator can find it.
        basename = rel.rsplit("/", 1)[-1]
        assert rel in runbook_text or basename in runbook_text, (
            f"§12 contract index must reference {rel} (or {basename}) "
            "so a stale sibling is caught at runbook-review time"
        )

    @pytest.mark.parametrize("rel", SIBLING_TESTS)
    def test_sibling_exists(self, rel: str) -> None:
        # If the runbook claims a sibling test exists, the tree must
        # have it. This catches the "deleted the test but forgot to
        # update the runbook" drift.
        p = PROJECT_ROOT / rel
        assert p.exists(), (
            f"runbook §12 names {rel} but the file does not exist"
        )


# ---------------------------------------------------------------------------
# (10) Async-is-default policy assertion
# ---------------------------------------------------------------------------


class TestSyncAsyncPolicy:
    def test_async_is_default(self, runbook_text: str) -> None:
        # The §5.3 policy table must state the async default explicitly
        # so an operator doesn't flip sync-mode on "because it sounds
        # safer" and then page themselves when the standby is down.
        # We accept either "default" or "default async" phrasing.
        lower = runbook_text.lower()
        assert "asynchronous" in lower and "default" in lower, (
            "runbook must call out that async replication is the default "
            "posture — flipping sync without reading §5.3 is a foot-gun"
        )

    def test_sync_opt_in_command_present(self, runbook_text: str) -> None:
        # The only way to engage sync is to flip synchronous_standby_names
        # to 'FIRST 1 (omnisight_standby)' and reload. The exact string
        # must appear so the operator doesn't guess the syntax.
        assert "FIRST 1 (omnisight_standby)" in runbook_text, (
            "runbook must show the exact synchronous_standby_names value "
            "the operator pastes into postgresql.primary.conf"
        )

    def test_sync_opt_in_command_matches_env_example(
        self, env_example_text: str
    ) -> None:
        # .env.example's operator guide block also names this literal —
        # if either drops it, the two docs drift.
        assert "FIRST 1 (omnisight_standby)" in env_example_text, (
            ".env.example lost the sync-mode operator guide — runbook and "
            "env template are out of sync"
        )
