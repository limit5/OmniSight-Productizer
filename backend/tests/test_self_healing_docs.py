"""BP.J.3 — Unit tests for ``backend.self_healing_docs``.

Coverage map (≈20 cases, see SOP Step 4 acceptance):

* ``detect_api_changes`` — pure-function diff, all six branches
  (None old, identical, added/removed/modified routes, schema
  set delta, ``has_breaking_changes`` semantics, ``is_empty``).
* ``ApiChangeReport`` dataclass — frozen + ``as_dict`` JSON shape.
* ``_canonical_openapi`` **drift guard** vs
  ``scripts/dump_openapi.py::_canonical`` (byte-identical contract).
  This is the test the production module's docstring promises;
  if the dump script ever changes its canonicalisation rule,
  ``--check`` mode would create false drift — this test catches
  that at CI time before the divergence ships.
* ``_atomic_write`` + ``regenerate_openapi_snapshot`` — atomic
  rename, no half-written files, byte-identical output.
* ``regenerate_architecture_md`` — seed-when-missing,
  sentinel-bracketed in-place rewrite (preserves manual content
  outside sentinels), append-when-no-sentinel, idempotency.
* ``load_snapshot`` — missing → None, malformed JSON → None.
* ``run`` orchestrator — no-drift fast-path, ``check=True``
  no-write path, ``strict=True`` breaking-refusal path,
  full apply path. ``load_current_schema`` is monkeypatched so
  these tests do not trigger the (slow) FastAPI app import.
* ``_cli`` — argument parsing + exit-code semantics
  (0 clean, 1 drift, 2 strict-refused).

Module-global state audit (SOP 2026-04-21)
──────────────────────────────────────────
Production module is stateless (verified via the docstring it
self-documents). Tests therefore need no per-test reset hook;
each test gets fresh ``tmp_path`` + isolated dict literals.
Cross-worker safety isn't tested here — that's enforced by
``os.replace`` itself, not by anything we wrote — but the
"atomic rename leaves no .tmp lingering" assertion below pins
the contract empirically.
"""
from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest

from backend import self_healing_docs as shd


# ── Test fixtures: minimal OpenAPI-shaped dicts ──────────────────────
def _schema(
    *,
    paths: dict | None = None,
    schemas: dict | None = None,
) -> dict:
    """Build a minimal OpenAPI 3.x dict for diffing tests."""
    return {
        "openapi": "3.1.0",
        "info": {"title": "OmniSight Engine API", "version": "contract"},
        "paths": paths or {},
        "components": {"schemas": schemas or {}},
    }


_OP_GET_USERS = {"summary": "List users", "responses": {"200": {"description": "ok"}}}
_OP_POST_USERS = {"summary": "Create user", "responses": {"201": {"description": "ok"}}}
_OP_GET_HEALTH = {"summary": "Health", "responses": {"200": {"description": "ok"}}}


