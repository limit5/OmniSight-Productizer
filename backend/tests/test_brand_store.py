"""W12.5 — :mod:`backend.brand_store` contract tests.

Pins the on-disk persistence contract for ``.omnisight/brand.json``:

* Public-surface invariants — alphabetised ``__all__``, canonical
  path / dir / filename literals, error subclass hierarchy.
* :func:`resolve_brand_store_path` — pure path resolver, accepts
  ``str`` / ``Path`` / ``os.PathLike``, rejects ``None`` and other
  non-path types.
* :func:`write_brand_spec` — atomic write, dir creation, indent
  pass-through, byte-for-byte canonical JSON, fail-loud on bad
  input, OS errors wrapped via ``__cause__``.
* :func:`read_brand_spec` — strict load, fail-loud on missing /
  malformed / wrong shape / unsupported schema version.
* :func:`read_brand_spec_if_exists` — soft-not-found, returns ``None``
  for absent file but still raises on corrupted content.
* :func:`delete_brand_spec` — removes the file, returns the right
  truthy/falsy signal, leaves siblings (e.g. ``clone-manifest.json``)
  untouched.
* End-to-end roundtrip — every value the W12.4 resolver might
  surface (populated spec / fail-soft empty spec / provenance-only
  spec) survives write → read.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend import brand_store as bs
from backend.brand_spec import (
    BRAND_SPEC_SCHEMA_VERSION,
    BrandSpec,
    BrandSpecError,
    HeadingScale,
    spec_to_json,
)
from backend.brand_store import (
    BRAND_STORE_DIR,
    BRAND_STORE_FILENAME,
    BRAND_STORE_RELATIVE_PATH,
    BrandStoreError,
    BrandStoreReadError,
    BrandStoreWriteError,
    delete_brand_spec,
    read_brand_spec,
    read_brand_spec_if_exists,
    resolve_brand_store_path,
    write_brand_spec,
)


# ── Shared fixtures ─────────────────────────────────────────────────


def _populated_spec() -> BrandSpec:
    return BrandSpec(
        palette=("#0066ff", "#FFFFFF", "#abc"),
        fonts=("Inter", "sans-serif"),
        heading=HeadingScale(h1=48, h2=32, h3=24),
        spacing=(4, 8, 16),
        radius=(0, 4, 8),
        source_url="https://example.com",
        extracted_at="2026-04-29T00:00:00+00:00",
    )


def _empty_provenance_spec() -> BrandSpec:
    """Mirror of the fail-soft envelope from W12.4 — empty palette /
    fonts / heading / spacing / radius but provenance fields set."""
    return BrandSpec(
        source_url="https://flaky.example.com",
        extracted_at="2026-04-29T00:00:00+00:00",
    )


# ── Module-level invariants ─────────────────────────────────────────


class TestModuleInvariants:
    def test_exports_alphabetised(self):
        assert bs.__all__ == sorted(bs.__all__)

    def test_public_surface_exported(self):
        for name in (
            "BRAND_STORE_DIR",
            "BRAND_STORE_FILENAME",
            "BRAND_STORE_RELATIVE_PATH",
            "BrandStoreError",
            "BrandStoreReadError",
            "BrandStoreWriteError",
            "delete_brand_spec",
            "read_brand_spec",
            "read_brand_spec_if_exists",
            "resolve_brand_store_path",
            "write_brand_spec",
        ):
            assert name in bs.__all__, name

    def test_dir_pinned(self):
        # Aligned with backend.web.clone_manifest.MANIFEST_DIR so a
        # single ``.gitignore`` rule covers both records.
        assert BRAND_STORE_DIR == ".omnisight"

    def test_filename_pinned(self):
        assert BRAND_STORE_FILENAME == "brand.json"

    def test_relative_path_pinned(self):
        assert BRAND_STORE_RELATIVE_PATH == ".omnisight/brand.json"

    def test_relative_path_consistent(self):
        # Compose-time consistency between the three constants.
        assert (
            BRAND_STORE_RELATIVE_PATH
            == f"{BRAND_STORE_DIR}/{BRAND_STORE_FILENAME}"
        )

    def test_dir_aligned_with_clone_manifest(self):
        # Drift guard — both W11.7 and W12.5 store under the same
        # ``.omnisight/`` umbrella.  If one ever moves, the other must
        # follow (and this test will scream).
        from backend.web.clone_manifest import MANIFEST_DIR

        assert BRAND_STORE_DIR == MANIFEST_DIR

    def test_brand_store_error_subclass_of_brand_spec_error(self):
        # Existing ``except BrandSpecError`` chains catch us cleanly.
        assert issubclass(BrandStoreError, BrandSpecError)
        # Both are ValueError too.
        assert issubclass(BrandStoreError, ValueError)

    def test_read_error_subclass_of_brand_store_error(self):
        assert issubclass(BrandStoreReadError, BrandStoreError)

    def test_write_error_subclass_of_brand_store_error(self):
        assert issubclass(BrandStoreWriteError, BrandStoreError)

    def test_read_and_write_errors_distinct(self):
        # Write and read errors should be distinguishable at except-time.
        assert not issubclass(BrandStoreReadError, BrandStoreWriteError)
        assert not issubclass(BrandStoreWriteError, BrandStoreReadError)


# ── resolve_brand_store_path ────────────────────────────────────────


class TestResolveBrandStorePath:
    def test_str_root(self, tmp_path: Path):
        target = resolve_brand_store_path(str(tmp_path))
        assert target == tmp_path / ".omnisight" / "brand.json"
        assert target.is_absolute()

    def test_path_root(self, tmp_path: Path):
        target = resolve_brand_store_path(tmp_path)
        assert target == tmp_path / ".omnisight" / "brand.json"

    def test_pathlike_root(self, tmp_path: Path):
        # ``os.PathLike`` — anything with ``__fspath__``.
        class Wrapper:
            def __init__(self, p: Path) -> None:
                self._p = p

            def __fspath__(self) -> str:
                return str(self._p)

        target = resolve_brand_store_path(Wrapper(tmp_path))
        assert target == tmp_path / ".omnisight" / "brand.json"

    def test_relative_root_resolved(self, tmp_path: Path, monkeypatch):
        # A relative root resolves against CWD.
        monkeypatch.chdir(tmp_path)
        target = resolve_brand_store_path("subdir")
        assert target.is_absolute()
        assert target == tmp_path / "subdir" / ".omnisight" / "brand.json"

    def test_does_not_create_directory(self, tmp_path: Path):
        # Pure resolver — must not touch the filesystem.
        target = resolve_brand_store_path(tmp_path)
        assert not target.parent.exists()
        assert not target.exists()

    def test_rejects_none(self):
        with pytest.raises(BrandStoreWriteError):
            resolve_brand_store_path(None)  # type: ignore[arg-type]

    def test_rejects_int(self):
        with pytest.raises(BrandStoreWriteError):
            resolve_brand_store_path(12345)  # type: ignore[arg-type]

    def test_rejects_dict(self):
        with pytest.raises(BrandStoreWriteError):
            resolve_brand_store_path({"root": "/tmp"})  # type: ignore[arg-type]


# ── write_brand_spec ────────────────────────────────────────────────


class TestWriteBrandSpec:
    def test_writes_file(self, tmp_path: Path):
        spec = _populated_spec()
        out = write_brand_spec(spec, project_root=tmp_path)
        assert out == tmp_path / ".omnisight" / "brand.json"
        assert out.exists()
        assert out.is_file()

    def test_creates_omnisight_directory(self, tmp_path: Path):
        spec = _populated_spec()
        assert not (tmp_path / ".omnisight").exists()
        write_brand_spec(spec, project_root=tmp_path)
        assert (tmp_path / ".omnisight").is_dir()

    def test_creates_nested_project_root(self, tmp_path: Path):
        # ``parents=True`` must apply — a not-yet-existing project_root
        # parent should still produce the file.
        nested = tmp_path / "level1" / "level2"
        nested.mkdir(parents=True)
        spec = _populated_spec()
        out = write_brand_spec(spec, project_root=nested)
        assert out.exists()
        assert out.parent == nested / ".omnisight"

    def test_returns_absolute_path(self, tmp_path: Path):
        out = write_brand_spec(_populated_spec(), project_root=tmp_path)
        assert out.is_absolute()

    def test_roundtrip_populated(self, tmp_path: Path):
        spec = _populated_spec()
        write_brand_spec(spec, project_root=tmp_path)
        loaded = read_brand_spec(tmp_path)
        assert loaded == spec

    def test_roundtrip_empty_provenance(self, tmp_path: Path):
        # Mirrors the W12.4 fail-soft envelope — empty dimensions
        # + provenance fields set.  Must persist cleanly so the
        # audit record reads "we tried, got nothing".
        spec = _empty_provenance_spec()
        write_brand_spec(spec, project_root=tmp_path)
        loaded = read_brand_spec(tmp_path)
        assert loaded == spec
        assert loaded.is_empty
        assert loaded.source_url == "https://flaky.example.com"

    def test_roundtrip_default_spec(self, tmp_path: Path):
        # Even the all-defaults BrandSpec roundtrips.
        spec = BrandSpec()
        write_brand_spec(spec, project_root=tmp_path)
        loaded = read_brand_spec(tmp_path)
        assert loaded == spec

    def test_canonical_json_payload(self, tmp_path: Path):
        # The on-disk text must equal ``spec_to_json`` + trailing
        # newline — diff-friendly, byte-deterministic.
        spec = _populated_spec()
        out = write_brand_spec(spec, project_root=tmp_path)
        text = out.read_text(encoding="utf-8")
        assert text == spec_to_json(spec, indent=2) + "\n"

    def test_indent_pass_through(self, tmp_path: Path):
        spec = _populated_spec()
        out = write_brand_spec(spec, project_root=tmp_path, indent=None)
        text = out.read_text(encoding="utf-8")
        # ``indent=None`` ⇒ compact JSON (no leading whitespace per
        # property).  ``indent=2`` would have ``\n  "`` after the
        # opening brace.
        assert "\n  \"" not in text

    def test_overwrites_existing_file(self, tmp_path: Path):
        spec_a = _populated_spec()
        spec_b = spec_a.replace_with(palette=("#000000",))
        write_brand_spec(spec_a, project_root=tmp_path)
        write_brand_spec(spec_b, project_root=tmp_path)
        loaded = read_brand_spec(tmp_path)
        assert loaded == spec_b

    def test_str_project_root(self, tmp_path: Path):
        spec = _populated_spec()
        out = write_brand_spec(spec, project_root=str(tmp_path))
        assert out.exists()

    def test_rejects_non_brand_spec(self, tmp_path: Path):
        with pytest.raises(BrandStoreWriteError):
            write_brand_spec(  # type: ignore[arg-type]
                {"palette": ["#000000"]}, project_root=tmp_path
            )

    def test_rejects_none_spec(self, tmp_path: Path):
        with pytest.raises(BrandStoreWriteError):
            write_brand_spec(None, project_root=tmp_path)  # type: ignore[arg-type]

    def test_rejects_string_spec(self, tmp_path: Path):
        with pytest.raises(BrandStoreWriteError):
            write_brand_spec("not-a-spec", project_root=tmp_path)  # type: ignore[arg-type]

    def test_rejects_invalid_project_root(self):
        with pytest.raises(BrandStoreWriteError):
            write_brand_spec(_populated_spec(), project_root=None)  # type: ignore[arg-type]

    def test_no_tempfile_left_behind(self, tmp_path: Path):
        # Atomic-write contract — after a successful write, the
        # ``.omnisight/`` directory contains exactly the target file
        # and no ``.brand.json.*.tmp`` artefacts.
        write_brand_spec(_populated_spec(), project_root=tmp_path)
        omnisight_dir = tmp_path / ".omnisight"
        children = sorted(p.name for p in omnisight_dir.iterdir())
        assert children == ["brand.json"], children

    def test_atomic_replace_does_not_corrupt_existing(self, tmp_path: Path):
        # If the new spec passes type validation, the existing file
        # must be replaced atomically — never leave a half-written
        # file on disk.  We cannot easily fault-inject the rename
        # itself, but we can at least assert the post-condition: any
        # successful call leaves the target with the new content.
        spec_a = _populated_spec()
        spec_b = spec_a.replace_with(spacing=(2, 4, 6, 8, 10))
        write_brand_spec(spec_a, project_root=tmp_path)
        first = read_brand_spec(tmp_path)
        write_brand_spec(spec_b, project_root=tmp_path)
        second = read_brand_spec(tmp_path)
        assert first == spec_a
        assert second == spec_b
        # No trailing tempfile.
        children = sorted(
            p.name for p in (tmp_path / ".omnisight").iterdir()
        )
        assert children == ["brand.json"]


# ── read_brand_spec ─────────────────────────────────────────────────


class TestReadBrandSpec:
    def test_reads_written_spec(self, tmp_path: Path):
        spec = _populated_spec()
        write_brand_spec(spec, project_root=tmp_path)
        assert read_brand_spec(tmp_path) == spec

    def test_raises_on_missing_file(self, tmp_path: Path):
        with pytest.raises(BrandStoreReadError):
            read_brand_spec(tmp_path)

    def test_raises_on_missing_dir(self, tmp_path: Path):
        nonexistent = tmp_path / "no-such-project"
        with pytest.raises(BrandStoreReadError):
            read_brand_spec(nonexistent)

    def test_raises_on_invalid_json(self, tmp_path: Path):
        target = resolve_brand_store_path(tmp_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("this is not json", encoding="utf-8")
        with pytest.raises(BrandStoreReadError):
            read_brand_spec(tmp_path)

    def test_raises_on_empty_file(self, tmp_path: Path):
        target = resolve_brand_store_path(tmp_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("", encoding="utf-8")
        with pytest.raises(BrandStoreReadError):
            read_brand_spec(tmp_path)

    def test_raises_on_json_array_root(self, tmp_path: Path):
        # JSON array at top level is valid JSON but wrong shape.
        target = resolve_brand_store_path(tmp_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
        with pytest.raises(BrandStoreReadError):
            read_brand_spec(tmp_path)

    def test_raises_on_unsupported_schema_version(self, tmp_path: Path):
        target = resolve_brand_store_path(tmp_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps({"schema_version": "99.0.0", "palette": []}),
            encoding="utf-8",
        )
        with pytest.raises(BrandStoreReadError):
            read_brand_spec(tmp_path)

    def test_raises_on_non_string_schema_version(self, tmp_path: Path):
        target = resolve_brand_store_path(tmp_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps({"schema_version": 1, "palette": []}),
            encoding="utf-8",
        )
        with pytest.raises(BrandStoreReadError):
            read_brand_spec(tmp_path)

    def test_raises_on_bad_payload_shape(self, tmp_path: Path):
        # ``palette`` should be a list — give it a dict instead.
        target = resolve_brand_store_path(tmp_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(
                {
                    "schema_version": BRAND_SPEC_SCHEMA_VERSION,
                    "palette": {"red": "#ff0000"},
                }
            ),
            encoding="utf-8",
        )
        with pytest.raises(BrandStoreReadError):
            read_brand_spec(tmp_path)

    def test_raises_on_invalid_hex_value(self, tmp_path: Path):
        target = resolve_brand_store_path(tmp_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(
                {
                    "schema_version": BRAND_SPEC_SCHEMA_VERSION,
                    "palette": ["not-a-hex"],
                }
            ),
            encoding="utf-8",
        )
        with pytest.raises(BrandStoreReadError):
            read_brand_spec(tmp_path)

    def test_accepts_missing_schema_version(self, tmp_path: Path):
        # Older / hand-authored payloads without the version key
        # still load — :meth:`BrandSpec.from_dict` defaults the
        # version, and the version gate only fires when the key is
        # present and disagrees.
        target = resolve_brand_store_path(tmp_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps({"palette": ["#ff0000"], "fonts": ["inter"]}),
            encoding="utf-8",
        )
        loaded = read_brand_spec(tmp_path)
        assert loaded.palette == ("#ff0000",)
        assert loaded.fonts == ("inter",)


# ── read_brand_spec_if_exists ───────────────────────────────────────


class TestReadBrandSpecIfExists:
    def test_returns_none_on_missing_file(self, tmp_path: Path):
        assert read_brand_spec_if_exists(tmp_path) is None

    def test_returns_none_when_omnisight_dir_missing(self, tmp_path: Path):
        # The whole ``.omnisight/`` umbrella is absent — should still
        # be a clean ``None``.
        assert not (tmp_path / ".omnisight").exists()
        assert read_brand_spec_if_exists(tmp_path) is None

    def test_returns_spec_when_present(self, tmp_path: Path):
        spec = _populated_spec()
        write_brand_spec(spec, project_root=tmp_path)
        loaded = read_brand_spec_if_exists(tmp_path)
        assert loaded == spec

    def test_raises_on_corrupted_when_present(self, tmp_path: Path):
        # File present but unparseable — fail loud rather than fall
        # back to "no override".  Hand-edits gone wrong should be
        # surfaced, not silently dropped.
        target = resolve_brand_store_path(tmp_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("{not-json", encoding="utf-8")
        with pytest.raises(BrandStoreReadError):
            read_brand_spec_if_exists(tmp_path)


# ── delete_brand_spec ───────────────────────────────────────────────


class TestDeleteBrandSpec:
    def test_returns_false_when_absent(self, tmp_path: Path):
        assert delete_brand_spec(tmp_path) is False

    def test_returns_true_when_deleted(self, tmp_path: Path):
        write_brand_spec(_populated_spec(), project_root=tmp_path)
        assert delete_brand_spec(tmp_path) is True

    def test_idempotent_after_delete(self, tmp_path: Path):
        write_brand_spec(_populated_spec(), project_root=tmp_path)
        assert delete_brand_spec(tmp_path) is True
        # Second call returns False — file is gone.
        assert delete_brand_spec(tmp_path) is False

    def test_leaves_omnisight_dir(self, tmp_path: Path):
        # We deliberately do not delete the dir — other artefacts
        # (clone-manifest.json, platform hint) may share it.
        write_brand_spec(_populated_spec(), project_root=tmp_path)
        delete_brand_spec(tmp_path)
        assert (tmp_path / ".omnisight").is_dir()

    def test_leaves_sibling_files_intact(self, tmp_path: Path):
        # A sibling clone-manifest.json must survive deletion of the
        # brand spec.
        omnisight = tmp_path / ".omnisight"
        omnisight.mkdir(parents=True)
        sibling = omnisight / "clone-manifest.json"
        sibling.write_text("{}", encoding="utf-8")
        write_brand_spec(_populated_spec(), project_root=tmp_path)
        delete_brand_spec(tmp_path)
        assert sibling.exists()
        assert sibling.read_text(encoding="utf-8") == "{}"

    def test_rejects_invalid_root(self):
        with pytest.raises(BrandStoreWriteError):
            delete_brand_spec(None)  # type: ignore[arg-type]


# ── End-to-end resolver → store plumbing ────────────────────────────


class TestEndToEndResolverToStore:
    def test_resolved_spec_persists(self, tmp_path: Path):
        # Operator runs ``--reference-url https://example.com``;
        # the W12.4 resolver returns a populated BrandSpec; W12.5
        # writes it; downstream agent edits read it back.
        from backend.scaffold_reference import resolve_reference_url

        def fake_fetch(url: str) -> tuple[int, str]:
            return 200, (
                "<style>"
                "body{font-family:'Inter',sans-serif;color:#0066ff;}"
                "h1{font-size:64px;}"
                "div{padding:8px;border-radius:4px;}"
                "</style>"
            )

        spec = resolve_reference_url(
            "https://example.com",
            fetch=fake_fetch,
            now=lambda: "2026-04-29T00:00:00+00:00",
        )
        assert spec is not None
        write_brand_spec(spec, project_root=tmp_path)
        loaded = read_brand_spec_if_exists(tmp_path)
        assert loaded == spec
        assert "#0066ff" in loaded.palette
        assert "inter" in loaded.fonts
        assert loaded.heading.h1 == 64.0

    def test_fail_soft_envelope_persists(self, tmp_path: Path):
        # Reference URL was unreachable — resolver returns an empty
        # spec with provenance.  W12.5 must persist that record so
        # the audit trail reads "we tried, got nothing" instead of
        # "operator never set --reference-url".
        from backend.scaffold_reference import resolve_reference_url

        def boom(url: str) -> tuple[int, str]:
            raise OSError("network unreachable")

        spec = resolve_reference_url(
            "https://flaky.example.com",
            fetch=boom,
            now=lambda: "2026-04-29T00:00:00+00:00",
        )
        assert spec is not None
        assert spec.is_empty
        write_brand_spec(spec, project_root=tmp_path)
        loaded = read_brand_spec_if_exists(tmp_path)
        assert loaded == spec
        assert loaded.is_empty
        assert loaded.source_url == "https://flaky.example.com"
        assert loaded.extracted_at == "2026-04-29T00:00:00+00:00"

    def test_no_resolution_means_no_file(self, tmp_path: Path):
        # Operator did not pass ``--reference-url`` at all — resolver
        # returns ``None`` and no file is written.  Downstream agent
        # edits see ``read_brand_spec_if_exists`` return ``None``
        # and fall back to project tokens.
        from backend.scaffold_reference import resolve_reference_url

        spec = resolve_reference_url(None)
        assert spec is None
        # Caller responsibility: only call write when spec is non-None.
        # ``read_brand_spec_if_exists`` must still tolerate the
        # untouched project_root.
        assert read_brand_spec_if_exists(tmp_path) is None
