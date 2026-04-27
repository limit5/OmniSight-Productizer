"""BS.10.5 — Lazy install coach hook unit tests.

Scope
─────
Locks the BS.10.1 / BS.10.2 / BS.10.3 helpers in ``backend/routers/invoke.py``
that detect a missing-toolchain coaching trigger from the operator's INVOKE
intent and turn it into both an LLM-friendly context block and a templated
fallback message that carries the BS.10.4 ``/settings/platforms?entry=<slug>``
deeplink.

Coverage axes (~10 cases, 1 per behaviour the BS.10 epic codifies):

  1.  ``_TOOLCHAIN_DISPLAY`` keys 1:1 with ``_TOOLCHAIN_KEYWORD_MAP`` —
      drift guard already raises at import time, this re-asserts the
      contract from a test run so a future refactor that moves the
      assertion can't silently drop the check.
  2.  ``_collect_toolchain_hints`` matches each of the 5 vertical
      primary entries case-insensitively (parametrize over verticals).
  3.  ``_build_coach_text_corpus`` includes ``backlog`` / ``assigned`` /
      ``in_progress`` task title+description and the running-agent
      ``thought_chain`` while excluding ``completed`` / ``blocked``
      tasks — locks the BS.10.1 corpus contract.
  4.  ``_resolve_tenant_id`` covers the Pydantic-``User`` and dict
      paths plus the ``"t-default"`` fallback.
  5.  ``_detect_coaching_triggers`` emits one ``missing_toolchain:<slug>``
      trigger per hinted-but-not-installed vertical, sorted, with the
      installed slug filtered out and per-slug ``suppress`` honoured.
  6.  ``_detect_coaching_triggers`` is a no-op when ``installed_entries``
      is absent (backwards-compat for callers that never pre-load).
  7.  ``_build_templated_coach_message`` renders the 1-of-1 missing
      phrasing with the canonical display name + deeplink and overrides
      the legacy ``empty_workspace`` framing.
  8.  ``_build_templated_coach_message`` renders the N-of-N missing
      phrasing with per-slug bullets and appends a stale-PEP reminder
      when both co-fire.
  9.  ``_build_coach_context`` (LLM-driven path) hands the LLM a fully
      populated bullet — display name + hint + install URL — for each
      missing-toolchain trigger so the LLM never has to translate the
      slug itself.
  10. ``_load_installed_entry_ids`` falls back to ``frozenset()`` when
      PG is unavailable (empty tenant_id short-circuit + uninitialised
      pool path) so the planner is never crashed by the lazy install
      coach hook.

These tests are PG-free and LLM-free by design — every helper under
test is either a pure function or an async function whose only IO
path is a ``try / except`` that gracefully degrades when the asyncpg
pool has not been initialised. PG-backed integration over the live
``install_jobs`` table is owned by ``test_installer_api.py`` (BS.2.4);
this row stays focused on the planner-side wiring so it remains green
on a developer machine that has no PG running.

Module-global / cross-worker state audit
────────────────────────────────────────
Pure test code. No mutable module-level state on the test side; every
helper under test reads either a frozen const map (``_TOOLCHAIN_KEYWORD_MAP``
/ ``_TOOLCHAIN_DISPLAY``) or its own immutable inputs. The one async
case exercises ``_load_installed_entry_ids`` against an *uninitialised*
``backend.db_pool``; we don't touch ``init_pool`` so we never leak a
pool between tests.
"""

from __future__ import annotations

import pytest

