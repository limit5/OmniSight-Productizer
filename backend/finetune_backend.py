"""Phase 65 S3 — fine-tune backend abstraction.

Three implementations behind one `FinetuneBackend` Protocol, picked
at runtime via `OMNISIGHT_FINETUNE_BACKEND`:

  noop      Default. Synthetic job handle, polls return "succeeded"
            immediately. Useful for dev / staging / opt-out prod.
  openai    Submits via the openai SDK fine-tune endpoints. Raises
            `BackendUnavailable` if the package isn't installed; raises
            `BackendUnavailable` if `OPENAI_API_KEY` is unset.
  unsloth   Submits via a local `unsloth-cli` subprocess. For prod
            this MUST be invoked inside a Phase 64-B Tier 2 sandbox
            (egress to the model registry); the v1 module accepts a
            `runner` injection point so the caller (Phase 65 S4
            nightly) controls that.

This module is pure code — no side effects at import time. The S4
nightly orchestrator owns scheduling, JSONL handoff, and audit log.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Optional, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Errors + dataclasses
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class BackendUnavailable(RuntimeError):
    """Selected backend's runtime prerequisite (SDK, env var, binary)
    isn't available. Caller should fall back to noop or skip."""


JobState = str  # "queued" | "running" | "succeeded" | "failed" | "cancelled"


@dataclass
class JobHandle:
    """Opaque handle returned by submit(). The string `external_id` is
    backend-specific (OpenAI fine-tune ID, Unsloth pid, etc.)."""
    backend: str
    external_id: str
    submitted_at: float
    metadata: dict = field(default_factory=dict)


@dataclass
class JobStatus:
    handle: JobHandle
    state: JobState
    fine_tuned_model: Optional[str] = None  # filled when state="succeeded"
    error: Optional[str] = None             # filled when state="failed"
    raw: Optional[dict] = None              # backend-native payload