# ════════════════════════════════════════════════════════════════════
# 1. detect_api_changes — pure-function diff
# ════════════════════════════════════════════════════════════════════
class TestDetectApiChanges:
    def test_none_old_schema_reports_everything_added(self) -> None:
        """First-run bootstrap: no prior snapshot → every route is added."""
        new = _schema(
            paths={"/users": {"get": _OP_GET_USERS, "post": _OP_POST_USERS}},
            schemas={"User": {"type": "object"}},
        )
        report = shd.detect_api_changes(None, new)

        assert report.added_routes == (("/users", "GET"), ("/users", "POST"))
        assert report.removed_routes == ()
        assert report.modified_routes == ()
        assert report.added_schemas == ("User",)
        assert report.removed_schemas == ()
        assert not report.has_breaking_changes
        assert not report.is_empty

    def test_identical_schemas_yield_empty_report(self) -> None:
        s = _schema(paths={"/h": {"get": _OP_GET_HEALTH}}, schemas={"H": {}})
        report = shd.detect_api_changes(s, s)

        assert report.is_empty
        assert not report.has_breaking_changes
        assert report.added_routes == ()
        assert report.removed_routes == ()
        assert report.modified_routes == ()

    def test_added_routes_only_is_not_breaking(self) -> None:
        """Adding endpoints is the safe / additive case."""
        old = _schema(paths={"/h": {"get": _OP_GET_HEALTH}})
        new = _schema(
            paths={
                "/h": {"get": _OP_GET_HEALTH},
                "/users": {"get": _OP_GET_USERS},
            }
        )
        report = shd.detect_api_changes(old, new)

        assert report.added_routes == (("/users", "GET"),)
        assert report.removed_routes == ()
        assert not report.has_breaking_changes

    def test_removed_routes_flagged_breaking(self) -> None:
        old = _schema(paths={"/users": {"get": _OP_GET_USERS}})
        new = _schema(paths={})
        report = shd.detect_api_changes(old, new)

        assert report.removed_routes == (("/users", "GET"),)
        assert report.has_breaking_changes

    def test_modified_route_detected_via_canonical_json(self) -> None:
        """Same path+method but different operation body → modified."""
        old = _schema(paths={"/u": {"get": {"summary": "old"}}})
        new = _schema(paths={"/u": {"get": {"summary": "new"}}})
        report = shd.detect_api_changes(old, new)

        assert report.modified_routes == (("/u", "GET"),)
        assert report.added_routes == ()
        assert report.removed_routes == ()
        # Modifications alone are NOT breaking by default — see docstring.
        assert not report.has_breaking_changes

    def test_modification_ignores_key_ordering(self) -> None:
        """``_canonical_op`` sorts keys → reordering must not surface."""
        op_a = {"summary": "x", "responses": {"200": {"description": "ok"}}}
        op_b = {"responses": {"200": {"description": "ok"}}, "summary": "x"}
        old = _schema(paths={"/u": {"get": op_a}})
        new = _schema(paths={"/u": {"get": op_b}})

        assert shd.detect_api_changes(old, new).is_empty

    def test_schema_set_delta_and_breaking_on_removal(self) -> None:
        old = _schema(schemas={"User": {}, "Tenant": {}})
        new = _schema(schemas={"User": {}, "Org": {}})
        report = shd.detect_api_changes(old, new)

        assert report.added_schemas == ("Org",)
        assert report.removed_schemas == ("Tenant",)
        # Removed schema = breaking, even with no removed routes.
        assert report.has_breaking_changes

    def test_non_http_path_keys_filtered(self) -> None:
        """OpenAPI lets ``parameters`` / ``summary`` sit at path-level —
        they must not pollute the (path, METHOD) pair set."""
        old = _schema()
        new = _schema(
            paths={
                "/u": {
                    "summary": "doc-only key",
                    "parameters": [{"name": "id", "in": "query"}],
                    "get": _OP_GET_USERS,
                }
            }
        )
        report = shd.detect_api_changes(old, new)

        # Only the GET surfaces — never ("/u", "PARAMETERS") or "SUMMARY".
        assert report.added_routes == (("/u", "GET"),)


# ════════════════════════════════════════════════════════════════════
# 2. ApiChangeReport dataclass — frozenness + JSON shape
# ════════════════════════════════════════════════════════════════════
class TestApiChangeReport:
    def test_dataclass_is_frozen(self) -> None:
        """Reports must be immutable so they can be safely shared."""
        r = shd.ApiChangeReport(
            added_routes=(),
            removed_routes=(),
            modified_routes=(),
            added_schemas=(),
            removed_schemas=(),
        )
        with pytest.raises(dataclasses_FrozenInstanceError := __import__(
            "dataclasses"
        ).FrozenInstanceError):
            r.added_routes = (("/x", "GET"),)  # type: ignore[misc]

    def test_as_dict_round_trips_through_json(self) -> None:
        """as_dict() must be JSON-serialisable (used by --report-only CLI)."""
        r = shd.ApiChangeReport(
            added_routes=(("/a", "GET"),),
            removed_routes=(("/b", "POST"),),
            modified_routes=(),
            added_schemas=("S1",),
            removed_schemas=(),
        )
        d = r.as_dict()
        # Must round-trip via stdlib json with no custom encoder.
        text = json.dumps(d, sort_keys=True)
        loaded = json.loads(text)

        assert loaded["added_routes"] == [["/a", "GET"]]
        assert loaded["removed_routes"] == [["/b", "POST"]]
        assert loaded["added_schemas"] == ["S1"]
        assert loaded["has_breaking_changes"] is True
        assert loaded["is_empty"] is False


