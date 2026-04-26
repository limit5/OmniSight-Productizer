"""BS.1.5 — Catalog schema + seed completeness + drift guard.

Three concerns, four test groups:

1.  **Schema structure** — ``configs/embedded_catalog/_schema.yaml`` is a
    valid Draft 2020-12 JSON Schema, and its enums/patterns line up with
    alembic 0051's CHECK constraints (family enum, install_method enum,
    required-fields list).  If alembic 0051 grows a family value but the
    JSONSchema doesn't, an operator yaml using the new value passes the
    schema lint while INSERT bombs on PG — this group catches that.

2.  **YAML structural validation** — every per-family yaml validates
    against the schema, the file's stem matches its top-level
    ``family`` key, ids are unique cross-file, and ``depends_on``
    targets resolve.  Mirrors ``scripts/check_catalog_schema.py``'s CI
    gate so a broken yaml fails *both* a fast CI lint and the
    backend-tests shard (defence in depth — the lint job can be
    skipped or temporarily disabled, the contract test cannot).

3.  **Seed completeness** — every alembic 0052 ``_SEED_ENTRIES`` row
    carries the alembic-required columns, an enum-allowed
    ``install_method``, a kebab-case id, no tenant_id (shipped
    contract), no ``custom`` family (reserved for BS.8.5 subscription
    feeds), and ``depends_on`` resolves within the seed.  Locks the
    BS.1.2 design contract that the seed is shippable today.

4.  **Drift guard** — yaml mirror in ``configs/embedded_catalog/*.yaml``
    ↔ alembic 0052 ``_SEED_ENTRIES`` per-field equality.  The yaml is
    a human-readable mirror of the alembic-frozen seed (BS.1.2 design
    decision: alembic = source of truth at upgrade time, yaml = source
    of truth at edit time, drift caught at commit time).  Editing one
    without the other lights up red here.

Why the schema + lint already in BS.1.3 isn't enough on its own
───────────────────────────────────────────────────────────────

BS.1.3's ``check_catalog_schema.py`` runs as a separate CI job
(``catalog-schema``).  That gate enforces the yaml side only — it
doesn't see alembic 0052 at all.  This test sits in the
backend-tests shard so the *combined* contract (yaml + alembic +
schema all in agreement) is locked end-to-end on the same gate that
gates merge.

Module-global state audit (per implement_phase_step.md Step 1)
──────────────────────────────────────────────────────────────

This module loads the alembic 0052 migration source as a Python
module via ``importlib`` — same pattern as
``test_alembic_0052_catalog_seed.py`` — and reads
``configs/embedded_catalog/*.yaml`` once per test using
``yaml.safe_load``.  No module-level cache, no singleton, no
ContextVar, no DB connection.  Every test re-derives state from the
on-disk files; pytest workers share nothing.  Answer #1 — every
worker reads the same files from the same checkout.
"""
from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml


BACKEND_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BACKEND_ROOT.parent
EMBEDDED_CATALOG = PROJECT_ROOT / "configs" / "embedded_catalog"
SCHEMA_PATH = EMBEDDED_CATALOG / "_schema.yaml"
MIGRATION_0052 = (
    BACKEND_ROOT / "alembic" / "versions" / "0052_catalog_seed.py"
)
MIGRATION_0051 = (
    BACKEND_ROOT / "alembic" / "versions" / "0051_catalog_tables.py"
)

# Family enum mirror — BS.1.3 schema seven values incl. 'custom' for
# BS.8.5 subscription feed.  Shipped seed uses six (no 'custom').
ALEMBIC_FAMILY_ENUM = {
    "mobile",
    "embedded",
    "web",
    "software",
    "rtos",
    "cross-toolchain",
    "custom",
}
SHIPPED_FAMILY_ENUM = ALEMBIC_FAMILY_ENUM - {"custom"}

# install_method enum mirror — alembic 0051 CHECK + BS.1.3 schema
# enum.  Adding a method requires bumping all three.
ALEMBIC_INSTALL_METHOD_ENUM = {
    "noop",
    "docker_pull",
    "shell_script",
    "vendor_installer",
}

# 6 family yaml files (BS.1.2 split).  Excludes _schema.yaml.
EXPECTED_FAMILY_FILES = {
    "mobile.yaml",
    "embedded.yaml",
    "web.yaml",
    "software.yaml",
    "rtos.yaml",
    "cross-toolchain.yaml",
}

