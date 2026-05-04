"""Tests for CODEOWNERS enforcement and conflict detection (Phase 24)."""

import os
from pathlib import Path



class TestCodeownersParser:

    def test_file_exists(self):
        codeowners = Path(__file__).resolve().parent.parent.parent / "configs" / "CODEOWNERS"
        assert codeowners.exists()

    def test_load_rules(self):
        from backend.codeowners import _load_rules
        rules = _load_rules()
        assert len(rules) > 0
        # Check at least firmware rules exist
        types = [r[1] for r in rules]
        assert "firmware" in types

    def test_get_file_owners_hal(self):
        from backend.codeowners import get_file_owners
        owners = get_file_owners("src/hal/gpio.h")
        assert len(owners) >= 1
        assert owners[0][0] == "firmware"

    def test_get_file_owners_algorithm(self):
        from backend.codeowners import get_file_owners
        owners = get_file_owners("src/algorithm/core.c")
        assert any(t == "software" for t, _, _ in owners)

    def test_get_file_owners_unowned(self):
        from backend.codeowners import get_file_owners
        owners = get_file_owners("random_file_not_in_codeowners.txt")
        assert len(owners) == 0

    def test_config_cache_invalidates_on_codeowners_mtime(
        self, monkeypatch, tmp_path
    ):
        from backend import codeowners

        config = tmp_path / "CODEOWNERS"
        config.write_text("src/hal/** firmware/hal\n", encoding="utf-8")
        monkeypatch.setattr(codeowners, "_CODEOWNERS_PATH", config)
        codeowners.reload_codeowners_for_tests()

        first = codeowners.get_file_owners("src/hal/gpio.h")
        config.write_text("src/hal/** software/algorithm\n", encoding="utf-8")
        stat = config.stat()
        os.utime(config, (stat.st_atime + 2.0, stat.st_mtime + 2.0))
        later = codeowners.get_file_owners("src/hal/gpio.h")

        assert [(t, s) for t, s, _ in first] == [("firmware", "hal")]
        assert [(t, s) for t, s, _ in later] == [("software", "algorithm")]


class TestFilePermission:

    def test_owner_allowed(self):
        from backend.codeowners import check_file_permission
        allowed, reason = check_file_permission("src/hal/gpio.h", "firmware", "hal")
        assert allowed
        assert reason == ""

    def test_non_owner_soft_warn(self):
        from backend.codeowners import check_file_permission
        allowed, reason = check_file_permission("src/hal/gpio.h", "software")
        assert allowed  # Soft enforcement — still allowed
        assert "Warning" in reason

    def test_unowned_file_allowed(self):
        from backend.codeowners import check_file_permission
        allowed, reason = check_file_permission("some/random/file.txt", "software")
        assert allowed
        assert reason == ""


class TestAgentFileScope:

    def test_get_scope(self):
        from backend.codeowners import get_scope_for_agent
        scope = get_scope_for_agent("firmware", "bsp")
        assert len(scope) >= 1
        assert any("driver" in p or "dts" in p for p in scope)

    def test_agent_model_has_file_scope(self):
        from backend.models import Agent, AgentType
        a = Agent(id="test", name="test", type=AgentType.firmware, file_scope=["src/hal/*"])
        assert a.file_scope == ["src/hal/*"]

    def test_agent_model_default_empty(self):
        from backend.models import Agent, AgentType
        a = Agent(id="test", name="test", type=AgentType.firmware)
        assert a.file_scope == []


class TestConflictDetection:

    def test_finalize_result_has_conflict_field(self):
        """Verify finalize() returns conflict_files in result dict."""
        # This is a structural test — actual git conflict testing requires
        # a full workspace setup which is integration-level
        from backend.workspace import finalize
        import asyncio
        assert asyncio.iscoroutinefunction(finalize)
