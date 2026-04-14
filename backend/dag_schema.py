"""Phase 56-DAG-A — DAG schema for the self-healing planner.

Pydantic models that mirror the Orchestrator template's JSON contract
(`docs/design/self-healing-scheduling-mechanism.md` §三). Schema-level
validation runs automatically on `DAG.model_validate(payload)`. The
**semantic** rules (cycles, tier capability, MECE, dep closure, I/O
entity) live in `backend/dag_validator.py` so they can be invoked
once and produce a list of all errors at once instead of failing on
the first.

`schema_version: int = 1` lets us evolve without breaking historic
plans stored in `dag_plans` (Phase 56-DAG-B).
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator


TierId = Literal["t1", "networked", "t3"]
SCHEMA_VERSION = 1


class Task(BaseModel):
    """One node in the DAG."""

    task_id: str = Field(..., min_length=1, max_length=64)
    description: str = Field(..., min_length=1, max_length=4000)

    required_tier: TierId
    toolchain: str = Field(..., min_length=1, max_length=128)

    inputs: list[str] = Field(default_factory=list)
    expected_output: str = Field(..., min_length=1, max_length=512)
    depends_on: list[str] = Field(default_factory=list)

    # Phase 56-DAG-A — explicit MECE escape hatch. Two tasks may share
    # an `expected_output` only when BOTH set this to True (e.g. parallel
    # benchmark runs writing the same report path that's later merged).
    output_overlap_ack: bool = False

    @field_validator("task_id")
    @classmethod
    def _id_is_simple(cls, v: str) -> str:
        # Keep id printable — used as Decision Engine key + as part of
        # workflow_steps.idempotency_key (Phase 56-DAG-B).
        if not v.replace("-", "").replace("_", "").isalnum():
            raise ValueError(
                "task_id must be alphanumeric / dash / underscore only"
            )
        return v

    @field_validator("depends_on")
    @classmethod
    def _no_self_dep(cls, v: list[str], info) -> list[str]:
        tid = info.data.get("task_id")
        if tid and tid in v:
            raise ValueError(f"task {tid!r} cannot depend on itself")
        if len(v) != len(set(v)):
            raise ValueError("depends_on must not contain duplicates")
        return v


class DAG(BaseModel):
    """The full planning artefact."""

    schema_version: int = SCHEMA_VERSION
    dag_id: str = Field(..., min_length=1, max_length=64)
    total_tasks: Optional[int] = None
    tasks: list[Task]

    @field_validator("schema_version")
    @classmethod
    def _supported_version(cls, v: int) -> int:
        if v != SCHEMA_VERSION:
            # Keep simple for v1; raise instead of silent migrate.
            raise ValueError(
                f"unsupported schema_version {v} (expected {SCHEMA_VERSION})"
            )
        return v

    @field_validator("tasks")
    @classmethod
    def _at_least_one(cls, v: list[Task]) -> list[Task]:
        if not v:
            raise ValueError("DAG must have at least one task")
        return v