# Injectable shell runner — `(cmd: list[str]) → (rc, stdout, stderr)`.
# The unsloth backend uses this so callers (Phase 65 S4) can route the
# command through `container.exec_in_container` for the T2 sandbox.
ShellRunner = Callable[[list[str]], Awaitable[tuple[int, str, str]]]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Protocol
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@runtime_checkable
class FinetuneBackend(Protocol):
    name: str

    async def submit(self, jsonl_path: Path, *,
                     base_model: str, suffix: str = "") -> JobHandle: ...

    async def poll(self, handle: JobHandle) -> JobStatus: ...


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Noop — opt-out / dev / unit tests
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class NoopBackend:
    """Records what would have happened. submit() returns a synthetic
    handle; poll() returns succeeded immediately with a fake model id
    so the caller's pipeline can be exercised end-to-end without
    actually fine-tuning anything."""
    name = "noop"

    async def submit(self, jsonl_path: Path, *,
                     base_model: str, suffix: str = "") -> JobHandle:
        eid = f"noop-{uuid.uuid4().hex[:10]}"
        logger.info(
            "[noop-finetune] would submit %s base=%s suffix=%r → %s",
            jsonl_path, base_model, suffix, eid,
        )
        return JobHandle(
            backend=self.name, external_id=eid,
            submitted_at=time.time(),
            metadata={"jsonl_path": str(jsonl_path),
                      "base_model": base_model,
                      "suffix": suffix or ""},
        )

    async def poll(self, handle: JobHandle) -> JobStatus:
        base = handle.metadata.get("base_model", "unknown")
        suffix = handle.metadata.get("suffix") or "noop"
        return JobStatus(
            handle=handle, state="succeeded",
            fine_tuned_model=f"ft:{base}:{suffix}-{handle.external_id[-6:]}",
            raw={"synthetic": True},
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  OpenAI — real, gated by SDK + env key
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class OpenAIBackend:
    """Submits via openai.fine_tuning.jobs.create.

    Raises BackendUnavailable on import-time absence of the SDK or
    runtime absence of OPENAI_API_KEY. Network errors during submit
    raise the SDK's native exception; the S4 nightly orchestrator
    catches + records.
    """
    name = "openai"

    def __init__(self, *, base_model_default: str = "gpt-4o-mini-2024-07-18"):
        self.base_model_default = base_model_default

    def _client(self):
        try:
            import openai  # type: ignore
        except ImportError as exc:
            raise BackendUnavailable(
                "openai SDK not installed; pip install openai"
            ) from exc
        if not (os.environ.get("OPENAI_API_KEY") or "").strip():
            raise BackendUnavailable("OPENAI_API_KEY env var is empty")
        return openai.OpenAI()

    async def submit(self, jsonl_path: Path, *,
                     base_model: str, suffix: str = "") -> JobHandle:
        client = self._client()

        def _do() -> tuple[str, str]:
            with open(jsonl_path, "rb") as fh:
                file_obj = client.files.create(file=fh, purpose="fine-tune")
            job = client.fine_tuning.jobs.create(
                training_file=file_obj.id,
                model=base_model or self.base_model_default,
                suffix=suffix or None,
            )
            return file_obj.id, job.id

        file_id, job_id = await asyncio.to_thread(_do)
        logger.info("[openai-finetune] submitted job=%s file=%s", job_id, file_id)
        return JobHandle(
            backend=self.name, external_id=job_id,
            submitted_at=time.time(),
            metadata={
                "training_file": file_id,
                "base_model": base_model or self.base_model_default,
                "suffix": suffix or "",
            },
        )

    async def poll(self, handle: JobHandle) -> JobStatus:
        client = self._client()
        job = await asyncio.to_thread(
            client.fine_tuning.jobs.retrieve, handle.external_id,
        )
        # SDK normalises status → "validating_files" / "queued" /
        # "running" / "succeeded" / "failed" / "cancelled". Map.
        sdk_status = getattr(job, "status", "running")
        state = {
            "validating_files": "queued",
            "queued": "queued",
            "running": "running",
            "succeeded": "succeeded",
            "failed": "failed",
            "cancelled": "cancelled",
        }.get(sdk_status, "running")
        return JobStatus(
            handle=handle, state=state,
            fine_tuned_model=getattr(job, "fine_tuned_model", None),
            error=getattr(getattr(job, "error", None), "message", None),
            raw={"sdk_status": sdk_status},
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Unsloth — local subprocess via injected shell runner
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class UnslothBackend:  # noqa: D401 — audit-fix H1: was UnsloththBackend (typo)
    """Submits a fine-tune job via a local Unsloth CLI subprocess.

    The `runner` argument is the injection point — production callers
    pass a wrapper around `container.exec_in_container` so the actual
    `unsloth` invocation runs INSIDE a Phase 64-B Tier 2 sandbox
    (which has the public-internet egress required for hub model
    pulls). Without that wrapper the runner defaults to local
    `asyncio.create_subprocess_exec`, suitable only for dev.
    """
    name = "unsloth"

    def __init__(self, *,
                 cli: str = "unsloth-cli",
                 runner: Optional[ShellRunner] = None):
        self.cli = cli
        self._runner: ShellRunner = runner or _default_local_runner

    async def submit(self, jsonl_path: Path, *,
                     base_model: str, suffix: str = "") -> JobHandle:
        eid = f"unsloth-{uuid.uuid4().hex[:8]}"
        cmd = [
            self.cli, "submit",
            "--data", str(jsonl_path),
            "--base", base_model,
            "--suffix", suffix or "omnisight",
            "--job-id", eid,
        ]
        rc, out, err = await self._runner(cmd)
        if rc != 0:
            raise BackendUnavailable(
                f"unsloth-cli submit failed (rc={rc}): {err.strip() or out.strip()}"
            )
        logger.info("[unsloth-finetune] submitted %s", eid)
        return JobHandle(
            backend=self.name, external_id=eid,
            submitted_at=time.time(),
            metadata={"base_model": base_model,
                      "suffix": suffix or "omnisight",
                      "submit_stdout": out[-400:]},
        )

    async def poll(self, handle: JobHandle) -> JobStatus:
        cmd = [self.cli, "status", "--job-id", handle.external_id]
        rc, out, err = await self._runner(cmd)
        if rc != 0:
            return JobStatus(handle=handle, state="failed",
                             error=err.strip() or out.strip())
        # Convention: CLI prints "STATUS: <state>\nMODEL: <name>" so
        # we don't need a JSON parser here. Robust enough for v1.
        state = "running"
        model = None
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("STATUS:"):
                state = line.split(":", 1)[1].strip().lower()
            elif line.startswith("MODEL:"):
                model = line.split(":", 1)[1].strip() or None
        return JobStatus(handle=handle, state=state,
                         fine_tuned_model=model, raw={"stdout": out})


async def _default_local_runner(cmd: list[str]) -> tuple[int, str, str]:
    """Fallback runner — local subprocess. NEVER use this in prod for
    real fine-tune jobs; T0 backend should not be running unsloth
    directly. Wrap container.exec_in_container instead."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    return (proc.returncode or 0,
            stdout.decode(errors="replace"),
            stderr.decode(errors="replace"))


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Factory
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def select_backend(name: Optional[str] = None) -> FinetuneBackend:
    """Pick a backend by name (or env if not supplied). Unknown names
    fall back to noop + log a warning so prod misconfig fails LOUD
    in the logs but doesn't crash the orchestrator."""
    raw = (name or os.environ.get("OMNISIGHT_FINETUNE_BACKEND")
           or "noop").strip().lower()
    if raw == "noop":
        return NoopBackend()
    if raw == "openai":
        return OpenAIBackend()
    if raw == "unsloth":
        return UnslothBackend()
    logger.warning(
        "unknown OMNISIGHT_FINETUNE_BACKEND=%r; falling back to noop", raw,
    )
    return NoopBackend()


# Back-compat alias for the typo the class shipped with. Remove in the
# next phase after downstream imports catch up.
UnsloththBackend = UnslothBackend
