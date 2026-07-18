"""U6-0 GAP-5c-loop sub-leaf B: an operator-allowlisted, hash-bound workspace-root-fd resolver (dormant, fail-closed).

Given a guard-constructed workspace_id ("ws:{tenant}:{adapter}:{sha256(resolved_root)}";
authoritative_context_resolver.py:43,47-48), return an OPEN O_DIRECTORY fd for the workspace root IFF an
operator-allowlisted root's resolved path hashes to the id's embedded digest.  The allowlist is a JSON array of ABSOLUTE
root paths in OMNISIGHT_U6_WORKSPACE_FD_ROOTS, read via os.environ (execution_gate precedent; runtime-flippable, no
config.py placeholder); DEFAULT EMPTY => every resolve returns None => the GAP-5d executor writes nothing (fail-closed).
The caller OWNS the returned fd; this module never closes it.  Dormant: no production caller (sub-leaf C wires this into
the driver loop).  Executes nothing itself.

SECURITY: the digest binds a PATH STRING, not a directory inode identity; O_NOFOLLOW guards only the final component.
The returned fd is a STARTING capability only -- the GAP-5d executor's O_NOFOLLOW fd-walk + st_dev + regular-file checks
are the actual write containment.  The operator MUST guarantee the allowlisted root AND its full ancestor path are on a
single local filesystem and not concurrently manipulable by an untrusted actor (see the resolver contract).  Linux-only.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

_ROOTS_ENV = "OMNISIGHT_U6_WORKSPACE_FD_ROOTS"
_HEX = frozenset("0123456789abcdef")


def _load_roots() -> "list[str]":
    """Operator allowlist: a JSON array of ABSOLUTE path strings.  Drops non-str / whitespace-dirty / relative entries;
    empty or malformed OMNISIGHT_U6_WORKSPACE_FD_ROOTS => [] (fail-closed)."""
    raw = os.environ.get(_ROOTS_ENV, "").strip()
    if not raw:
        return []
    try:
        entries = json.loads(raw)
    except (ValueError, TypeError):
        return []
    if not isinstance(entries, list):
        return []
    return [e for e in entries if isinstance(e, str) and e == e.strip() and e.startswith("/")]


def _embedded_digest(workspace_id: str) -> "str | None":
    """The trailing 64-lowercase-hex sha256 of a "ws:{tenant}:{adapter}:{digest}" id, or None if malformed."""
    if not isinstance(workspace_id, str) or not workspace_id.startswith("ws:"):
        return None
    digest = workspace_id.rsplit(":", 1)[-1]
    if len(digest) != 64 or any(c not in _HEX for c in digest):
        return None
    return digest


def _resolve_and_digest(root: str) -> "tuple[str, str] | None":
    """(resolved_path, sha256(resolved_path)) EXACTLY as authoritative_context_resolver did, or None if the entry is
    unresolvable / unencodable (Path.resolve raises RuntimeError on a symlink loop) -- one bad entry never
    aborts the others."""
    try:
        resolved = str(Path(root).resolve())
        return resolved, hashlib.sha256(resolved.encode("utf-8")).hexdigest()
    except (OSError, ValueError, UnicodeError, RuntimeError):
        return None


def resolve_root_fd(workspace_id: str) -> "int | None":
    """Return an open O_DIRECTORY fd for the allowlisted, guard-intent-bound workspace root, or None (fail-closed)."""
    try:
        digest = _embedded_digest(workspace_id)
        if digest is None:
            return None
        for root in _load_roots():
            match = _resolve_and_digest(root)
            if match is None or match[1] != digest:
                continue
            try:
                # O_NOFOLLOW: `resolved` is canonical (no final-component symlink normally); rejects a raced final swap.
                # Ancestor races are the contract-bounded residual (the digest binds a path string, not an inode).
                return os.open(match[0], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            except OSError:
                return None                                   # the matched root vanished / is not a directory
        return None                                           # no allowlisted root matches this id's digest
    except Exception:  # noqa: BLE001 -- a resolver must never raise into the executor; fail closed
        return None