EXPECTED_PER_FAMILY_COUNT = {
    "mobile": 6,
    "embedded": 8,
    "web": 4,
    "software": 5,
    "rtos": 3,
    "cross-toolchain": 4,
}

KEBAB_CASE_ID_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9]|-(?=[a-z0-9]))*$")


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules[name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


@pytest.fixture(scope="module")
def schema() -> dict[str, Any]:
    with SCHEMA_PATH.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle)


@pytest.fixture(scope="module")
def m0052():
    return _load_module(MIGRATION_0052, "_bs15_test_alembic_0052")


@pytest.fixture(scope="module")
def yaml_docs() -> dict[str, dict[str, Any]]:
    """Map of ``mobile.yaml`` (filename) -> parsed dict."""
    out: dict[str, dict[str, Any]] = {}
    for path in sorted(EMBEDDED_CATALOG.glob("*.yaml")):
        if path.name == "_schema.yaml":
            continue
        out[path.name] = yaml.safe_load(path.read_text())
    return out


@pytest.fixture(scope="module")
def yaml_entries_by_id(
    yaml_docs: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Flatten yaml entries keyed by id, with the parent ``family``
    folded into each entry (since per-yaml ``family:`` is top-level
    while alembic stores ``family`` per-row)."""
    out: dict[str, dict[str, Any]] = {}
    for filename, doc in yaml_docs.items():
        family = doc["family"]
        for entry in doc.get("entries", []) or []:
            merged = dict(entry)
            merged["family"] = family
            out[entry["id"]] = merged
    return out


# ─── Group 1: schema structure ────────────────────────────────────────────


class TestSchemaStructure:
    """Lock the schema's contract with alembic 0051's CHECKs.

    The lint job (BS.1.3) enforces "yaml validates against schema".
    This group enforces "schema is itself shaped to match alembic".
    """

    def test_schema_file_exists(self) -> None:
        assert SCHEMA_PATH.exists(), (
            f"schema file missing at {SCHEMA_PATH}"
        )

    def test_schema_is_valid_draft_2020_12(self, schema) -> None:
        from jsonschema import Draft202012Validator

        Draft202012Validator.check_schema(schema)

    def test_schema_advertises_2020_12_dialect(self, schema) -> None:
        assert (
            schema.get("$schema")
            == "https://json-schema.org/draft/2020-12/schema"
        )

    def test_schema_top_level_required(self, schema) -> None:
        assert set(schema.get("required", [])) == {
            "family",
            "schema_version",
            "entries",
        }

    def test_schema_top_level_additional_properties_false(
        self, schema
    ) -> None:
        assert schema.get("additionalProperties") is False

    def test_schema_family_enum_matches_alembic_check(self, schema) -> None:
        family_enum = set(
            schema["properties"]["family"]["enum"]
        )
        assert family_enum == ALEMBIC_FAMILY_ENUM, (
            f"schema family enum {family_enum} drifted from alembic 0051 "
            f"CHECK {ALEMBIC_FAMILY_ENUM} — bump both together"
        )

    def test_schema_install_method_enum_matches_alembic_check(
        self, schema
    ) -> None:
        method_enum = set(
            schema["$defs"]["install-method"]["enum"]
        )
        assert method_enum == ALEMBIC_INSTALL_METHOD_ENUM, (
            f"schema install_method enum {method_enum} drifted from "
            f"alembic 0051 CHECK {ALEMBIC_INSTALL_METHOD_ENUM} — "
            f"bump both together"
        )

    def test_schema_entry_required_columns(self, schema) -> None:
        required = set(schema["$defs"]["entry"]["required"])
        # Mirrors alembic 0051's NOT NULL columns on catalog_entries
        # that the BS.1.2 seed must always populate.
        assert required == {
            "id",
            "vendor",
            "display_name",
            "version",
            "install_method",
        }

    def test_schema_entry_additional_properties_false(
        self, schema
    ) -> None:
        # Typo guard: a misspelled field name (e.g. ``versoin``) must
        # not silently round-trip.  metadata is the deliberate escape
        # hatch (open object) — that's checked separately.
        assert schema["$defs"]["entry"]["additionalProperties"] is False

    def test_schema_metadata_is_open_object(self, schema) -> None:
        # R24 forward-compat: third-party vendors ship arbitrary
        # metadata keys; the schema must not reject them.
        meta = schema["$defs"]["entry"]["properties"]["metadata"]
        assert meta["type"] == "object"
        assert "additionalProperties" not in meta

    def test_schema_entry_id_pattern_is_kebab_case(self, schema) -> None:
        assert (
            schema["$defs"]["entry-id"]["pattern"]
            == "^[a-z0-9](?:[a-z0-9]|-(?=[a-z0-9]))*$"
        )

    def test_schema_sha256_oneof_hex_or_null(self, schema) -> None:
        sha = schema["$defs"]["entry"]["properties"]["sha256"]
        assert "oneOf" in sha
        kinds = []
        for branch in sha["oneOf"]:
            if "$ref" in branch:
                kinds.append("hex")
            elif branch.get("type") == "null":
                kinds.append("null")
        assert sorted(kinds) == ["hex", "null"], (
            f"sha256 must be (64-hex-string OR null) — got {sha['oneOf']}"
        )


# ─── Group 2: yaml structural validation ──────────────────────────────────


class TestYamlStructuralValidation:
    """Mirror of ``scripts/check_catalog_schema.py`` invariants.

    Defence in depth: the CI lint job can be temporarily disabled or
    skipped; the backend-tests gate cannot.  Drifting between the
    two is itself a tell.
    """

    def test_six_family_files_present(self) -> None:
        files = {
            p.name for p in EMBEDDED_CATALOG.glob("*.yaml")
            if p.name != "_schema.yaml"
        }
        assert files == EXPECTED_FAMILY_FILES, (
            f"family yaml file set drifted: extra={files - EXPECTED_FAMILY_FILES} "
            f"missing={EXPECTED_FAMILY_FILES - files}"
        )

    def test_schema_yaml_excluded_from_family_set(self) -> None:
        # _schema.yaml is the JSONSchema, not a family file.  Tests
        # that glob ``*.yaml`` must exclude it (BS.1.4-followup-yaml-split
        # was the failure mode this guards against).
        assert (EMBEDDED_CATALOG / "_schema.yaml").exists()
        assert "_schema.yaml" not in EXPECTED_FAMILY_FILES

    def test_every_family_yaml_validates_against_schema(
        self, schema, yaml_docs
    ) -> None:
        from jsonschema import Draft202012Validator

        validator = Draft202012Validator(schema)
        violations: list[str] = []
        for filename, doc in yaml_docs.items():
            for err in sorted(
                validator.iter_errors(doc),
                key=lambda e: tuple(str(p) for p in e.absolute_path),
            ):
                crumbs = "/".join(str(p) for p in err.absolute_path)
                violations.append(f"{filename}:{crumbs}: {err.message}")
        assert not violations, "\n".join(violations)

    def test_every_yaml_filename_matches_top_level_family(
        self, yaml_docs
    ) -> None:
        for filename, doc in yaml_docs.items():
            stem = filename.rsplit(".", 1)[0]
            assert doc.get("family") == stem, (
                f"{filename} carries family={doc.get('family')!r}, "
                f"filename stem is {stem!r} — they must match"
            )

    def test_cross_file_id_uniqueness(self, yaml_docs) -> None:
        seen: dict[str, str] = {}
        dups: list[str] = []
        for filename, doc in yaml_docs.items():
            for entry in doc.get("entries", []) or []:
                eid = entry["id"]
                if eid in seen:
                    dups.append(
                        f"{eid!r} in {filename} also defined in {seen[eid]}"
                    )
                else:
                    seen[eid] = filename
        assert not dups, "\n".join(dups)

    def test_depends_on_resolves_within_catalog(self, yaml_docs) -> None:
        all_ids: set[str] = set()
        for doc in yaml_docs.values():
            for entry in doc.get("entries", []) or []:
                all_ids.add(entry["id"])
        unresolved: list[str] = []
        for filename, doc in yaml_docs.items():
            for entry in doc.get("entries", []) or []:
                for dep in entry.get("depends_on", []) or []:
                    if dep not in all_ids:
                        unresolved.append(
                            f"{filename}:{entry['id']} depends_on "
                            f"{dep!r} — not defined in any catalog yaml"
                        )
        assert not unresolved, "\n".join(unresolved)

    def test_per_family_yaml_entry_count(self, yaml_docs) -> None:
        for filename, doc in yaml_docs.items():
            family = doc["family"]
            expected = EXPECTED_PER_FAMILY_COUNT[family]
            got = len(doc.get("entries", []) or [])
            assert got == expected, (
                f"{filename} ({family}): expected {expected} entries, "
                f"got {got} — split changed?  Update both alembic "
                f"0052 _SEED_ENTRIES and EXPECTED_PER_FAMILY_COUNT"
            )

    def test_every_yaml_schema_version_is_1(self, yaml_docs) -> None:
        # All shipped yamls today are schema_version: 1; bump means a
        # new alembic revision.  Catch a stray bump by hand.
        versions = {doc["schema_version"] for doc in yaml_docs.values()}
        assert versions == {1}, (
            f"schema_version drift: {versions} — bump requires a new "
            f"alembic revision plus an explicit migration plan"
        )


# ─── Group 3: seed completeness (alembic 0052 internals) ──────────────────


class TestSeedCompleteness:
    """Lock the BS.1.2 alembic 0052 ``_SEED_ENTRIES`` invariants.

    Some of these overlap test_alembic_0052_catalog_seed.py's
    structural group; we re-assert here so the BS.1.5 contract is
    self-contained and survives a hypothetical refactor that drops
    those structural tests.
    """

    def test_every_seed_entry_has_required_columns(self, m0052) -> None:
        required = {
            "id",
            "vendor",
            "family",
            "display_name",
            "version",
            "install_method",
        }
        for entry in m0052.SEED_ENTRIES:
            missing = required - set(entry.keys())
            assert not missing, (
                f"{entry.get('id')!r}: missing required columns {missing}"
            )
            for col in required:
                value = entry[col]
                assert isinstance(value, str) and value, (
                    f"{entry['id']}.{col} must be a non-empty string, "
                    f"got {value!r}"
                )

    def test_every_seed_install_method_in_alembic_enum(
        self, m0052
    ) -> None:
        for entry in m0052.SEED_ENTRIES:
            assert entry["install_method"] in ALEMBIC_INSTALL_METHOD_ENUM, (
                f"{entry['id']}.install_method={entry['install_method']!r} "
                f"not in {ALEMBIC_INSTALL_METHOD_ENUM}"
            )

    def test_every_seed_family_in_shipped_enum(self, m0052) -> None:
        # 'custom' is reserved for BS.8.5 third-party subscription
        # feed entries — no shipped row may carry it.
        for entry in m0052.SEED_ENTRIES:
            assert entry["family"] in SHIPPED_FAMILY_ENUM, (
                f"{entry['id']}.family={entry['family']!r} not in "
                f"{SHIPPED_FAMILY_ENUM} (custom reserved for BS.8.5)"
            )

    def test_every_seed_id_matches_kebab_case_pattern(
        self, m0052
    ) -> None:
        for entry in m0052.SEED_ENTRIES:
            assert KEBAB_CASE_ID_PATTERN.match(entry["id"]), (
                f"{entry['id']!r} fails kebab-case pattern"
            )

    def test_every_seed_id_within_length_bounds(self, m0052) -> None:
        for entry in m0052.SEED_ENTRIES:
            length = len(entry["id"])
            assert 2 <= length <= 64, (
                f"{entry['id']!r} length {length} outside [2, 64]"
            )

    def test_no_seed_entry_carries_tenant_id(self, m0052) -> None:
        # Shipped rows are tenant-scopeless (alembic 0051 CHECK enforces
        # source='shipped' XOR tenant_id IS NULL).
        for entry in m0052.SEED_ENTRIES:
            assert "tenant_id" not in entry, entry["id"]

    def test_no_seed_entry_overrides_source(self, m0052) -> None:
        # Migration hard-codes source='shipped' in _build_insert; an
        # entry-side override would be silently ignored.
        for entry in m0052.SEED_ENTRIES:
            assert "source" not in entry, entry["id"]

    def test_seed_depends_on_resolves(self, m0052) -> None:
        ids = {e["id"] for e in m0052.SEED_ENTRIES}
        unresolved: list[str] = []
        for entry in m0052.SEED_ENTRIES:
            for dep in entry.get("depends_on", []) or []:
                if dep not in ids:
                    unresolved.append(
                        f"{entry['id']} depends_on {dep!r} — not in seed"
                    )
        assert not unresolved, "\n".join(unresolved)

    def test_seed_sha256_is_null_or_64_hex(self, m0052) -> None:
        # BS.1.2 design: all shipped rows ship with sha256 NULL.  When
        # BS.7 back-fills digests in a later alembic rev, this
        # assertion will need either an exemption window or to be
        # updated to the hex pattern.
        hex_re = re.compile(r"^[0-9a-f]{64}$")
        for entry in m0052.SEED_ENTRIES:
            sha = entry.get("sha256")
            assert sha is None or (
                isinstance(sha, str) and hex_re.match(sha)
            ), f"{entry['id']}.sha256={sha!r} — must be null or 64-hex"

    def test_seed_size_bytes_within_sane_range(self, m0052) -> None:
        # 1 TiB cap mirrors schema's maximum.  Negative or > 1 TiB
        # is a typo (extra zero / wrong unit).
        for entry in m0052.SEED_ENTRIES:
            sz = entry.get("size_bytes")
            if sz is None:
                continue
            assert isinstance(sz, int)
            assert 0 <= sz <= 1099511627776, (
                f"{entry['id']}.size_bytes={sz} outside [0, 1 TiB]"
            )


# ─── Group 4: yaml ↔ alembic seed drift guard (per-field equality) ───────


class TestYamlSeedDriftGuard:
    """The load-bearing BS.1.5 contract.

    Every shipped catalog change (add an entry, bump a version, fix a
    typo) must touch the yaml mirror AND the alembic seed.  Any
    asymmetry shows up here as a per-field diff with the offending id
    in the assertion message.
    """

    # Fields that round-trip 1:1 between yaml and alembic seed.
    _SCALAR_FIELDS = (
        "vendor",
        "family",
        "display_name",
        "version",
        "install_method",
    )

    # Optional scalar fields — both sides must agree on absence too.
    _OPTIONAL_SCALAR_FIELDS = (
        "install_url",
        "size_bytes",
        "sha256",
    )

    def test_yaml_id_set_equals_seed_id_set(
        self, m0052, yaml_entries_by_id
    ) -> None:
        seed_ids = {e["id"] for e in m0052.SEED_ENTRIES}
        yaml_ids = set(yaml_entries_by_id.keys())
        only_yaml = yaml_ids - seed_ids
        only_seed = seed_ids - yaml_ids
        assert not only_yaml, (
            f"id present in yaml but not in alembic 0052 seed: "
            f"{sorted(only_yaml)}"
        )
        assert not only_seed, (
            f"id present in alembic 0052 seed but not in yaml mirror: "
            f"{sorted(only_seed)}"
        )

    def test_per_family_yaml_count_equals_seed_count(
        self, m0052, yaml_docs
    ) -> None:
        seed_by_family: dict[str, int] = {}
        for entry in m0052.SEED_ENTRIES:
            seed_by_family[entry["family"]] = (
                seed_by_family.get(entry["family"], 0) + 1
            )
        yaml_by_family = {
            doc["family"]: len(doc.get("entries", []) or [])
            for doc in yaml_docs.values()
        }
        assert seed_by_family == yaml_by_family, (
            f"per-family count drift: alembic={seed_by_family} "
            f"yaml={yaml_by_family}"
        )

    def test_every_seed_entry_has_yaml_mirror(
        self, m0052, yaml_entries_by_id
    ) -> None:
        for entry in m0052.SEED_ENTRIES:
            assert entry["id"] in yaml_entries_by_id, (
                f"alembic 0052 seed entry {entry['id']!r} has no yaml "
                f"mirror in configs/embedded_catalog/"
            )

    @pytest.mark.parametrize("field", _SCALAR_FIELDS)
    def test_scalar_field_per_entry_equality(
        self, field, m0052, yaml_entries_by_id
    ) -> None:
        diffs: list[str] = []
        for entry in m0052.SEED_ENTRIES:
            yaml_entry = yaml_entries_by_id.get(entry["id"])
            if yaml_entry is None:
                continue  # reported by test_every_seed_entry_has_yaml_mirror
            seed_value = entry[field]
            yaml_value = yaml_entry.get(field)
            if seed_value != yaml_value:
                diffs.append(
                    f"{entry['id']}.{field}: alembic={seed_value!r} "
                    f"yaml={yaml_value!r}"
                )
        assert not diffs, "\n".join(diffs)

    @pytest.mark.parametrize("field", _OPTIONAL_SCALAR_FIELDS)
    def test_optional_scalar_field_per_entry_equality(
        self, field, m0052, yaml_entries_by_id
    ) -> None:
        diffs: list[str] = []
        for entry in m0052.SEED_ENTRIES:
            yaml_entry = yaml_entries_by_id.get(entry["id"])
            if yaml_entry is None:
                continue
            seed_value = entry.get(field)
            yaml_value = yaml_entry.get(field)
            # Treat missing key + explicit None symmetrically.
            if seed_value != yaml_value:
                diffs.append(
                    f"{entry['id']}.{field}: alembic={seed_value!r} "
                    f"yaml={yaml_value!r}"
                )
        assert not diffs, "\n".join(diffs)

    def test_depends_on_per_entry_equality(
        self, m0052, yaml_entries_by_id
    ) -> None:
        # Order-insensitive: yaml authors may reorder for readability.
        # alembic 0052 _build_insert uses ``json.dumps(depends_on)`` which
        # preserves order, so on PG/SQLite the round-trip is ordered;
        # but that's the wire-level concern.  The drift gate is "same
        # set of deps".  If order matters operationally the rule
        # should tighten — for now (BS.1.2 first-seed) it doesn't.
        diffs: list[str] = []
        for entry in m0052.SEED_ENTRIES:
            yaml_entry = yaml_entries_by_id.get(entry["id"])
            if yaml_entry is None:
                continue
            seed_deps = sorted(entry.get("depends_on", []) or [])
            yaml_deps = sorted(yaml_entry.get("depends_on", []) or [])
            if seed_deps != yaml_deps:
                diffs.append(
                    f"{entry['id']}.depends_on: alembic={seed_deps} "
                    f"yaml={yaml_deps}"
                )
        assert not diffs, "\n".join(diffs)

    def test_metadata_per_entry_equality(
        self, m0052, yaml_entries_by_id
    ) -> None:
        # Metadata is an open dict (R24 forward-compat).  Compare with
        # JSON-canonicalised (sort_keys) round-trip so dict insertion
        # order doesn't show up as a diff.
        diffs: list[str] = []
        for entry in m0052.SEED_ENTRIES:
            yaml_entry = yaml_entries_by_id.get(entry["id"])
            if yaml_entry is None:
                continue
            seed_meta = entry.get("metadata", {}) or {}
            yaml_meta = yaml_entry.get("metadata", {}) or {}
            seed_canonical = json.dumps(seed_meta, sort_keys=True)
            yaml_canonical = json.dumps(yaml_meta, sort_keys=True)
            if seed_canonical != yaml_canonical:
                diffs.append(
                    f"{entry['id']}.metadata diverges:\n"
                    f"  alembic: {seed_canonical}\n"
                    f"  yaml:    {yaml_canonical}"
                )
        assert not diffs, "\n".join(diffs)

    def test_filename_to_family_groups_match_seed(
        self, m0052, yaml_docs
    ) -> None:
        # Each yaml's family bucket must contain exactly the alembic
        # entries with that family.  Catches an entry being moved
        # between yamls without bumping its alembic seed family.
        yaml_groups: dict[str, set[str]] = {}
        for doc in yaml_docs.values():
            family = doc["family"]
            yaml_groups.setdefault(family, set()).update(
                e["id"] for e in (doc.get("entries", []) or [])
            )
        seed_groups: dict[str, set[str]] = {}
        for entry in m0052.SEED_ENTRIES:
            seed_groups.setdefault(entry["family"], set()).add(entry["id"])
        all_families = yaml_groups.keys() | seed_groups.keys()
        only_in_yaml = {
            f: sorted(yaml_groups.get(f, set()) - seed_groups.get(f, set()))
            for f in all_families
        }
        only_in_seed = {
            f: sorted(seed_groups.get(f, set()) - yaml_groups.get(f, set()))
            for f in all_families
        }
        only_in_yaml = {f: ids for f, ids in only_in_yaml.items() if ids}
        only_in_seed = {f: ids for f, ids in only_in_seed.items() if ids}
        assert yaml_groups == seed_groups, (
            f"family bucket drift between yaml and alembic seed:\n"
            f"  yaml-only-by-family: {only_in_yaml}\n"
            f"  seed-only-by-family: {only_in_seed}"
        )