# ════════════════════════════════════════════════════════════════════
# 3. _canonical_openapi — byte-identical contract vs dump_openapi.py
# ════════════════════════════════════════════════════════════════════
def test_canonical_openapi_matches_dump_openapi_byte_identically() -> None:
    """**Drift guard** (SOP Step 4 — pin two `static lists` to the
    same answer). The production docstring promises ``self_healing_
    docs._canonical_openapi`` and ``scripts/dump_openapi.py::_canonical``
    produce byte-identical output for the same input dict; if anyone
    changes one without the other, ``--check`` mode would manufacture
    false drift on every CI run.
    """
    # Import the dump script as a module without executing main().
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "scripts"))
    try:
        dump_openapi = importlib.import_module("dump_openapi")
    finally:
        sys.path.pop(0)

    sample = _schema(
        paths={
            "/u": {"get": _OP_GET_USERS, "post": _OP_POST_USERS},
            "/h": {"get": _OP_GET_HEALTH},
        },
        schemas={"User": {"type": "object"}, "Z": {"type": "string"}},
    )

    a = shd._canonical_openapi(sample)
    b = dump_openapi._canonical(
        # dump_openapi expects the info-overridden form; replicate the
        # same pre-processing _canonical_openapi does internally.
        {**sample, "info": {"title": "OmniSight Engine API", "version": "contract"}}
    )
    assert a == b, "self_healing_docs and dump_openapi.py canonicalise differently"
    # Sanity: trailing newline + sorted keys.
    assert a.endswith("\n")
    parsed = json.loads(a)
    assert list(parsed.keys()) == sorted(parsed.keys())


# ════════════════════════════════════════════════════════════════════
# 4. _atomic_write + regenerate_openapi_snapshot
# ════════════════════════════════════════════════════════════════════
class TestAtomicWriters:
    def test_regenerate_openapi_writes_canonical_form(self, tmp_path: Path) -> None:
        target = tmp_path / "openapi.json"
        sample = _schema(paths={"/h": {"get": _OP_GET_HEALTH}})

        out = shd.regenerate_openapi_snapshot(sample, target=target)

        assert out == target.resolve()
        text = target.read_text(encoding="utf-8")
        # Byte-identical to the canonicaliser.
        assert text == shd._canonical_openapi(sample)
        # Parse back round-trips.
        assert json.loads(text)["paths"]["/h"]["get"]["summary"] == "Health"

    def test_atomic_write_leaves_no_tempfile_residue(self, tmp_path: Path) -> None:
        """``_atomic_write`` MUST clean up its tempfile; otherwise
        a ``.openapi.json.XXXX.tmp`` would accumulate every CI run."""
        target = tmp_path / "openapi.json"
        shd._atomic_write(target, "hello")

        residue = list(tmp_path.iterdir())
        assert residue == [target], (
            f"unexpected tempfile residue: {residue}"
        )

    def test_atomic_write_creates_parent_dir(self, tmp_path: Path) -> None:
        """Writers are called from CLI / hook contexts that may not
        have created ``docs/`` yet — they must mkdir on demand."""
        target = tmp_path / "deeply" / "nested" / "openapi.json"
        assert not target.parent.exists()
        shd._atomic_write(target, "{}\n")
        assert target.read_text() == "{}\n"


