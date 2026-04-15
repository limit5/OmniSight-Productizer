"""C5 — Unit tests for the skill pack framework (#214).

Covers:
  - SkillManifest schema validation (valid/invalid)
  - ArtifactRef kind validation
  - LifecycleHooks model
  - Missing artifact kind detection
  - Skill registry: list / get / validate / install / enumerate
  - Contract test: every skill must provide 5 artifact kinds
  - Registry convention: configs/skills/<name>/
  - Lifecycle hook execution (install/validate/enumerate)
  - API endpoints: /skills/list, /skills/registry/{name},
    /skills/registry/{name}/validate, /skills/install
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
import yaml

from backend.skill_manifest import (
    REQUIRED_ARTIFACT_KINDS,
    SCHEMA_VERSION,
    ArtifactRef,
    LifecycleHooks,
    SkillManifest,
)
from backend.skill_registry import (
    ValidationIssue,
    ValidationResult,
    _detect_artifact_kinds,
    _inspect_skill,
    enumerate_skill,
    get_skill,
    install_skill,
    list_skills,
    load_manifest,
    validate_skill,
)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Fixtures
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _write_yaml(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.dump(data, default_flow_style=False), encoding="utf-8")


def _make_complete_skill(base: Path, name: str, **overrides) -> Path:
    """Create a fully valid skill pack directory with all 5 artifacts."""
    skill_dir = base / name
    skill_dir.mkdir(parents=True, exist_ok=True)

    manifest = {
        "schema_version": 1,
        "name": name,
        "description": f"Test skill: {name}",
        "version": "1.0.0",
        "author": "test",
        "artifacts": [
            {"kind": "tasks", "path": "tasks.yaml"},
            {"kind": "scaffolds", "path": "scaffolds/"},
            {"kind": "tests", "path": "tests/"},
            {"kind": "hil", "path": "hil/"},
            {"kind": "docs", "path": "docs/"},
        ],
        "keywords": ["test"],
    }
    manifest.update(overrides)
    _write_yaml(skill_dir / "skill.yaml", manifest)

    # Create actual artifact files/dirs
    _write_yaml(skill_dir / "tasks.yaml", {"schema_version": 1, "tasks": []})
    (skill_dir / "scaffolds").mkdir(exist_ok=True)
    (skill_dir / "tests").mkdir(exist_ok=True)
    (skill_dir / "hil").mkdir(exist_ok=True)
    (skill_dir / "docs").mkdir(exist_ok=True)

    return skill_dir


@pytest.fixture
def registry(tmp_path: Path) -> Path:
    """Create a temporary skill registry with a few test packs."""
    return tmp_path / "skills"


@pytest.fixture
def populated_registry(registry: Path) -> Path:
    """Registry with 3 installed skills: 2 complete, 1 legacy."""
    registry.mkdir(parents=True, exist_ok=True)

    _make_complete_skill(registry, "skill-alpha")
    _make_complete_skill(registry, "skill-beta", compatible_socs=["Hi3516", "RK3566"])

    # Legacy skill (no manifest, only SKILL.md + tasks.yaml)
    legacy = registry / "legacy-skill"
    legacy.mkdir()
    (legacy / "SKILL.md").write_text("---\nname: legacy\n---\n# Legacy\n")
    _write_yaml(legacy / "tasks.yaml", {"schema_version": 1, "tasks": []})

    return registry


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  1. SkillManifest schema validation
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestSkillManifest:
    def test_valid_minimal(self):
        m = SkillManifest(name="my-skill")
        assert m.name == "my-skill"
        assert m.schema_version == SCHEMA_VERSION
        assert m.version == "0.1.0"
        assert m.artifacts == []

    def test_valid_full(self):
        m = SkillManifest(
            name="uvc-camera",
            description="UVC 1.5 skill",
            version="2.0.0",
            author="Team Alpha",
            license="MIT",
            compatible_socs=["Hi3516", "RK3566"],
            depends_on_skills=["skill-alpha"],
            depends_on_core=["CORE-16"],
            artifacts=[
                ArtifactRef(kind="tasks", path="tasks.yaml"),
                ArtifactRef(kind="scaffolds", path="scaffolds/"),
                ArtifactRef(kind="tests", path="tests/"),
                ArtifactRef(kind="hil", path="hil/"),
                ArtifactRef(kind="docs", path="docs/"),
            ],
            hooks=LifecycleHooks(
                install="make install",
                validate="make check",
                enumerate="make list-caps",
            ),
            keywords=["uvc", "camera"],
        )
        assert m.name == "uvc-camera"
        assert len(m.artifacts) == 5
        assert m.artifact_kinds_present() == REQUIRED_ARTIFACT_KINDS
        assert m.missing_artifact_kinds() == set()

    def test_invalid_name_uppercase(self):
        with pytest.raises(Exception):
            SkillManifest(name="MySkill")

    def test_invalid_name_empty(self):
        with pytest.raises(Exception):
            SkillManifest(name="")

    def test_invalid_name_starts_with_number(self):
        with pytest.raises(Exception):
            SkillManifest(name="1skill")

    def test_invalid_schema_version(self):
        with pytest.raises(Exception):
            SkillManifest(name="my-skill", schema_version=99)

    def test_invalid_version_format(self):
        with pytest.raises(Exception):
            SkillManifest(name="my-skill", version="abc")

    def test_missing_artifact_kinds(self):
        m = SkillManifest(
            name="partial",
            artifacts=[
                ArtifactRef(kind="tasks", path="tasks.yaml"),
                ArtifactRef(kind="docs", path="docs/"),
            ],
        )
        assert m.missing_artifact_kinds() == {"scaffolds", "tests", "hil"}

    def test_all_artifact_kinds_present(self):
        m = SkillManifest(
            name="complete",
            artifacts=[
                ArtifactRef(kind=k, path=f"{k}/")
                for k in REQUIRED_ARTIFACT_KINDS
            ],
        )
        assert m.missing_artifact_kinds() == set()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  2. ArtifactRef validation
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestArtifactRef:
    def test_valid_kinds(self):
        for k in REQUIRED_ARTIFACT_KINDS:
            ref = ArtifactRef(kind=k, path=f"{k}/")
            assert ref.kind == k

    def test_invalid_kind(self):
        with pytest.raises(Exception):
            ArtifactRef(kind="invalid", path="foo/")

    def test_empty_path_rejected(self):
        with pytest.raises(Exception):
            ArtifactRef(kind="tasks", path="")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  3. LifecycleHooks
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestLifecycleHooks:
    def test_empty_hooks(self):
        h = LifecycleHooks()
        assert h.install == ""
        assert h.validate_cmd == ""
        assert h.enumerate_cmd == ""

    def test_hooks_with_commands(self):
        h = LifecycleHooks(
            install="make install",
            validate_cmd="make check",
            enumerate_cmd="echo caps",
        )
        assert h.install == "make install"
        assert h.validate_cmd == "make check"
        assert h.enumerate_cmd == "echo caps"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  4. load_manifest
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestLoadManifest:
    def test_valid_yaml(self, tmp_path: Path):
        data = {
            "schema_version": 1,
            "name": "test-skill",
            "description": "A test",
            "version": "1.0.0",
            "artifacts": [{"kind": "tasks", "path": "tasks.yaml"}],
        }
        path = tmp_path / "skill.yaml"
        _write_yaml(path, data)
        m = load_manifest(path)
        assert m.name == "test-skill"
        assert len(m.artifacts) == 1

    def test_invalid_yaml_not_dict(self, tmp_path: Path):
        path = tmp_path / "skill.yaml"
        path.write_text("just a string", encoding="utf-8")
        with pytest.raises(ValueError, match="YAML mapping"):
            load_manifest(path)

    def test_invalid_manifest_fields(self, tmp_path: Path):
        data = {"schema_version": 1, "name": "INVALID-NAME"}
        path = tmp_path / "skill.yaml"
        _write_yaml(path, data)
        with pytest.raises(Exception):
            load_manifest(path)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  5. _detect_artifact_kinds (heuristic)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestDetectArtifactKinds:
    def test_all_present(self, tmp_path: Path):
        skill = tmp_path / "my-skill"
        skill.mkdir()
        _write_yaml(skill / "tasks.yaml", {"tasks": []})
        (skill / "scaffolds").mkdir()
        (skill / "tests").mkdir()
        (skill / "hil").mkdir()
        (skill / "docs").mkdir()
        kinds = _detect_artifact_kinds(skill)
        assert kinds == REQUIRED_ARTIFACT_KINDS

    def test_partial(self, tmp_path: Path):
        skill = tmp_path / "partial"
        skill.mkdir()
        _write_yaml(skill / "tasks.yaml", {"tasks": []})
        kinds = _detect_artifact_kinds(skill)
        assert kinds == {"tasks"}

    def test_empty(self, tmp_path: Path):
        skill = tmp_path / "empty"
        skill.mkdir()
        kinds = _detect_artifact_kinds(skill)
        assert kinds == set()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  6. list_skills
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestListSkills:
    def test_empty_registry(self, registry: Path):
        assert list_skills(registry) == []

    def test_nonexistent_registry(self, tmp_path: Path):
        assert list_skills(tmp_path / "nope") == []

    def test_populated(self, populated_registry: Path):
        skills = list_skills(populated_registry)
        names = [s.name for s in skills]
        assert "skill-alpha" in names
        assert "skill-beta" in names
        assert "legacy-skill" in names

    def test_skips_internal_prefix(self, registry: Path):
        registry.mkdir(parents=True)
        (registry / "_internal").mkdir()
        _make_complete_skill(registry, "visible")
        skills = list_skills(registry)
        names = [s.name for s in skills]
        assert "visible" in names
        assert "_internal" not in names

    def test_skill_info_fields(self, populated_registry: Path):
        skills = list_skills(populated_registry)
        alpha = next(s for s in skills if s.name == "skill-alpha")
        assert alpha.has_manifest is True
        assert alpha.manifest is not None
        assert alpha.manifest.name == "skill-alpha"
        assert alpha.artifact_kinds == REQUIRED_ARTIFACT_KINDS

    def test_legacy_skill_info(self, populated_registry: Path):
        skills = list_skills(populated_registry)
        legacy = next(s for s in skills if s.name == "legacy-skill")
        assert legacy.has_manifest is False
        assert legacy.manifest is None
        assert legacy.has_skill_md is True
        assert legacy.has_tasks_yaml is True
        assert "tasks" in legacy.artifact_kinds


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  7. get_skill
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestGetSkill:
    def test_existing(self, populated_registry: Path):
        info = get_skill("skill-alpha", populated_registry)
        assert info is not None
        assert info.name == "skill-alpha"

    def test_nonexistent(self, populated_registry: Path):
        info = get_skill("nope", populated_registry)
        assert info is None

    def test_internal_prefix(self, registry: Path):
        registry.mkdir(parents=True)
        (registry / "_hidden").mkdir()
        info = get_skill("_hidden", registry)
        assert info is None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  8. validate_skill
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestValidateSkill:
    def test_valid_complete_skill(self, populated_registry: Path):
        result = validate_skill("skill-alpha", populated_registry)
        assert result.ok is True
        assert result.errors == []

    def test_missing_directory(self, populated_registry: Path):
        result = validate_skill("nonexistent", populated_registry)
        assert result.ok is False
        assert any("not found" in e.message for e in result.errors)

    def test_missing_manifest(self, populated_registry: Path):
        result = validate_skill("legacy-skill", populated_registry)
        assert result.ok is False
        assert any("manifest not found" in e.message for e in result.errors)

    def test_name_mismatch(self, registry: Path):
        registry.mkdir(parents=True)
        skill_dir = _make_complete_skill(registry, "wrong-name")
        # Overwrite manifest with mismatched name
        _write_yaml(skill_dir / "skill.yaml", {
            "schema_version": 1,
            "name": "different-name",
            "artifacts": [{"kind": k, "path": f"{k}/" if k != "tasks" else "tasks.yaml"}
                          for k in REQUIRED_ARTIFACT_KINDS],
        })
        result = validate_skill("wrong-name", registry)
        assert result.ok is False
        assert any("does not match" in e.message for e in result.errors)

    def test_missing_artifact_kinds(self, registry: Path):
        registry.mkdir(parents=True)
        skill = registry / "partial"
        skill.mkdir()
        manifest = {
            "schema_version": 1,
            "name": "partial",
            "artifacts": [
                {"kind": "tasks", "path": "tasks.yaml"},
            ],
        }
        _write_yaml(skill / "skill.yaml", manifest)
        _write_yaml(skill / "tasks.yaml", {"tasks": []})
        result = validate_skill("partial", registry)
        assert result.ok is False
        assert any("missing required artifact kinds" in e.message for e in result.errors)

    def test_declared_artifact_not_on_disk(self, registry: Path):
        registry.mkdir(parents=True)
        skill = registry / "ghost-artifact"
        skill.mkdir()
        manifest = {
            "schema_version": 1,
            "name": "ghost-artifact",
            "artifacts": [
                {"kind": k, "path": f"{k}/"}
                for k in REQUIRED_ARTIFACT_KINDS
            ],
        }
        _write_yaml(skill / "skill.yaml", manifest)
        # only create tasks.yaml, nothing else
        _write_yaml(skill / "tasks.yaml", {"tasks": []})
        result = validate_skill("ghost-artifact", registry)
        assert result.ok is False
        assert any("not found" in e.message for e in result.errors)

    def test_dependency_warning(self, registry: Path):
        registry.mkdir(parents=True)
        _make_complete_skill(
            registry, "depends-on-missing",
            depends_on_skills=["nonexistent-dep"],
        )
        result = validate_skill("depends-on-missing", registry)
        assert any(
            "nonexistent-dep" in w.message
            for w in result.warnings
        )

    def test_validate_hook_success(self, registry: Path):
        registry.mkdir(parents=True)
        _make_complete_skill(
            registry, "hook-ok",
            hooks={"install": "", "validate": "true", "enumerate": ""},
        )
        result = validate_skill("hook-ok", registry)
        assert result.ok is True

    def test_validate_hook_failure(self, registry: Path):
        registry.mkdir(parents=True)
        _make_complete_skill(
            registry, "hook-fail",
            hooks={"install": "", "validate": "false", "enumerate": ""},
        )
        result = validate_skill("hook-fail", registry)
        assert result.ok is False
        assert any("validate hook failed" in e.message for e in result.errors)

    def test_invalid_manifest_yaml(self, registry: Path):
        registry.mkdir(parents=True)
        skill = registry / "bad-yaml"
        skill.mkdir()
        (skill / "skill.yaml").write_text("name: BAD-NAME\nschema_version: 1\n")
        result = validate_skill("bad-yaml", registry)
        assert result.ok is False
        assert any("parse error" in e.message for e in result.errors)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  9. install_skill
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestInstallSkill:
    def test_install_new(self, tmp_path: Path, registry: Path):
        registry.mkdir(parents=True)
        src = _make_complete_skill(tmp_path / "src", "new-skill")
        info = install_skill(src, skills_dir=registry)
        assert info.name == "new-skill"
        assert (registry / "new-skill" / "skill.yaml").exists()

    def test_install_overwrite(self, tmp_path: Path, registry: Path):
        registry.mkdir(parents=True)
        src = _make_complete_skill(tmp_path / "src", "overwrite-me")
        install_skill(src, skills_dir=registry)
        # update source
        _write_yaml(
            src / "skill.yaml",
            {
                "schema_version": 1, "name": "overwrite-me", "version": "2.0.0",
                "artifacts": [{"kind": k, "path": f"{k}/"} for k in REQUIRED_ARTIFACT_KINDS],
            },
        )
        info = install_skill(src, skills_dir=registry, overwrite=True)
        manifest = load_manifest(registry / "overwrite-me" / "skill.yaml")
        assert manifest.version == "2.0.0"

    def test_install_exists_no_overwrite(self, tmp_path: Path, registry: Path):
        registry.mkdir(parents=True)
        src = _make_complete_skill(tmp_path / "src", "dup")
        install_skill(src, skills_dir=registry)
        with pytest.raises(FileExistsError):
            install_skill(src, skills_dir=registry, overwrite=False)

    def test_install_not_a_directory(self, tmp_path: Path, registry: Path):
        registry.mkdir(parents=True)
        fake = tmp_path / "not-a-dir.txt"
        fake.write_text("hello")
        with pytest.raises(ValueError, match="directory"):
            install_skill(fake, skills_dir=registry)

    def test_install_with_name_override(self, tmp_path: Path, registry: Path):
        registry.mkdir(parents=True)
        src = _make_complete_skill(tmp_path / "src", "original")
        info = install_skill(src, name="renamed", skills_dir=registry)
        assert info.name == "renamed"
        assert (registry / "renamed" / "skill.yaml").exists()

    def test_install_hook_runs(self, tmp_path: Path, registry: Path):
        registry.mkdir(parents=True)
        src = _make_complete_skill(
            tmp_path / "src", "hook-install",
            hooks={"install": "touch installed.marker", "validate": "", "enumerate": ""},
        )
        install_skill(src, skills_dir=registry)
        assert (registry / "hook-install" / "installed.marker").exists()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  10. enumerate_skill
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestEnumerateSkill:
    def test_enumerate_with_manifest(self, populated_registry: Path):
        info = enumerate_skill("skill-beta", populated_registry)
        assert info["name"] == "skill-beta"
        assert info["has_manifest"] is True
        assert info["version"] == "1.0.0"
        assert "Hi3516" in info["compatible_socs"]
        assert set(info["artifact_kinds"]) == REQUIRED_ARTIFACT_KINDS

    def test_enumerate_legacy(self, populated_registry: Path):
        info = enumerate_skill("legacy-skill", populated_registry)
        assert info["name"] == "legacy-skill"
        assert info["has_manifest"] is False
        assert "tasks" in info["artifact_kinds"]

    def test_enumerate_hook_yaml_output(self, registry: Path):
        registry.mkdir(parents=True)
        _make_complete_skill(
            registry, "enum-hook",
            hooks={
                "install": "",
                "validate": "",
                "enumerate": "echo 'capabilities:\n  - streaming\n  - recording'",
            },
        )
        info = enumerate_skill("enum-hook", registry)
        assert "capabilities" in info or "capabilities_raw" in info


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  11. Contract test: 5 required artifacts
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestContractFiveArtifacts:
    """Every formal skill must provide all 5 artifact kinds:
    tasks, scaffolds, tests, hil, docs."""

    def test_required_kinds_constant(self):
        assert REQUIRED_ARTIFACT_KINDS == {"tasks", "scaffolds", "tests", "hil", "docs"}

    def test_complete_skill_passes_contract(self, registry: Path):
        registry.mkdir(parents=True)
        _make_complete_skill(registry, "contract-ok")
        result = validate_skill("contract-ok", registry)
        assert result.ok is True

    def test_missing_one_kind_fails(self, registry: Path):
        registry.mkdir(parents=True)
        skill = registry / "missing-hil"
        skill.mkdir()
        manifest = {
            "schema_version": 1,
            "name": "missing-hil",
            "artifacts": [
                {"kind": "tasks", "path": "tasks.yaml"},
                {"kind": "scaffolds", "path": "scaffolds/"},
                {"kind": "tests", "path": "tests/"},
                # missing hil
                {"kind": "docs", "path": "docs/"},
            ],
        }
        _write_yaml(skill / "skill.yaml", manifest)
        _write_yaml(skill / "tasks.yaml", {"tasks": []})
        (skill / "scaffolds").mkdir()
        (skill / "tests").mkdir()
        (skill / "docs").mkdir()
        result = validate_skill("missing-hil", registry)
        assert result.ok is False
        assert any("hil" in e.message for e in result.errors)

    def test_missing_all_artifacts_fails(self, registry: Path):
        registry.mkdir(parents=True)
        skill = registry / "no-artifacts"
        skill.mkdir()
        manifest = {"schema_version": 1, "name": "no-artifacts", "artifacts": []}
        _write_yaml(skill / "skill.yaml", manifest)
        result = validate_skill("no-artifacts", registry)
        assert result.ok is False
        missing_msg = [e for e in result.errors if "missing required" in e.message]
        assert len(missing_msg) == 1
        for kind in REQUIRED_ARTIFACT_KINDS:
            assert kind in missing_msg[0].message


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  12. ValidationResult helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestValidationResult:
    def test_errors_and_warnings(self):
        r = ValidationResult(
            skill_name="test",
            ok=False,
            issues=[
                ValidationIssue("error", "bad thing"),
                ValidationIssue("warning", "suspicious thing"),
                ValidationIssue("error", "another bad"),
            ],
        )
        assert len(r.errors) == 2
        assert len(r.warnings) == 1

    def test_empty_issues(self):
        r = ValidationResult(skill_name="test", ok=True)
        assert r.errors == []
        assert r.warnings == []


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  13. _inspect_skill
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestInspectSkill:
    def test_inspect_complete(self, registry: Path):
        registry.mkdir(parents=True)
        _make_complete_skill(registry, "inspectable")
        info = _inspect_skill(registry / "inspectable")
        assert info.name == "inspectable"
        assert info.has_manifest is True
        assert info.has_tasks_yaml is True
        assert info.artifact_kinds == REQUIRED_ARTIFACT_KINDS

    def test_inspect_legacy(self, tmp_path: Path):
        legacy = tmp_path / "legacy"
        legacy.mkdir()
        (legacy / "SKILL.md").write_text("# Legacy")
        info = _inspect_skill(legacy)
        assert info.has_manifest is False
        assert info.has_skill_md is True
        assert info.artifact_kinds == set()

    def test_inspect_bad_manifest_falls_back_to_heuristic(self, tmp_path: Path):
        skill = tmp_path / "bad-manifest"
        skill.mkdir()
        (skill / "skill.yaml").write_text("name: BAD_NAME\nschema_version: 1\n")
        _write_yaml(skill / "tasks.yaml", {"tasks": []})
        (skill / "scaffolds").mkdir()
        info = _inspect_skill(skill)
        assert info.has_manifest is True
        assert info.manifest is None
        assert "tasks" in info.artifact_kinds
        assert "scaffolds" in info.artifact_kinds


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  14. Edge cases
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestEdgeCases:
    def test_duplicate_artifact_kinds(self):
        m = SkillManifest(
            name="dup-arts",
            artifacts=[
                ArtifactRef(kind="tasks", path="tasks.yaml"),
                ArtifactRef(kind="tasks", path="extra-tasks.yaml"),
                ArtifactRef(kind="scaffolds", path="scaffolds/"),
                ArtifactRef(kind="tests", path="tests/"),
                ArtifactRef(kind="hil", path="hil/"),
                ArtifactRef(kind="docs", path="docs/"),
            ],
        )
        assert m.artifact_kinds_present() == REQUIRED_ARTIFACT_KINDS
        assert m.missing_artifact_kinds() == set()

    def test_manifest_keywords_list(self):
        m = SkillManifest(name="kw", keywords=["a", "b", "c"])
        assert m.keywords == ["a", "b", "c"]

    def test_manifest_depends_on_core(self):
        m = SkillManifest(
            name="deps",
            depends_on_core=["CORE-16", "CORE-15"],
        )
        assert "CORE-16" in m.depends_on_core

    def test_compatible_socs_empty_means_all(self):
        m = SkillManifest(name="universal")
        assert m.compatible_socs == []

    def test_lifecycle_hooks_default_empty(self):
        m = SkillManifest(name="no-hooks")
        assert m.hooks.install == ""
        assert m.hooks.validate_cmd == ""
        assert m.hooks.enumerate_cmd == ""
