"""C5 — L4-CORE-05 Skill manifest schema (#214).

Pydantic model for ``skill.yaml`` — the formal manifest that every skill
pack must ship inside ``configs/skills/<name>/skill.yaml``.

The manifest declares metadata, required artifacts, compatible SoCs,
dependency on other skills/core modules, and lifecycle hook commands.
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field, field_validator, model_validator

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
    skill_id: str | None = Field(
        None,
        min_length=1,
        max_length=128,
        pattern=r"^[a-z][a-z0-9_-]*$",
        description="Stable slug; when omitted, name is treated as the legacy slug",
    )
    name: str = Field(..., min_length=1, max_length=128)
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

    # 1F.P2a (OP-1827) — cross-pack wiring tokens. Free-form capability
    # labels used by the system-of-systems product planner
    # (backend.product_planner.compose_product) to wire one pack's
    # `requires` to another pack's `provides` when composing several packs
    # into one product DAG. Both are OPTIONAL and unconstrained: a token is
    # an arbitrary string (e.g. "rtsp_stream", or an "external:"/"user:"
    # token the planner treats as caller-satisfied). Declaring these on
    # REAL packs is P2b — this field only adds the schema seam.
    provides: list[str] = Field(
        default_factory=list,
        description="Free-form capability tokens this pack provides to other packs",
    )
    requires: list[str] = Field(
        default_factory=list,
        description="Free-form capability tokens this pack requires from other packs",
    )

    artifacts: list[ArtifactRef] = Field(
        default_factory=list,
        description="Declared artifact files/dirs the skill provides",
    )
    hooks: LifecycleHooks = Field(default_factory=LifecycleHooks)
    keywords: list[str] = Field(default_factory=list)

    # Optional scaffolder platform_profile pin. When set, the dispatcher
    # (via skill_registry.resolve_scaffolder) overrides the resolved
    # scaffolder's default platform_profile for this pack only — without
    # mutating the shared scaffolder default. The intended consumer is a
    # reuse pack whose target OS differs from the borrowed scaffolder's
    # default profile (e.g. windows-uvc-host borrows the desktop-tauri
    # scaffolder, which defaults to linux-x86_64-native, but must render a
    # Windows target).
    platform_profile: Optional[str] = Field(
        default=None,
        max_length=64,
        pattern=r"^[A-Za-z0-9_.-]+$",
        description="Scaffolder platform_profile id to pin for this pack",
    )

    @field_validator("schema_version")
    @classmethod
    def _check_version(cls, v: int) -> int:
        if v != SCHEMA_VERSION:
            raise ValueError(f"unsupported schema_version {v}, expected {SCHEMA_VERSION}")
        return v

    @model_validator(mode="after")
    def _check_slug(self) -> "SkillManifest":
        if self.skill_id is None and not self._slug_is_valid(self.name):
            raise ValueError("name must be a valid skill slug when skill_id is absent")
        return self

    @staticmethod
    def _slug_is_valid(v: str) -> bool:
        if not v:
            return False
        first, rest = v[0], v[1:]
        if first < "a" or first > "z":
            return False
        return all(("a" <= ch <= "z") or ("0" <= ch <= "9") or ch in "-_" for ch in rest)

    def artifact_kinds_present(self) -> set[str]:
        return {a.kind for a in self.artifacts}

    def missing_artifact_kinds(self) -> set[str]:
        return REQUIRED_ARTIFACT_KINDS - self.artifact_kinds_present()
