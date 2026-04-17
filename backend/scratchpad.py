"""R3 (#309) — Scratchpad Memory Offload + Auto-Continuation.

Per-agent persistent "scratchpad" file where the agent parks its
working state: current task, progress, blockers, next steps, and a
summary of surrounding context. The scratchpad has two jobs:

* **Memory offload.** An agent that must juggle a long ReAct loop
  keeps the distilled state on disk (at-rest encrypted via Fernet) so
  we can prune the live prompt without losing the through-line. When
  the agent is restarted or crash-recovered (R4), ``reload_latest()``
  rehydrates the saved markdown into the context head.

* **Auto-continuation buffer.** When the LLM adapter returns
  ``stop_reason=max_tokens`` the ``AutoContinuation`` helper repeatedly
  re-prompts the model with "please continue from where you were
  truncated" and stitches the deltas together. The scratchpad records
  the continuation rounds so the UI can attach an "↩ auto-continued"
  tag to the stitched message.

Structured markdown format — the five H2 sections are fixed so every
reader (future-agent, operator UI preview, test fixture) can look up a
field without parsing a bespoke schema::

    ## Current Task
    ## Progress
    ## Blockers
    ## Next Steps
    ## Context Summary

Storage layout::

    data/agents/<agent_id>/
        scratchpad.md           # latest ciphertext snapshot
        scratchpad.meta.json    # plaintext header: turn / bytes / updated_at
        archive/
            scratchpad-<iso>.md # historical snapshots + on-success archives

On success the active scratchpad is archived; on failure we keep the
active pointer in place so a human can inspect the failing state.

Everything here is best-effort — scratchpad IO must never block an
agent step. Callers use the module-level ``save()`` / ``reload_latest()``
shortcuts and tolerate a ``None`` return.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

logger = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Paths
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_DATA_ROOT = _PROJECT_ROOT / "data" / "agents"


def _data_root() -> Path:
    """Override point for tests. ``OMNISIGHT_SCRATCHPAD_ROOT`` wins."""
    override = os.environ.get("OMNISIGHT_SCRATCHPAD_ROOT", "").strip()
    if override:
        return Path(override)
    return _DEFAULT_DATA_ROOT


_AGENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-.:@]{0,127}$")


def _validate_agent_id(agent_id: str) -> str:
    """Reject path traversal / empty agent IDs up front.

    Scratchpad paths are composed from ``agent_id`` so a ``..`` slip
    would escape the data directory; a hard reject is cheaper than a
    post-hoc resolve check.
    """
    if not agent_id or not _AGENT_ID_RE.match(agent_id):
        raise ValueError(f"invalid agent_id: {agent_id!r}")
    return agent_id


def agent_dir(agent_id: str) -> Path:
    return _data_root() / _validate_agent_id(agent_id)


def scratchpad_path(agent_id: str) -> Path:
    return agent_dir(agent_id) / "scratchpad.md"


def meta_path(agent_id: str) -> Path:
    return agent_dir(agent_id) / "scratchpad.meta.json"


def archive_dir(agent_id: str) -> Path:
    return agent_dir(agent_id) / "archive"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Model
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

SECTIONS: tuple[str, ...] = (
    "Current Task",
    "Progress",
    "Blockers",
    "Next Steps",
    "Context Summary",
)


@dataclass
class ScratchpadState:
    agent_id: str
    current_task: str = ""
    progress: str = ""
    blockers: str = ""
    next_steps: str = ""
    context_summary: str = ""
    turn: int = 0
    total_turns: int = 0
    subtask: str | None = None
    trigger: str = "manual"
    updated_at: float = 0.0

    def with_advance(self, trigger: str = "turn_interval") -> "ScratchpadState":
        return replace(self, trigger=trigger, turn=self.turn + 1, updated_at=time.time())

    def sections_count(self) -> int:
        """How many sections are non-empty — used for the UI progress ring."""
        return sum(1 for v in (
            self.current_task, self.progress, self.blockers,
            self.next_steps, self.context_summary,
        ) if v.strip())

    def to_markdown(self) -> str:
        return render_markdown(self)


def render_markdown(state: ScratchpadState) -> str:
    """Serialise to the fixed-section markdown the reader expects."""
    body = [
        f"<!-- scratchpad: agent_id={state.agent_id} turn={state.turn}"
        f" subtask={state.subtask or '-'} trigger={state.trigger} -->",
        f"# Scratchpad — {state.agent_id}",
        "",
        "## Current Task",
        state.current_task.strip() or "_(no current task yet)_",
        "",
        "## Progress",
        state.progress.strip() or "_(none)_",
        "",
        "## Blockers",
        state.blockers.strip() or "_(none)_",
        "",
        "## Next Steps",
        state.next_steps.strip() or "_(none)_",
        "",
        "## Context Summary",
        state.context_summary.strip() or "_(none)_",
        "",
    ]
    return "\n".join(body)


_SECTION_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)


def parse_markdown(agent_id: str, text: str) -> ScratchpadState:
    """Tolerant reverse of ``render_markdown``.

    Unknown sections are dropped silently. Missing sections → empty
    string, not an error, so agents that upgrade the schema don't
    explode reading old snapshots.
    """
    sections: dict[str, str] = {}
    matches = list(_SECTION_RE.finditer(text))
    for i, m in enumerate(matches):
        name = m.group(1).strip()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        sections[name] = text[start:end].strip()

    def _clean(val: str) -> str:
        return "" if val in ("_(none)_", "_(no current task yet)_") else val

    return ScratchpadState(
        agent_id=agent_id,
        current_task=_clean(sections.get("Current Task", "")),
        progress=_clean(sections.get("Progress", "")),
        blockers=_clean(sections.get("Blockers", "")),
        next_steps=_clean(sections.get("Next Steps", "")),
        context_summary=_clean(sections.get("Context Summary", "")),
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Crypto — reuse the repo Fernet key
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _encrypt(plaintext: str) -> bytes:
    """Scratchpads may contain code snippets + design decisions — the
    same threat model as tokens, so we reuse ``secret_store`` Fernet.
    If encryption fails (cryptography missing), we fall back to plain
    utf-8 with a sentinel prefix so the file is still readable. That
    fallback is fine for dev boxes — production must have Fernet.
    """
    try:
        from backend import secret_store
        return secret_store.encrypt(plaintext).encode("ascii")
    except Exception as exc:  # pragma: no cover - dev-fallback
        logger.warning("scratchpad encrypt fallback (%s) — file will be plaintext", exc)
        return b"# PLAINTEXT-FALLBACK\n" + plaintext.encode("utf-8")


def _decrypt(blob: bytes) -> str:
    if blob.startswith(b"# PLAINTEXT-FALLBACK\n"):
        return blob[len(b"# PLAINTEXT-FALLBACK\n"):].decode("utf-8", errors="replace")
    try:
        from backend import secret_store
        return secret_store.decrypt(blob.decode("ascii"))
    except Exception as exc:
        logger.warning("scratchpad decrypt failed (%s) — returning blob as-is", exc)
        return blob.decode("utf-8", errors="replace")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Per-agent write lock registry
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_locks: dict[str, threading.Lock] = {}
_registry_lock = threading.Lock()


def _agent_lock(agent_id: str) -> threading.Lock:
    with _registry_lock:
        lk = _locks.get(agent_id)
        if lk is None:
            lk = threading.Lock()
            _locks[agent_id] = lk
        return lk


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Save / Load / Archive
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@dataclass
class SaveResult:
    agent_id: str
    turn: int
    size_bytes: int
    sections_count: int
    path: Path
    trigger: str
    archived: bool = False
    meta: dict = field(default_factory=dict)


def save(
    state: ScratchpadState,
    *,
    trigger: str = "turn_interval",
    task_id: str | None = None,
    emit: bool = True,
) -> SaveResult:
    """Write the scratchpad for ``agent_id`` atomically.

    Atomicity: we write to ``scratchpad.md.tmp`` then ``os.replace`` so
    a mid-write crash can't leave a half-encrypted file. Meta is saved
    the same way. The returned ``SaveResult`` is what the HTTP router
    exposes so tests can cheaply assert without re-reading disk.
    """
    _validate_agent_id(state.agent_id)
    lk = _agent_lock(state.agent_id)
    with lk:
        agent_dir(state.agent_id).mkdir(parents=True, exist_ok=True)

        state = replace(state, trigger=trigger, updated_at=time.time())
        plaintext = render_markdown(state)
        blob = _encrypt(plaintext)

        target = scratchpad_path(state.agent_id)
        tmp = target.with_suffix(".md.tmp")
        tmp.write_bytes(blob)
        os.replace(tmp, target)

        meta = {
            "agent_id": state.agent_id,
            "turn": state.turn,
            "total_turns": state.total_turns,
            "size_bytes": len(blob),
            "sections_count": state.sections_count(),
            "subtask": state.subtask,
            "trigger": trigger,
            "task_id": task_id,
            "updated_at": state.updated_at,
            "updated_at_iso": (
                datetime.fromtimestamp(state.updated_at, tz=timezone.utc).isoformat()
                if state.updated_at else None
            ),
        }
        meta_tmp = meta_path(state.agent_id).with_suffix(".json.tmp")
        meta_tmp.write_text(json.dumps(meta, indent=2), encoding="utf-8")
        os.replace(meta_tmp, meta_path(state.agent_id))

        result = SaveResult(
            agent_id=state.agent_id,
            turn=state.turn,
            size_bytes=len(blob),
            sections_count=state.sections_count(),
            path=target,
            trigger=trigger,
            meta=meta,
        )

    # Metrics + SSE are best-effort and live OUTSIDE the lock.
    _post_save_broadcast(result, task_id=task_id, emit=emit)
    return result


def _post_save_broadcast(result: SaveResult, *, task_id: str | None, emit: bool) -> None:
    try:
        from backend import metrics as _m
        _m.scratchpad_saves_total.labels(
            agent_id=result.agent_id, trigger=result.trigger,
        ).inc()
        _m.scratchpad_size_bytes.labels(agent_id=result.agent_id).set(result.size_bytes)
    except Exception:
        pass
    if not emit:
        return
    try:
        from backend.events import emit_agent_scratchpad_saved
        emit_agent_scratchpad_saved(
            agent_id=result.agent_id,
            turn=result.turn,
            size_bytes=result.size_bytes,
            sections_count=result.sections_count,
            trigger=result.trigger,
            task_id=task_id,
        )
    except Exception as exc:
        logger.debug("emit_agent_scratchpad_saved failed: %s", exc)


def reload_latest(agent_id: str) -> ScratchpadState | None:
    """Read back the most recent scratchpad, or ``None`` if the agent
    has no prior state.

    Crash-recovery path: if the ``.md.tmp`` sibling exists but the
    main file does not (torn write), we fall back to the tmp so the
    agent can still resume — at worst the operator sees a slightly
    older snapshot.
    """
    _validate_agent_id(agent_id)
    target = scratchpad_path(agent_id)
    tmp = target.with_suffix(".md.tmp")
    source: Path | None = None
    if target.exists():
        source = target
    elif tmp.exists():
        source = tmp
    if source is None:
        return None
    try:
        text = _decrypt(source.read_bytes())
    except Exception as exc:
        logger.warning("reload_latest(%s) decrypt failed: %s", agent_id, exc)
        return None
    state = parse_markdown(agent_id, text)

    mp = meta_path(agent_id)
    if mp.exists():
        try:
            meta = json.loads(mp.read_text(encoding="utf-8"))
            state.turn = int(meta.get("turn", state.turn) or 0)
            state.total_turns = int(meta.get("total_turns", state.total_turns) or 0)
            state.subtask = meta.get("subtask")
            state.trigger = meta.get("trigger", state.trigger)
            state.updated_at = float(meta.get("updated_at", state.updated_at) or 0.0)
        except Exception:
            pass
    return state


def read_meta(agent_id: str) -> dict | None:
    _validate_agent_id(agent_id)
    mp = meta_path(agent_id)
    if not mp.exists():
        return None
    try:
        return json.loads(mp.read_text(encoding="utf-8"))
    except Exception:
        return None


def list_agents() -> list[str]:
    """Every directory under ``data/agents`` that has a scratchpad.md."""
    root = _data_root()
    if not root.exists():
        return []
    out: list[str] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        if (child / "scratchpad.md").exists() or (child / "scratchpad.md.tmp").exists():
            out.append(child.name)
    return out


def archive_on_success(agent_id: str, *, label: str = "success") -> Path | None:
    """Move the active scratchpad under ``archive/`` — called when a
    task succeeds. Meta file moves too so the UI can render the final
    header after the move.
    """
    _validate_agent_id(agent_id)
    lk = _agent_lock(agent_id)
    with lk:
        src = scratchpad_path(agent_id)
        if not src.exists():
            return None
        archive_dir(agent_id).mkdir(parents=True, exist_ok=True)
        ts = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        dst = archive_dir(agent_id) / f"scratchpad-{label}-{ts}.md"
        os.replace(src, dst)
        mp = meta_path(agent_id)
        if mp.exists():
            os.replace(mp, archive_dir(agent_id) / f"scratchpad-{label}-{ts}.meta.json")
        return dst


def retain_for_debug(agent_id: str, *, note: str = "") -> Path | None:
    """Failure counterpart to ``archive_on_success``. The ACTIVE
    scratchpad stays in place (so a post-mortem reload picks it up);
    we just write a breadcrumb archive copy so later successes don't
    overwrite the forensic snapshot.
    """
    _validate_agent_id(agent_id)
    lk = _agent_lock(agent_id)
    with lk:
        src = scratchpad_path(agent_id)
        if not src.exists():
            return None
        archive_dir(agent_id).mkdir(parents=True, exist_ok=True)
        ts = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        suffix = "-".join(p for p in ("debug", note) if p)
        dst = archive_dir(agent_id) / f"scratchpad-{suffix}-{ts}.md"
        dst.write_bytes(src.read_bytes())
        return dst


def list_archive(agent_id: str) -> list[dict]:
    _validate_agent_id(agent_id)
    ad = archive_dir(agent_id)
    if not ad.exists():
        return []
    out: list[dict] = []
    for p in sorted(ad.iterdir()):
        if not p.is_file() or not p.name.startswith("scratchpad-") or not p.name.endswith(".md"):
            continue
        stat = p.stat()
        out.append({
            "name": p.name,
            "size_bytes": stat.st_size,
            "modified_at": stat.st_mtime,
            "modified_at_iso": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
        })
    return out


def preview_markdown(agent_id: str, *, max_chars: int = 8000) -> str | None:
    """Return the decrypted markdown, truncated for UI preview.

    Kept separate from ``reload_latest`` so the UI path does not rehydrate
    the ScratchpadState dataclass; the read-only preview just needs text.
    """
    state = reload_latest(agent_id)
    if state is None:
        return None
    text = state.to_markdown()
    if len(text) > max_chars:
        return text[:max_chars] + f"\n\n<!-- truncated at {max_chars} chars -->"
    return text


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Auto-continuation helper
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


MAX_TOKENS_STOP_REASONS: frozenset[str] = frozenset({
    "max_tokens", "length", "LENGTH",
    "max_output_tokens", "MAX_TOKENS",
})


def is_truncated(stop_reason: str | None) -> bool:
    if not stop_reason:
        return False
    return str(stop_reason).lower() in {s.lower() for s in MAX_TOKENS_STOP_REASONS}


@dataclass
class ContinuationOutcome:
    """Aggregate of an auto-continuation run.

    ``text`` is the stitched output (all rounds concatenated). ``rounds``
    counts only continuation rounds — the original truncated response
    is round 0. ``reached_limit`` is True if we gave up before
    ``stop_reason`` went back to ``end_turn``.
    """
    text: str
    rounds: int
    reached_limit: bool
    provider: str
    finishing_stop_reason: str | None


CONTINUE_PROMPT = (
    "The previous response was truncated because it hit the output token "
    "limit. Continue from exactly where it was cut off — do not repeat text "
    "already produced, and do not restart the explanation. Pick up on the "
    "same sentence or code line and finish."
)


class AutoContinuation:
    """Lightweight stitcher around any callable that returns
    ``(text, stop_reason)``.

    The class is intentionally decoupled from LangChain / the real LLM
    adapter so tests can drop in a fake function. In production the
    caller wires this up around ``invoke_chat`` (llm_adapter.py).
    """

    def __init__(self, *, max_rounds: int = 4, provider: str = "unknown") -> None:
        self.max_rounds = max(1, max_rounds)
        self.provider = provider

    def run(
        self,
        first: tuple[str, str | None],
        continue_fn,
        *,
        agent_id: str | None = None,
        task_id: str | None = None,
        emit: bool = True,
    ) -> ContinuationOutcome:
        """``first`` is the initial (text, stop_reason) tuple. ``continue_fn``
        is a callable that given the current stitched text returns the
        next (delta_text, stop_reason) pair.
        """
        text, stop = first
        rounds = 0
        reached_limit = False
        while is_truncated(stop) and rounds < self.max_rounds:
            rounds += 1
            try:
                delta, stop = continue_fn(text)
            except Exception as exc:
                logger.warning("auto-continuation round %d failed: %s", rounds, exc)
                break
            if delta:
                # Prepend a thin separator so readers can spot the seams
                # when scrolling the stitched output. The separator is
                # intentionally on its own line so it won't split mid-code.
                text = _stitch(text, delta)
            _bump_continuation(agent_id=agent_id, provider=self.provider)
            if emit:
                _emit_continuation(
                    agent_id=agent_id,
                    task_id=task_id,
                    provider=self.provider,
                    round_idx=rounds,
                    total=rounds,
                    appended=len(delta or ""),
                )
        if is_truncated(stop) and rounds >= self.max_rounds:
            reached_limit = True
        return ContinuationOutcome(
            text=text,
            rounds=rounds,
            reached_limit=reached_limit,
            provider=self.provider,
            finishing_stop_reason=stop,
        )


def _stitch(prior: str, delta: str) -> str:
    """Concatenate with a newline only when necessary.

    If ``prior`` ends mid-word we append immediately; otherwise we
    insert a newline so the output doesn't visually run sentences
    together. This is a heuristic — callers that need byte-exact
    stitching should track indices themselves.
    """
    if not prior:
        return delta
    if prior.endswith(("\n", " ", "\t")):
        return prior + delta.lstrip("\n")
    # If prior ends on punctuation end-of-sentence, add a newline so
    # the next round doesn't get glued to the last sentence.
    if prior[-1] in ".!?}])":
        return prior + "\n" + delta.lstrip("\n")
    return prior + delta


def _bump_continuation(*, agent_id: str | None, provider: str) -> None:
    try:
        from backend import metrics as _m
        _m.token_continuation_total.labels(
            agent_id=agent_id or "-", provider=provider,
        ).inc()
    except Exception:
        pass


def _emit_continuation(*, agent_id, task_id, provider, round_idx, total, appended):
    try:
        from backend.events import emit_agent_token_continuation
        emit_agent_token_continuation(
            agent_id=agent_id or "-",
            task_id=task_id,
            provider=provider,
            continuation_round=round_idx,
            total_rounds=total,
            appended_chars=appended,
        )
    except Exception as exc:
        logger.debug("emit_agent_token_continuation failed: %s", exc)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Auto-save trigger
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@dataclass
class AutoSaveTracker:
    """Simple counter that decides when to flush the scratchpad.

    The contract is:
      * Every ``interval`` ReAct turns → trigger ``turn_interval``.
      * Every tool-call completion → trigger ``tool_done``.
      * Every sub-task switch → trigger ``subtask_switch``.

    The tracker itself doesn't write — it just tells the caller ``True``
    when a save is due. Keeping the write policy outside the file IO
    makes this testable without touching disk.
    """
    interval: int = 10
    _turn: int = 0
    _last_saved_turn: int = 0
    _last_subtask: str | None = None

    def note_turn(self) -> bool:
        self._turn += 1
        if self._turn - self._last_saved_turn >= self.interval:
            self._last_saved_turn = self._turn
            return True
        return False

    def note_tool_done(self) -> bool:
        # Always true — every tool_done should flush.
        self._last_saved_turn = self._turn
        return True

    def note_subtask(self, subtask: str | None) -> bool:
        if subtask != self._last_subtask:
            self._last_subtask = subtask
            self._last_saved_turn = self._turn
            return True
        return False

    @property
    def turn(self) -> int:
        return self._turn


# Module-level registry so different callers (emit_tool_progress wrapper,
# agent loop tick) can share the tracker per agent.
_trackers: dict[str, AutoSaveTracker] = {}
_trackers_lock = threading.Lock()


def get_tracker(agent_id: str, *, interval: int = 10) -> AutoSaveTracker:
    _validate_agent_id(agent_id)
    with _trackers_lock:
        t = _trackers.get(agent_id)
        if t is None:
            t = AutoSaveTracker(interval=interval)
            _trackers[agent_id] = t
        return t


def reset_for_tests() -> None:
    """Clear in-memory caches. Filesystem state is not touched."""
    with _trackers_lock:
        _trackers.clear()
    with _registry_lock:
        _locks.clear()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  UI summary — one call for the matrix-wall card
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def ui_summary(agent_id: str) -> dict | None:
    """Compact dict consumed by the Scratchpad Progress Indicator.

    Returns ``None`` if the agent has never saved a scratchpad — the
    UI uses that to hide the section entirely. Keys intentionally
    mirror the frontend ``AgentScratchpadSummary`` interface.
    """
    meta = read_meta(agent_id)
    if meta is None:
        return None
    now = time.time()
    updated = float(meta.get("updated_at", 0.0) or 0.0)
    age_seconds = max(0.0, now - updated) if updated else None
    total = int(meta.get("total_turns", 0) or 0)
    turn = int(meta.get("turn", 0) or 0)
    # If total wasn't set, treat the latest turn as the denominator.
    denom = max(total, turn, 1)
    return {
        "agent_id": agent_id,
        "turn": turn,
        "total_turns": denom,
        "sections_count": int(meta.get("sections_count", 0) or 0),
        "size_bytes": int(meta.get("size_bytes", 0) or 0),
        "subtask": meta.get("subtask"),
        "trigger": meta.get("trigger"),
        "updated_at": updated or None,
        "updated_at_iso": meta.get("updated_at_iso"),
        "age_seconds": age_seconds,
        "recoverable": True,
    }


def ui_summary_all() -> list[dict]:
    return [s for s in (ui_summary(a) for a in list_agents()) if s]


__all__: Iterable[str] = (
    "SECTIONS",
    "ScratchpadState",
    "SaveResult",
    "ContinuationOutcome",
    "AutoContinuation",
    "AutoSaveTracker",
    "render_markdown",
    "parse_markdown",
    "save",
    "reload_latest",
    "read_meta",
    "list_agents",
    "archive_on_success",
    "retain_for_debug",
    "list_archive",
    "preview_markdown",
    "is_truncated",
    "ui_summary",
    "ui_summary_all",
    "get_tracker",
    "reset_for_tests",
    "scratchpad_path",
    "meta_path",
    "agent_dir",
    "archive_dir",
)
