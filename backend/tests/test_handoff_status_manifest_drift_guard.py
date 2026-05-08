"""FX.7.4 — drift guard for ``docs/status/handoff_status.yaml``.

Background
----------
HANDOFF.md ships ~230 entries each with a "Production status" + "Next
gate" pair. The original format was prose-only and accumulated ~6
formatting variants over time, making it unparseable without manual
reading. FX.7.4 introduces ``docs/status/handoff_status.yaml`` as the
machine-readable manifest, generated from HANDOFF.md by
``scripts/extract_handoff_status.py``.

OP-790 removes the generated manifest from git tracking. This test now
guarantees the generator still parses HANDOFF.md and emits the expected
schema without requiring the generated output to exist in a clean
checkout.

What the guard checks
---------------------
1. Re-runs the extractor functions against the current HANDOFF.md.
2. Spot-checks the generated manifest payload for invariants the generator
   guarantees (no duplicate ids, no missing required fields, every
   ``production_status`` value is in ``canonical_statuses`` plus the
   "unknown" escape hatch).
3. Sanity floor: at least 200 entries (HANDOFF.md is grow-only; if the
   parser silently drops below this the regex changed shape).

To regenerate after editing HANDOFF.md:
    python3 scripts/extract_handoff_status.py --write
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
EXTRACTOR_PATH = REPO_ROOT / "scripts" / "extract_handoff_status.py"
HANDOFF_PATH = REPO_ROOT / "HANDOFF.md"


@pytest.fixture(scope="module")
def manifest() -> dict:
    """Generated manifest payload — load once for all assertions."""
    sys.path.insert(0, str(EXTRACTOR_PATH.parent))
    import extract_handoff_status

    text = HANDOFF_PATH.read_text(encoding="utf-8")
    entries, _warnings = extract_handoff_status.parse_handoff(text)
    return extract_handoff_status.build_manifest(entries)


def test_required_artefacts_exist() -> None:
    """The source and generator must be present."""
    missing = [
        p.relative_to(REPO_ROOT)
        for p in (HANDOFF_PATH, EXTRACTOR_PATH)
        if not p.exists()
    ]
    assert not missing, f"FX.7.4 artefacts missing: {missing}"


def test_manifest_serialises_from_handoff() -> None:
    """Re-run extractor functions and parse the generated manifest.

    The checked-in manifest was removed in OP-790, so this test validates
    that the generator still produces parseable schema-v1 output from
    HANDOFF.md without requiring the generated file to exist in git.
    """
    sys.path.insert(0, str(EXTRACTOR_PATH.parent))
    import extract_handoff_status

    text = HANDOFF_PATH.read_text(encoding="utf-8")
    entries, _warnings = extract_handoff_status.parse_handoff(text)
    serialised = extract_handoff_status.serialise_manifest(
        extract_handoff_status.build_manifest(entries)
    )

    parsed = yaml.safe_load(serialised)
    assert parsed["schema_version"] == 1
    assert parsed["generated_from"] == "HANDOFF.md"


def test_manifest_schema_invariants(manifest: dict) -> None:
    """Manifest must declare schema_version=1 and the canonical fields."""
    assert manifest["schema_version"] == 1
    assert manifest["generated_from"] == "HANDOFF.md"
    assert manifest["entry_count"] == len(manifest["entries"])
    assert isinstance(manifest["canonical_statuses"], list)
    assert "dev-only" in manifest["canonical_statuses"]
    assert "deployed-active" in manifest["canonical_statuses"]


def test_manifest_entry_count_floor(manifest: dict) -> None:
    """HANDOFF.md is grow-only; the parser must keep finding ≥ 200 rows.

    A sudden drop signals the entry-header regex broke or someone
    truncated HANDOFF.md.
    """
    assert manifest["entry_count"] >= 200, (
        f"Manifest only has {manifest['entry_count']} entries; "
        f"HANDOFF.md should produce ≥ 200. The entry-header regex in "
        f"scripts/extract_handoff_status.py probably needs updating."
    )


def test_manifest_ids_unique(manifest: dict) -> None:
    """Every entry needs a unique stable id."""
    ids = [e["id"] for e in manifest["entries"]]
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    assert not duplicates, (
        f"Duplicate manifest ids: {duplicates}. The dedup suffix logic "
        f"in scripts/extract_handoff_status.py may have regressed."
    )


def test_manifest_required_fields_per_entry(manifest: dict) -> None:
    """Every entry must carry the fields downstream tooling expects."""
    required = {
        "id",
        "header_line",
        "date",
        "author",
        "title",
        "production_status",
        "next_gate",
    }
    missing_per_entry: list[tuple[str, list[str]]] = []
    for e in manifest["entries"]:
        missing = sorted(required - set(e.keys()))
        if missing:
            missing_per_entry.append((e.get("id", "<no id>"), missing))
    assert not missing_per_entry, (
        f"Manifest entries missing required fields: "
        f"{missing_per_entry[:5]} (showing first 5)"
    )


def test_production_status_values_are_canonical(manifest: dict) -> None:
    """Every Production status must be canonical OR explicit "unknown".

    "unknown" is the escape hatch for non-canonical statuses. When
    unknown, the entry must carry a ``raw_status`` so the operator can
    triage.
    """
    canonical = set(manifest["canonical_statuses"]) | {"unknown"}
    bad: list[tuple[str, str]] = []
    missing_raw: list[str] = []
    for e in manifest["entries"]:
        s = e["production_status"]
        if s not in canonical:
            bad.append((e["id"], s))
        if s == "unknown" and not e.get("raw_status"):
            missing_raw.append(e["id"])
    assert not bad, (
        f"Non-canonical production_status values: {bad[:5]}. "
        f"Either add the new status to CANONICAL_STATUSES / "
        f"EXTRA_STATUSES in scripts/extract_handoff_status.py, or fix "
        f"the HANDOFF.md entry."
    )
    assert not missing_raw, (
        f"Entries marked 'unknown' must include raw_status for triage: "
        f"{missing_raw[:5]}"
    )


def test_status_counts_sum_matches_entry_count(manifest: dict) -> None:
    """status_counts must partition the entry set."""
    total = sum(manifest["status_counts"].values())
    assert total == manifest["entry_count"], (
        f"status_counts sum ({total}) != entry_count "
        f"({manifest['entry_count']}). Generator has a categorisation "
        f"bug (a status value is in entries but not in status_counts)."
    )
