"""B3 — Theory-of-Mind scratchpad (OP-830, master plan §2.5).

Append-only structured trace of the model's hypotheses + verifications.
The scratchpad is the **only continuous trace** across context resets:
when the loop detector forces a reset (B3 AC #4) the conversation history
is wiped except for system + first user message, but the ToM scratchpad
persists. This gives the post-reset model a brief summary of what was
already tried so it can avoid repeating itself.

Schema (per AC #6):

    {"hypothesis": str, "verifying": str, "outcome": "pending|success|fail"}

Malformed entries are **rejected** (``append`` returns ``False``).
The B3 detector treats rejected scratchpad turns as "not progress":
they do not count toward Levenshtein progressive-narrowing exemption,
and they do not break a pending 3x-loop streak.

Persistence (per AC #7): the scratchpad writes each accepted entry to
``progress.txt`` (B9) line by line. ``ToMScratchpad.load(path)`` rebuilds
the in-memory list at runner restart so the trace survives crashes.
B9's progress.txt is a future Phase-3 artifact; until it lands, callers
pass any per-ticket path (e.g. ``<worktree>/.runner/progress-<KEY>.txt``)
and the scratchpad maintains the same JSONL format that B9 will adopt.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Allowed values for the ``outcome`` field. Anything else => malformed.
VALID_OUTCOMES: frozenset[str] = frozenset({"pending", "success", "fail"})

# Tag used in the JSONL stream so progress.txt (B9) can multiplex other
# B-component traces in the same file without collision.
ENTRY_TYPE = "tom_scratchpad"


@dataclass(frozen=True)
class ScratchpadEntry:
    """One ToM turn. Fields validated at append-time."""

    hypothesis: str
    verifying: str
    outcome: str  # one of VALID_OUTCOMES

    def to_jsonl_record(self) -> str:
        return json.dumps(
            {
                "type": ENTRY_TYPE,
                "hypothesis": self.hypothesis,
                "verifying": self.verifying,
                "outcome": self.outcome,
            },
            ensure_ascii=False,
        )


@dataclass
class ToMScratchpad:
    """Append-only scratchpad. Survives context reset; survives crash."""

    entries: list[ScratchpadEntry] = field(default_factory=list)
    progress_path: Path | None = None

    def append(self, raw: Any) -> bool:
        """Validate + append a structured scratchpad entry.

        Returns ``True`` on accept, ``False`` on malformed (per AC #6 — a
        malformed scratchpad turn is rejected and does not count as
        progress).
        """
        entry = self._validate(raw)
        if entry is None:
            return False
        self.entries.append(entry)
        if self.progress_path is not None:
            self._persist(entry)
        return True

    @staticmethod
    def _validate(raw: Any) -> ScratchpadEntry | None:
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except (ValueError, json.JSONDecodeError):
                return None
        if not isinstance(raw, dict):
            return None
        h = raw.get("hypothesis")
        v = raw.get("verifying")
        o = raw.get("outcome")
        if not isinstance(h, str) or not h.strip():
            return None
        if not isinstance(v, str) or not v.strip():
            return None
        if not isinstance(o, str) or o not in VALID_OUTCOMES:
            return None
        return ScratchpadEntry(hypothesis=h, verifying=v, outcome=o)

    def _persist(self, entry: ScratchpadEntry) -> None:
        path = self.progress_path
        assert path is not None  # checked by caller
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(entry.to_jsonl_record() + "\n")

    @classmethod
    def load(cls, progress_path: Path | str) -> ToMScratchpad:
        """Restore from a progress.txt JSONL file. Missing file => empty."""
        path = Path(progress_path)
        sp = cls(progress_path=path)
        if not path.is_file():
            return sp
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except (ValueError, json.JSONDecodeError):
                continue
            if not isinstance(row, dict) or row.get("type") != ENTRY_TYPE:
                continue
            entry = cls._validate(
                {k: v for k, v in row.items() if k != "type"}
            )
            if entry is not None:
                sp.entries.append(entry)
        return sp

    def render_summary(self, max_entries: int = 6) -> str:
        """Compact text rendering for context-reset injection.

        The post-reset model receives system prompt + first user message +
        a ``PRIOR ATTEMPT`` paragraph (per AC #4). The scratchpad summary
        is appended to that paragraph so the model sees what hypotheses
        it has already explored.
        """
        if not self.entries:
            return ""
        recent = self.entries[-max_entries:]
        offset = len(self.entries) - len(recent)
        lines = ["Prior ToM scratchpad (continuous trace, persists across reset):"]
        for i, e in enumerate(recent, start=offset + 1):
            lines.append(
                f"  {i}. [{e.outcome}] hypothesis: {e.hypothesis[:120]} | "
                f"verifying: {e.verifying[:120]}"
            )
        return "\n".join(lines)

    def __len__(self) -> int:
        return len(self.entries)
