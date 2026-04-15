"""C5 — L4-CORE-05 Skill manifest schema (#214).

Pydantic model for ``skill.yaml`` — the formal manifest that every skill
pack must ship inside ``configs/skills/<name>/skill.yaml``.

The manifest declares metadata, required artifacts, compatible SoCs,
dependency on other skills/core modules, and lifecycle hook commands.
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field, field_validator

SCHEMA_VERSION = 1

REQUIRED_ARTIFACT_KINDS = frozenset({"tasks", "scaffolds", "tests", "hil", "docs"})


class ArtifactRef(BaseModel):
    kind: str = Field(..., description="One of: tasks, scaffolds, tests, hil, docs")
    path: str = Field(..., min_length=1, max_length=512, description="Relative path within skill dir")

    @field_validator("kind")
    @classmethod
    def _check_kind(cls, v: str) -> str:
        if v not in REQUIRED_ARTIFACT_KINDS:
            raise ValueError(
                f"artifact kind must be one of {sorted(REQUIRED_ARTIFACT_KINDS)}, got {v!r}"
            )
        return v


class LifecycleHooks(BaseModel):
    model_config = {"populate_by_name": True}

    install: str = Field("", max_length=1024, description="Shell command to run on install")
    validate_cmd: str = Field("", max_length=1024, alias="validate", description="Shell command for validation")
    enumerate_cmd: str = Field("", max_length=1024, alias="enumerate", description="Shell command to list provided capabilities")


class SkillManifest(BaseModel):
    schema_version: int = SCHEMA_VERSION
    name: str = Field(..., min_length=1, max_length=128, pattern=r"^[a-z][a-z0-9\-]*$")
    description: str = Field("", max_length=1024)
    version: str = Field("0.1.0", max_length=32, pattern=r"^\d+\.\d+\.\d+")
    author: str = Field("", max_length=256)
    license: str = Field("", max_length=64)

    compatible_socs: list[str] = Field(
        default_factory=list,
        description="SoC patterns this skill supports (empty = all)",
    )
    depends_on_skills: list[str] = Field(
        default_factory=list,
        description="Other skill pack names this skill requires",
    )
    depends_on_core: list[str] = Field(
        default_factory=list,
        description="L4-CORE modules required (e.g. CORE-16 for OTA)",
    )

    artifacts: list[ArtifactRef] = Field(
        default_factory=list,
        description="Declared artifact files/dirs the skill provides",
    )
    hooks: LifecycleHooks = Field(default_factory=LifecycleHooks)
    keywords: list[str] = Field(default_factory=list)

    @field_validator("schema_version")
    @classmethod
    def _check_version(cls, v: int) -> int:
        if v != SCHEMA_VERSION:
            raise ValueError(f"unsupported schema_version {v}, expected {SCHEMA_VERSION}")
        return v

    def artifact_kinds_present(self) -> set[str]:
        return {a.kind for a in self.artifacts}

    def missing_artifact_kinds(self) -> set[str]:
        return REQUIRED_ARTIFACT_KINDS - self.artifact_kinds_present()