from backend.models import (
    Agent,
    AgentStatus,
    AgentType,
    Task,
    TaskStatus,
)
from backend.routers import invoke as inv


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  1) Display-vs-keyword drift guard (BS.10.2)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestToolchainTablesAligned:
    """``_TOOLCHAIN_DISPLAY`` must cover every slug in ``_TOOLCHAIN_KEYWORD_MAP``.

    The module-import-time ``assert`` already enforces this — having a
    runtime test as well documents the contract for human readers and
    catches a refactor that accidentally moves the check.
    """

    def test_display_keys_match_keyword_keys(self):
        assert set(inv._TOOLCHAIN_DISPLAY.keys()) == set(
            inv._TOOLCHAIN_KEYWORD_MAP.keys()
        )

    def test_keyword_map_covers_5_canonical_verticals(self):
        # Locked to BS.6.0 ``BOOTSTRAP_VERTICAL_PRIMARY_ENTRY`` slugs in
        # lib/api.ts; new toolchain rows are a code change, not catalog
        # drift, so this set is intentionally hard-coded.
        assert set(inv._TOOLCHAIN_KEYWORD_MAP.keys()) == {
            "android-sdk-platform-tools",
            "espressif-esp-idf-v5",
            "nodejs-lts-20",
            "python-uv",
            "arm-gnu-toolchain-13",
        }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  2) _collect_toolchain_hints — substring scan
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestCollectToolchainHints:

    @pytest.mark.parametrize("text,expected_slug", [
        ("Build APK for Android Studio emulator", "android-sdk-platform-tools"),
        ("flash ESP32-S3 with esp-idf v5", "espressif-esp-idf-v5"),
        ("npm install next.js typescript", "nodejs-lts-20"),
        ("uv pip install pyproject deps", "python-uv"),
        ("cross compile arm cortex-m for stm32", "arm-gnu-toolchain-13"),
    ])
    def test_hints_match_each_vertical_case_insensitive(self, text, expected_slug):
        # Mixed casing in the test inputs deliberately — the helper
        # lower-cases internally so the operator's literal command can
        # be in any form.
        hits = inv._collect_toolchain_hints(text.upper())
        assert expected_slug in hits

    def test_empty_text_returns_empty_set(self):
        assert inv._collect_toolchain_hints("") == set()
        assert inv._collect_toolchain_hints("   ") == set()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  3) _build_coach_text_corpus — corpus shape contract
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestBuildCoachTextCorpus:

    def _state(self) -> dict:
        backlog = Task(
            id="t-backlog", title="Build ESP32-S3 firmware",
            description="flash via esp-idf",
            status=TaskStatus.backlog,
        )
        in_progress = Task(
            id="t-in-progress", title="Run pytest on python-uv venv",
            description="uv pip install -r requirements.txt",
            status=TaskStatus.in_progress,
        )
        completed = Task(
            id="t-done", title="Old android adb deploy",
            description="historic — already shipped",
            status=TaskStatus.completed,
        )
        blocked = Task(
            id="t-blocked", title="cortex-m STM32 cross compile",
            description="blocked — should NOT count",
            status=TaskStatus.blocked,
        )
        running_agent = Agent(
            id="a-1", name="Worker", type=AgentType.firmware,
            status=AgentStatus.running,
            thought_chain="next step: arm-none-eabi-gcc -mcpu=cortex-m",
        )
        return {
            "tasks": [backlog, in_progress, completed, blocked],
            "running_agents": [running_agent],
        }

    def test_corpus_includes_command_and_pending_tasks(self):
        corpus = inv._build_coach_text_corpus(
            self._state(), "deploy android APK to emulator",
        )
        # command echo
        assert "deploy android" in corpus.lower()
        # backlog task title + description
        assert "esp32" in corpus.lower()
        assert "esp-idf" in corpus.lower()
        # in_progress task title + description
        assert "python-uv" in corpus.lower()
        assert "uv pip install" in corpus.lower()
        # running agent thought_chain
        assert "arm-none-eabi" in corpus.lower()

    def test_corpus_excludes_completed_and_blocked_tasks(self):
        corpus = inv._build_coach_text_corpus(self._state(), None)
        # completed-task content must not leak — coach is for work the
        # operator is *about to* run, not historical work.
        assert "historic" not in corpus.lower()
        # blocked-task content must not leak either — blocked work is
        # not actionable until the operator unblocks it.
        assert "should not count" not in corpus.lower()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  4) _resolve_tenant_id — Pydantic User / dict / fallback
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestResolveTenantId:

    def test_pydantic_user_with_tenant_id(self):
        from backend.auth import User
        u = User(
            id="u-1", email="op@example.com", name="Op", role="operator",
            enabled=True, tenant_id="t-acme",
        )
        assert inv._resolve_tenant_id(u) == "t-acme"

    def test_dict_user_with_tenant_id(self):
        # Degraded auth modes (open / pre-bootstrap) sometimes hand a
        # plain dict instead of the Pydantic model.
        assert inv._resolve_tenant_id({"tenant_id": "t-foo"}) == "t-foo"

    def test_no_tenant_falls_back_to_default(self):
        # Both code paths converge on "t-default" when no tenant is
        # advertised — matches every other tenant-aware router.
        assert inv._resolve_tenant_id({}) == "t-default"
        assert inv._resolve_tenant_id(None) == "t-default"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  5) _detect_coaching_triggers — primary emit + filter + suppress
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestDetectCoachingTriggers:

    def _empty_state_with_command_hints(self, installed=frozenset()):
        # Empty workspace + multi-vertical command → multiple slugs
        # hinted; planner should emit one missing_toolchain trigger per
        # hinted-but-not-installed slug, sorted alphabetically.
        return {
            "agents": [],
            "tasks": [],
            "running_agents": [],
            "idle_agents": [],
            "installed_entries": installed,
        }

    def test_missing_toolchain_emitted_per_slug_sorted(self):
        # Command hints android + esp32 + arm cortex-m. Nothing is
        # installed yet → 3 missing triggers, sorted alphabetically.
        state = self._empty_state_with_command_hints()
        triggers, _ = inv._detect_coaching_triggers(
            state, frozenset(),
            command="flash esp32 then build android APK + cortex-m firmware",
        )
        missing = [t for t in triggers if t.startswith("missing_toolchain:")]
        # Sorted alphabetically — guarantees deterministic UX across runs.
        assert missing == sorted(missing)
        assert "missing_toolchain:android-sdk-platform-tools" in missing
        assert "missing_toolchain:espressif-esp-idf-v5" in missing
        assert "missing_toolchain:arm-gnu-toolchain-13" in missing
        # empty_workspace co-fires (workspace is empty by construction).
        assert "empty_workspace" in triggers

    def test_installed_slug_filtered_out(self):
        # ESP-IDF already installed; command still hints at it but the
        # trigger is suppressed because the toolchain is on the box.
        state = self._empty_state_with_command_hints(
            installed=frozenset({"espressif-esp-idf-v5"}),
        )
        triggers, _ = inv._detect_coaching_triggers(
            state, frozenset(), command="flash esp32 firmware",
        )
        assert "missing_toolchain:espressif-esp-idf-v5" not in triggers

    def test_per_slug_suppress_filters_only_named_slug(self):
        # Operator dismissed the android coach earlier this session;
        # the esp32 coach should still fire because suppress is
        # per-slug, not per-trigger-family.
        state = self._empty_state_with_command_hints()
        suppress = frozenset({"missing_toolchain:android-sdk-platform-tools"})
        triggers, _ = inv._detect_coaching_triggers(
            state, suppress,
            command="build android APK and flash esp32",
        )
        assert "missing_toolchain:android-sdk-platform-tools" not in triggers
        assert "missing_toolchain:espressif-esp-idf-v5" in triggers

    def test_backwards_compat_when_installed_entries_absent(self):
        # Caller did not pre-load installed_entries (e.g. legacy code
        # path or a unit test). Detector must NOT emit any
        # missing_toolchain triggers — it has no ground truth for
        # "installed".
        state = {
            "agents": [], "tasks": [], "running_agents": [], "idle_agents": [],
        }
        triggers, _ = inv._detect_coaching_triggers(
            state, frozenset(), command="flash esp32 with esp-idf",
        )
        assert all(not t.startswith("missing_toolchain:") for t in triggers)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  6) _build_templated_coach_message — LLM-off fallback
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestBuildTemplatedCoachMessage:

    def test_one_missing_overrides_empty_workspace_with_deeplink(self):
        # Single missing toolchain co-fires with empty_workspace; the
        # missing-toolchain banner takes priority and the empty-workspace
        # framing is dropped because the operator has already declared
        # intent (typed the command).
        msg = inv._build_templated_coach_message(
            triggers=[
                "empty_workspace",
                "missing_toolchain:espressif-esp-idf-v5",
            ],
            pending_count=0,
        )
        # Display name (not slug) is used — operator can paste straight
        # into a search.
        assert "ESP-IDF v5" in msg
        # Deeplink shape locked to BS.10.4 frontend handler.
        assert "/settings/platforms?entry=espressif-esp-idf-v5" in msg
        # Empty-workspace framing must NOT appear — install-first-then-run
        # is the productive path.
        assert "工作台是空的喔" not in msg
        assert "/tour" not in msg

    def test_many_missing_with_pep_co_fire_appends_reminder(self):
        # Three missing toolchains + 2 stale PEP HOLDs co-firing — the
        # banner enumerates each toolchain on its own bullet and adds
        # ONE short reminder line about the pending PEP queue.
        msg = inv._build_templated_coach_message(
            triggers=[
                "stale_pep",
                "missing_toolchain:android-sdk-platform-tools",
                "missing_toolchain:espressif-esp-idf-v5",
                "missing_toolchain:nodejs-lts-20",
            ],
            pending_count=2,
        )
        # Per-slug bullet for each missing toolchain.
        assert "Android SDK Platform Tools" in msg
        assert "ESP-IDF v5" in msg
        assert "Node.js LTS 20" in msg
        # Per-slug deeplink for each missing toolchain.
        assert "/settings/platforms?entry=android-sdk-platform-tools" in msg
        assert "/settings/platforms?entry=espressif-esp-idf-v5" in msg
        assert "/settings/platforms?entry=nodejs-lts-20" in msg
        # PEP reminder appears as ONE additional line — count by line
        # to avoid catching the banner accidentally.
        reminder_lines = [
            line for line in msg.splitlines()
            if "PEP HOLD" in line and "2" in line
        ]
        assert len(reminder_lines) == 1


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  7) _build_coach_context — LLM-driven path
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestBuildCoachContext:

    def test_missing_toolchain_block_includes_name_hint_and_url(self):
        # The LLM context block must hand the LLM the canonical display
        # name + a one-line hint + the install URL so it never needs to
        # translate the slug itself (BS.10.3 prompt design).
        block = inv._build_coach_context(
            triggers=["missing_toolchain:python-uv"],
            pending_count=0,
        )
        assert "Python toolchain (uv)" in block  # display name
        assert "uv pip" in block                  # hint
        assert "/settings/platforms?entry=python-uv" in block  # deeplink


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  8) _load_installed_entry_ids — graceful PG-unavailable fallback
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestLoadInstalledEntryIdsFallback:
    """The lazy install coach hook is best-effort. PG unavailable / pool
    not initialised / SELECT raises must NOT crash the planner — it
    degrades to "no missing-toolchain triggers" which is strictly safer
    than a 500 on the INVOKE endpoint.
    """

    async def test_empty_tenant_id_short_circuits_to_empty_set(self):
        # Defensive guard for callers that hand an empty/None tenant
        # (degraded auth modes, pre-bootstrap, etc.).
        out = await inv._load_installed_entry_ids("")
        assert out == frozenset()
        assert isinstance(out, frozenset)

    async def test_pg_unavailable_returns_empty_set(self, monkeypatch):
        # Force the pool acquisition to raise — the helper's broad
        # except branch must catch it and degrade to frozenset().
        from backend import db_pool as _db_pool

        def _boom():
            raise RuntimeError("db_pool not initialised in this test")

        monkeypatch.setattr(_db_pool, "get_pool", _boom)
        out = await inv._load_installed_entry_ids("t-default")
        assert out == frozenset()
        assert isinstance(out, frozenset)