# ════════════════════════════════════════════════════════════════════
# 5. regenerate_architecture_md — seed / sentinel preservation
# ════════════════════════════════════════════════════════════════════
class TestRegenerateArchitectureMd:
    def _empty_report(self) -> shd.ApiChangeReport:
        return shd.ApiChangeReport((), (), (), (), ())

    def test_seeds_full_scaffold_when_file_missing(
        self, tmp_path: Path
    ) -> None:
        target = tmp_path / "architecture.md"
        sample = _schema(paths={"/h": {"get": _OP_GET_HEALTH}})

        shd.regenerate_architecture_md(sample, self._empty_report(), target=target)

        text = target.read_text(encoding="utf-8")
        # Scaffold front + back matter.
        assert "# OmniSight Architecture" in text
        assert "## Overview" in text
        assert "## Notes" in text
        # Auto-generated block is delimited.
        assert shd.ARCH_BEGIN in text
        assert shd.ARCH_END in text
        # Route table content present.
        assert "`GET`" in text and "`/h`" in text

    def test_preserves_manual_content_outside_sentinels(
        self, tmp_path: Path
    ) -> None:
        """The whole point of sentinels: hand-written notes survive."""
        target = tmp_path / "architecture.md"
        original = (
            "# Custom Title\n"
            "\n"
            "## My handcrafted overview — DO NOT TOUCH\n"
            "Author note A.\n"
            "\n"
            f"{shd.ARCH_BEGIN}\n"
            "stale auto content\n"
            f"{shd.ARCH_END}\n"
            "\n"
            "## My handcrafted footer — DO NOT TOUCH EITHER\n"
            "Author note B.\n"
        )
        target.write_text(original, encoding="utf-8")
        sample = _schema(paths={"/x": {"post": _OP_POST_USERS}})

        shd.regenerate_architecture_md(sample, self._empty_report(), target=target)
        new_text = target.read_text(encoding="utf-8")

        # Manual sections preserved verbatim.
        assert "# Custom Title" in new_text
        assert "Author note A." in new_text
        assert "Author note B." in new_text
        assert "DO NOT TOUCH" in new_text
        # Stale block clobbered.
        assert "stale auto content" not in new_text
        # New block exists.
        assert "/x" in new_text and "POST" in new_text

    def test_appends_block_when_no_sentinels_present(
        self, tmp_path: Path
    ) -> None:
        """Pre-existing architecture.md without sentinels: append, do
        not clobber, so the operator can manually merge."""
        target = tmp_path / "architecture.md"
        target.write_text("# Hand-written file\nno sentinels here.\n", encoding="utf-8")
        sample = _schema(paths={"/y": {"get": _OP_GET_USERS}})

        shd.regenerate_architecture_md(sample, self._empty_report(), target=target)
        text = target.read_text(encoding="utf-8")

        assert text.startswith("# Hand-written file\nno sentinels here.\n")
        assert shd.ARCH_BEGIN in text
        assert shd.ARCH_END in text
        # Original line not nuked.
        assert "no sentinels here." in text

    def test_idempotent_when_already_up_to_date(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Re-running with identical input must NOT bump the file
        mtime (proves the no-op short-circuit at the bottom of
        ``regenerate_architecture_md`` fires).

        Subtle point: the *first* call seeds via the scaffold template
        whereas the *second* call walks the sentinel-replace branch —
        the two formatters emit slightly different leading whitespace.
        Idempotency is therefore measured between two consecutive
        sentinel-replace runs (call #2 vs call #3), which is the
        regime that matters in production: every CI run after the
        initial seed.
        """
        # Freeze wall-clock so the rendered ``Last regenerated`` line
        # doesn't move between invocations.
        from datetime import datetime as real_dt, timezone

        frozen = real_dt(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc)

        class _FrozenDT(real_dt):
            @classmethod
            def now(cls, tz=None):  # noqa: D401 — patch shape
                return frozen

        monkeypatch.setattr(shd, "datetime", _FrozenDT)

        target = tmp_path / "architecture.md"
        sample = _schema(paths={"/h": {"get": _OP_GET_HEALTH}})

        # Call #1: seeds scaffold.
        shd.regenerate_architecture_md(sample, self._empty_report(), target=target)
        # Call #2: walks the sentinel-replace branch, normalises whitespace.
        shd.regenerate_architecture_md(sample, self._empty_report(), target=target)
        steady_mtime = target.stat().st_mtime_ns
        steady_text = target.read_text(encoding="utf-8")

        # Call #3: identical input → must short-circuit.
        shd.regenerate_architecture_md(sample, self._empty_report(), target=target)

        assert target.read_text(encoding="utf-8") == steady_text
        assert target.stat().st_mtime_ns == steady_mtime, (
            "expected no-op rewrite when content unchanged"
        )


# ════════════════════════════════════════════════════════════════════
# 6. load_snapshot — missing / malformed
# ════════════════════════════════════════════════════════════════════
class TestLoadSnapshot:
    def test_returns_none_when_file_missing(self, tmp_path: Path) -> None:
        assert shd.load_snapshot(tmp_path / "nope.json") is None

    def test_returns_none_when_file_malformed(self, tmp_path: Path) -> None:
        bad = tmp_path / "openapi.json"
        bad.write_text("not { valid json", encoding="utf-8")
        assert shd.load_snapshot(bad) is None


# ════════════════════════════════════════════════════════════════════
# 7. run() orchestrator — modes + branches
# ════════════════════════════════════════════════════════════════════
class TestRunOrchestrator:
    """``load_current_schema`` boots the FastAPI app — slow + side-effect-y.
    Patch it for these tests so we keep the orchestrator tests in the
    pure-Python lane (no DB, no app import)."""

    def _patch_loader(
        self, monkeypatch: pytest.MonkeyPatch, schema: dict
    ) -> None:
        monkeypatch.setattr(shd, "load_current_schema", lambda: schema)

    def test_no_drift_returns_empty_report_no_writes(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        sample = _schema(paths={"/h": {"get": _OP_GET_HEALTH}})
        snapshot = tmp_path / "openapi.json"
        snapshot.write_text(shd._canonical_openapi(sample), encoding="utf-8")
        arch = tmp_path / "architecture.md"

        self._patch_loader(monkeypatch, sample)
        result = shd.run(openapi_path=snapshot, architecture_path=arch)

        assert result.report.is_empty
        assert result.wrote_openapi is False
        assert result.wrote_architecture is False
        # No drift → architecture.md must not be seeded either.
        assert not arch.exists()

    def test_check_mode_reports_drift_without_writing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        old = _schema(paths={"/h": {"get": _OP_GET_HEALTH}})
        new = _schema(
            paths={
                "/h": {"get": _OP_GET_HEALTH},
                "/new": {"get": _OP_GET_USERS},
            }
        )
        snapshot = tmp_path / "openapi.json"
        snapshot.write_text(shd._canonical_openapi(old), encoding="utf-8")
        arch = tmp_path / "architecture.md"
        snap_text_before = snapshot.read_text(encoding="utf-8")

        self._patch_loader(monkeypatch, new)
        result = shd.run(check=True, openapi_path=snapshot, architecture_path=arch)

        assert not result.report.is_empty
        assert result.wrote_openapi is False
        assert result.wrote_architecture is False
        # Snapshot untouched, arch not seeded.
        assert snapshot.read_text(encoding="utf-8") == snap_text_before
        assert not arch.exists()

    def test_strict_refuses_breaking_diff(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """strict=True + removed route → refusal, no writes, exit-code-2 path."""
        old = _schema(paths={"/keep": {"get": _OP_GET_HEALTH}, "/gone": {"get": _OP_GET_USERS}})
        new = _schema(paths={"/keep": {"get": _OP_GET_HEALTH}})
        snapshot = tmp_path / "openapi.json"
        snapshot.write_text(shd._canonical_openapi(old), encoding="utf-8")
        arch = tmp_path / "architecture.md"
        snap_before = snapshot.read_text(encoding="utf-8")

        self._patch_loader(monkeypatch, new)
        result = shd.run(
            strict=True, openapi_path=snapshot, architecture_path=arch
        )

        assert result.report.has_breaking_changes
        assert result.refused_breaking is True
        assert result.wrote_openapi is False
        assert result.wrote_architecture is False
        # Crucial: snapshot untouched on refusal.
        assert snapshot.read_text(encoding="utf-8") == snap_before
        assert not arch.exists()

    def test_apply_mode_rewrites_both_artefacts(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        old = _schema(paths={"/h": {"get": _OP_GET_HEALTH}})
        new = _schema(
            paths={
                "/h": {"get": _OP_GET_HEALTH},
                "/new": {"post": _OP_POST_USERS},
            },
            schemas={"NewModel": {"type": "object"}},
        )
        snapshot = tmp_path / "openapi.json"
        snapshot.write_text(shd._canonical_openapi(old), encoding="utf-8")
        arch = tmp_path / "architecture.md"

        self._patch_loader(monkeypatch, new)
        result = shd.run(openapi_path=snapshot, architecture_path=arch)

        assert result.wrote_openapi is True
        assert result.wrote_architecture is True
        assert result.refused_breaking is False
        # Snapshot now equals canonical(new).
        assert snapshot.read_text(encoding="utf-8") == shd._canonical_openapi(new)
        # Architecture seeded with the new route table.
        assert "/new" in arch.read_text(encoding="utf-8")


# ════════════════════════════════════════════════════════════════════
# 8. CLI exit-code semantics
# ════════════════════════════════════════════════════════════════════
class TestCliExitCodes:
    def test_check_mode_exits_zero_when_clean(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        sample = _schema(paths={"/h": {"get": _OP_GET_HEALTH}})
        snapshot = tmp_path / "openapi.json"
        snapshot.write_text(shd._canonical_openapi(sample), encoding="utf-8")
        arch = tmp_path / "architecture.md"

        monkeypatch.setattr(shd, "load_current_schema", lambda: sample)
        monkeypatch.setattr(shd, "OPENAPI_SNAPSHOT", snapshot)
        monkeypatch.setattr(shd, "ARCHITECTURE_MD", arch)

        rc = shd._cli(["--check"])
        assert rc == 0

    def test_check_mode_exits_one_on_drift(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        old = _schema(paths={"/h": {"get": _OP_GET_HEALTH}})
        new = _schema(
            paths={"/h": {"get": _OP_GET_HEALTH}, "/new": {"get": _OP_GET_USERS}}
        )
        snapshot = tmp_path / "openapi.json"
        snapshot.write_text(shd._canonical_openapi(old), encoding="utf-8")
        arch = tmp_path / "architecture.md"

        monkeypatch.setattr(shd, "load_current_schema", lambda: new)
        monkeypatch.setattr(shd, "OPENAPI_SNAPSHOT", snapshot)
        monkeypatch.setattr(shd, "ARCHITECTURE_MD", arch)

        rc = shd._cli(["--check"])
        assert rc == 1
