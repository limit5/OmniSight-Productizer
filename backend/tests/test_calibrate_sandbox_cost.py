"""H4b — Calibration script unit tests.

Pure-function coverage for ``scripts/calibrate_sandbox_cost.py``. The
script's DB-fetch path is exercised in production; here we drive
``calibrate()`` with synthetic ``AuditRow`` lists so the calibration
math + class-inference + diff renderer are pinned regardless of which
DB backend the operator runs against.

Test surface:
    * ``parse_memory_limit_to_mb`` — every docker-style suffix the
      audit rows have ever recorded.
    * ``infer_class`` — tier-aware nearest-tokens lookup, including
      drift cases (an unknown tier or a tokens value not in the table).
    * ``calibrate`` — start/end pairing rules, orphan accounting, the
      ``MIN_DURATION_S`` race floor, OOM peak-memory upgrade, and the
      lightest-class normalisation.
    * ``render_yaml`` / ``render_text`` / ``render_json`` — surface
      contract that downstream consumers (operator + audit row) read.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

# Make ``scripts/`` importable as a package — the script lives outside
# the backend package so the conftest's path setup doesn't reach it.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts import calibrate_sandbox_cost as cal  # noqa: E402


# ─────────────────────────────────────────────────────────────────────
#  parse_memory_limit_to_mb
# ─────────────────────────────────────────────────────────────────────

class TestParseMemoryLimit:
    """Audit rows record memory in many flavours — the parser has to
    accept them all without a single bad row poisoning the aggregate."""

    @pytest.mark.parametrize("raw,expected_mb", [
        ("512m", 512.0),
        ("1g", 1024.0),
        ("256MiB", 256.0),
        ("2GiB", 2048.0),
        ("1024", 1024.0 / (1024 * 1024)),  # bare bytes
        ("1MB", 10**6 / (1024 * 1024)),     # decimal MB
    ])
    def test_parses_known_suffixes(self, raw, expected_mb):
        assert cal.parse_memory_limit_to_mb(raw) == pytest.approx(expected_mb)

    def test_parses_int_bytes_input(self):
        # Some legacy audit rows recorded raw int bytes.
        assert cal.parse_memory_limit_to_mb(2 * 1024 * 1024) == pytest.approx(2.0)

    @pytest.mark.parametrize("bad", ["", None, "garbage", "12xy"])
    def test_returns_zero_on_unparseable(self, bad):
        assert cal.parse_memory_limit_to_mb(bad) == 0.0


# ─────────────────────────────────────────────────────────────────────
#  Canonical class table
# ─────────────────────────────────────────────────────────────────────

class TestCanonicalClassTable:
    """The table must surface every ``SandboxCostWeight`` member with the
    fields the diff renderer + yaml writer expect."""

    def test_exposes_all_h4a_classes(self):
        names = set(cal.canonical_class_table().keys())
        # Lock the H4a roster — adding a member without updating the
        # tier hint dict above would silently bucket new launches into
        # the lightweight class.
        assert names == {
            "gvisor_lightweight",
            "docker_t2_networked",
            "phase64c_local_compile",
            "phase64c_qemu_aarch64",
            "phase64c_ssh_remote",
        }

    def test_each_entry_has_required_metadata(self):
        for name, meta in cal.canonical_class_table().items():
            for key in ("tokens", "memory_mb", "cpu_cores",
                        "burst", "use_case", "tier_hint"):
                assert key in meta, f"{name} missing {key}"


# ─────────────────────────────────────────────────────────────────────
#  infer_class — class identity at the (tier, tokens) keypair
# ─────────────────────────────────────────────────────────────────────

class TestInferClass:
    @pytest.fixture
    def canonical(self):
        return cal.canonical_class_table()

    def test_exact_t1_lightweight_match(self, canonical):
        assert cal.infer_class("t1", 1.0, canonical) == "gvisor_lightweight"

    def test_exact_networked_match(self, canonical):
        assert cal.infer_class("networked", 2.0, canonical) == "docker_t2_networked"

    def test_exact_t3_local_compile_match(self, canonical):
        assert cal.infer_class("t3-local", 4.0, canonical) == "phase64c_local_compile"

    def test_unknown_tier_falls_back_to_closest_tokens(self, canonical):
        # No tier hint -> walk all classes by token distance. tokens=0.6
        # is closest to ssh_remote (0.5).
        assert cal.infer_class("zzz-unknown", 0.6, canonical) == "phase64c_ssh_remote"

    def test_t1_with_drifted_tokens_picks_nearest_t1(self, canonical):
        # 1.2 tokens still resolves to lightweight (1.0) over
        # qemu_aarch64 (3.0) or ssh_remote (0.5) — nearest in the t1 set.
        assert cal.infer_class("t1", 1.2, canonical) == "gvisor_lightweight"

    def test_returns_none_when_both_inputs_missing(self, canonical):
        assert cal.infer_class(None, None, canonical) is None


# ─────────────────────────────────────────────────────────────────────
#  calibrate() — the integration
# ─────────────────────────────────────────────────────────────────────

def _row(idx: int, ts: float, action: str, entity_id: str, **after) -> cal.AuditRow:
    return cal.AuditRow(id=idx, ts=ts, action=action, entity_id=entity_id,
                        after=dict(after))


class TestCalibrate:
    NOW = 1_700_000_000.0  # frozen wall-clock for deterministic timestamps

    def test_pairs_launch_with_subsequent_kill_on_same_entity(self):
        rows = [
            _row(1, self.NOW - 100, "sandbox_launched", "c1",
                 tier="t1", tenant_budget=1.0, memory="512m"),
            _row(2, self.NOW - 60, "sandbox_killed", "c1",
                 reason="lifetime", tier="t1", lifetime_s=40),
        ]
        r = cal.calibrate(rows, window_days=7, now=self.NOW)
        assert r.total_paired == 1
        assert r.total_orphaned == 0
        gv = r.classes["gvisor_lightweight"]
        assert gv.sample_count == 1
        assert gv.duration_s_total == pytest.approx(40.0)
        assert gv.cpu_token_s_total == pytest.approx(40.0)  # 1.0 * 40

    def test_orphan_launch_with_no_end_event(self):
        rows = [
            _row(1, self.NOW - 100, "sandbox_launched", "c1",
                 tier="t1", tenant_budget=1.0, memory="512m"),
        ]
        r = cal.calibrate(rows, window_days=7, now=self.NOW)
        assert r.total_paired == 0
        assert r.total_orphaned == 1
        # No new weight pinned — falls back to the H4a default.
        assert r.new_weights["gvisor_lightweight"] == 1.0

    def test_drops_sub_minimum_duration_runs(self):
        # Docker race: launch + immediate end < MIN_DURATION_S.
        rows = [
            _row(1, self.NOW - 100.0, "sandbox_launched", "c1",
                 tier="t1", tenant_budget=1.0, memory="512m"),
            _row(2, self.NOW - 99.9, "sandbox_killed", "c1",
                 reason="error", tier="t1"),
        ]
        r = cal.calibrate(rows, window_days=7, now=self.NOW)
        # Pairing happened in the matcher but didn't accumulate stats.
        assert r.total_paired == 0
        assert r.classes["gvisor_lightweight"].sample_count == 0

    def test_oom_upgrades_peak_memory(self):
        rows = [
            _row(1, self.NOW - 200, "sandbox_launched", "c1",
                 tier="t3-local", tenant_budget=4.0, memory="2048m"),
            _row(2, self.NOW - 100, "sandbox.oom", "c1",
                 tier="t3-local", memory_limit="2048m", exit_code=137),
        ]
        r = cal.calibrate(rows, window_days=7, now=self.NOW)
        compile_stats = r.classes["phase64c_local_compile"]
        assert compile_stats.oom_count == 1
        assert compile_stats.peak_mem_mb == 2048.0

    def test_ends_only_pair_to_first_compatible_launch(self):
        # Two launches on the same entity name (container reused after
        # cleanup); each end pairs with its preceding launch only.
        rows = [
            _row(1, self.NOW - 400, "sandbox_launched", "c1",
                 tier="t1", tenant_budget=1.0, memory="512m"),
            _row(2, self.NOW - 380, "sandbox_killed", "c1",
                 tier="t1", reason="lifetime"),
            _row(3, self.NOW - 200, "sandbox_launched", "c1",
                 tier="t1", tenant_budget=1.0, memory="512m"),
            _row(4, self.NOW - 100, "sandbox_killed", "c1",
                 tier="t1", reason="lifetime"),
        ]
        r = cal.calibrate(rows, window_days=7, now=self.NOW)
        assert r.total_paired == 2
        # Two distinct end ids consumed — no double-counting.
        assert r.classes["gvisor_lightweight"].sample_count == 2

    def test_normalisation_pins_lightest_class_to_one(self):
        # Two classes both sampled. The lighter (lower mean CPU x s)
        # must be pinned to 1.0 token; the other scales relative to it.
        rows = [
            # gvisor: 1 token x 20s = 20 cpu_token_s
            _row(1, self.NOW - 1000, "sandbox_launched", "lt",
                 tier="t1", tenant_budget=1.0, memory="512m"),
            _row(2, self.NOW - 980, "sandbox_killed", "lt",
                 tier="t1", reason="lifetime"),
            # local_compile: 4 tokens x 100s = 400 cpu_token_s
            _row(3, self.NOW - 900, "sandbox_launched", "cc",
                 tier="t3-local", tenant_budget=4.0, memory="2048m"),
            _row(4, self.NOW - 800, "sandbox_killed", "cc",
                 tier="t3-local", reason="lifetime"),
        ]
        r = cal.calibrate(rows, window_days=7, now=self.NOW)
        assert r.new_weights["gvisor_lightweight"] == 1.0
        # 400 / 20 = 20.0 (matches manual sanity check above).
        assert r.new_weights["phase64c_local_compile"] == pytest.approx(20.0)
        # Unsampled classes fall back to their H4a default.
        assert r.new_weights["phase64c_qemu_aarch64"] == 3.0
        assert r.new_weights["phase64c_ssh_remote"] == 0.5

    def test_zero_budget_legacy_launch_does_not_collapse_table(self):
        # An older audit row that recorded tenant_budget=0 must not pin
        # the reference at 0 cpu_token_s and collapse every other
        # class to division-by-zero. We expect the zero-budget class to
        # be skipped from normalisation (mean cpu_token_s == 0).
        rows = [
            _row(1, self.NOW - 200, "sandbox_launched", "c1",
                 tier="t1", tenant_budget=0.0, memory="512m"),
            _row(2, self.NOW - 100, "sandbox_killed", "c1",
                 tier="t1", reason="lifetime"),
            _row(3, self.NOW - 90, "sandbox_launched", "c2",
                 tier="t1", tenant_budget=1.0, memory="512m"),
            _row(4, self.NOW - 30, "sandbox_killed", "c2",
                 tier="t1", reason="lifetime"),
        ]
        r = cal.calibrate(rows, window_days=7, now=self.NOW)
        # gvisor saw both rows (the zero-budget one bucketed by tier);
        # its mean cpu_token_s is non-zero because the second sample
        # has budget=1.0. The reference is still itself.
        assert r.new_weights["gvisor_lightweight"] == 1.0

    def test_records_host_ring_size(self):
        r = cal.calibrate([], window_days=7, now=self.NOW, host_ring_size=42)
        assert r.host_ring_size == 42


# ─────────────────────────────────────────────────────────────────────
#  Renderers
# ─────────────────────────────────────────────────────────────────────

class TestRenderers:
    NOW = 1_700_000_000.0

    def _result_with_one_calibrated_class(self):
        rows = [
            _row(1, self.NOW - 100, "sandbox_launched", "c1",
                 tier="t1", tenant_budget=1.0, memory="512m"),
            _row(2, self.NOW - 60, "sandbox_killed", "c1",
                 reason="lifetime", tier="t1"),
        ]
        return cal.calibrate(rows, window_days=7, now=self.NOW)

    def test_text_render_includes_header_and_table(self):
        text = cal.render_text(self._result_with_one_calibrated_class())
        assert "Sandbox cost calibration" in text
        # Classes appear sorted alphabetically.
        assert "gvisor_lightweight" in text
        # Table header row.
        assert "| Class | Tier | Old | New" in text

    def test_text_render_empty_window_shows_notice(self):
        empty = cal.calibrate([], window_days=7, now=self.NOW)
        text = cal.render_text(empty)
        assert "No paired sandbox launches" in text

    def test_json_render_round_trips(self):
        result = self._result_with_one_calibrated_class()
        payload = json.loads(cal.render_json(result))
        assert payload["window_days"] == 7
        assert payload["total_paired"] == 1
        assert "gvisor_lightweight" in payload["classes"]
        assert payload["classes"]["gvisor_lightweight"]["new_tokens"] == 1.0

    def test_yaml_render_includes_all_classes(self):
        result = self._result_with_one_calibrated_class()
        yaml_text = cal.render_yaml(result)
        # Header lines.
        assert "H4b — Auto-generated sandbox cost weights." in yaml_text
        # All 5 H4a classes are written even when only one was sampled.
        for name in ("gvisor_lightweight", "docker_t2_networked",
                     "phase64c_local_compile", "phase64c_qemu_aarch64",
                     "phase64c_ssh_remote"):
            assert f"  {name}:" in yaml_text
        # Sampled class records its observation count.
        assert "    sample_count: 1" in yaml_text


# ─────────────────────────────────────────────────────────────────────
#  YAML write — atomic + parseable
# ─────────────────────────────────────────────────────────────────────

class TestWriteYaml:
    def test_writes_file_with_expected_header(self, tmp_path):
        result = cal.calibrate([], window_days=7, now=1_700_000_000.0)
        out = tmp_path / "weights.yaml"
        cal.write_yaml(result, out)
        text = out.read_text(encoding="utf-8")
        assert text.startswith("# H4b")
        assert "weights:" in text
        # Tmp file is cleaned up.
        assert not out.with_suffix(".yaml.tmp").exists()

    def test_yaml_is_pyyaml_parseable_when_available(self, tmp_path):
        # PyYAML lives in the backend deps so this assertion documents
        # the consumer-side contract: the loader the I6 wiring will use
        # must successfully read what we wrote.
        yaml = pytest.importorskip("yaml")
        rows = [
            _row(1, 1_700_000_000.0 - 100, "sandbox_launched", "c1",
                 tier="t1", tenant_budget=1.0, memory="512m"),
            _row(2, 1_700_000_000.0 - 60, "sandbox_killed", "c1",
                 reason="lifetime", tier="t1"),
        ]
        result = cal.calibrate(rows, window_days=7, now=1_700_000_000.0)
        out = tmp_path / "weights.yaml"
        cal.write_yaml(result, out)
        loaded = yaml.safe_load(out.read_text(encoding="utf-8"))
        assert loaded["calibration_window_days"] == 7
        assert loaded["sample_count"] == 1
        assert "weights" in loaded
        assert loaded["weights"]["gvisor_lightweight"]["tokens"] == 1.0


# ─────────────────────────────────────────────────────────────────────
#  H4b row 2589 — Δmem_peak from host_metrics ring per sandbox window
# ─────────────────────────────────────────────────────────────────────

class _FakeHost:
    """Minimal duck-type for ``HostSnapshot.host`` — just exposes
    ``mem_used_gb``. ``calibrate()`` uses ``getattr(...)`` paths so
    we don't need to import the real ``HostSample`` dataclass here."""

    def __init__(self, mem_used_gb: float) -> None:
        self.mem_used_gb = mem_used_gb


class _FakeSnap:
    """Minimal duck-type for ``HostSnapshot`` itself."""

    def __init__(self, ts: float, mem_used_gb: float) -> None:
        self.sampled_at = ts
        self.host = _FakeHost(mem_used_gb)


def _snap(ts: float, mem_gb: float) -> _FakeSnap:
    return _FakeSnap(ts, mem_gb)


class TestDeltaMemPeakWindow:
    """Direct coverage of the per-window helper — pinning the contract
    callers depend on (None when no ring coverage; clamp negatives;
    GB→MB unit conversion)."""

    def test_returns_none_when_ring_empty(self):
        assert cal._delta_mem_peak_mb(0.0, 100.0, []) is None
        assert cal._delta_mem_peak_mb(0.0, 100.0, None) is None

    def test_returns_none_when_no_samples_in_window(self):
        snaps = [_snap(50.0, 4.0), _snap(60.0, 5.0)]
        # Window ends before first sample.
        assert cal._delta_mem_peak_mb(10.0, 30.0, snaps) is None
        # Window starts after last sample.
        assert cal._delta_mem_peak_mb(70.0, 90.0, snaps) is None

    def test_computes_peak_minus_baseline_in_mb(self):
        snaps = [
            _snap(100.0, 4.0),  # baseline (first in window)
            _snap(110.0, 5.5),  # peak inside window
            _snap(120.0, 4.2),
        ]
        # 5.5 - 4.0 = 1.5 GB = 1536 MB
        result = cal._delta_mem_peak_mb(100.0, 130.0, snaps)
        assert result == pytest.approx(1.5 * 1024.0)

    def test_clamps_negative_delta_to_zero(self):
        # Other workload exited mid-window, freeing host RAM. Sandbox
        # contributed no upward pressure → 0.0, not negative.
        snaps = [_snap(100.0, 8.0), _snap(110.0, 5.0)]
        result = cal._delta_mem_peak_mb(100.0, 120.0, snaps)
        assert result == 0.0

    def test_window_filtering_is_inclusive_at_both_ends(self):
        snaps = [_snap(100.0, 4.0), _snap(150.0, 6.0)]
        result = cal._delta_mem_peak_mb(100.0, 150.0, snaps)
        assert result == pytest.approx(2.0 * 1024.0)

    def test_orders_unsorted_input_before_picking_baseline(self):
        # If callers pass an unordered list, we still pick the
        # earliest sample inside the window as baseline.
        snaps = [_snap(150.0, 7.0), _snap(100.0, 4.0), _snap(120.0, 6.0)]
        result = cal._delta_mem_peak_mb(100.0, 150.0, snaps)
        # Earliest in window = 100/4.0; peak = 7.0; Δ = 3 GB.
        assert result == pytest.approx(3.0 * 1024.0)

    def test_returns_none_on_malformed_snapshot(self):
        # A snapshot missing ``host.mem_used_gb`` shouldn't crash —
        # the calibrator's defence is "drop the signal, continue
        # without it" so a single bad ring entry doesn't poison the
        # whole calibration.
        class _Broken:
            sampled_at = 100.0
            # No .host attribute at all.

        assert cal._delta_mem_peak_mb(50.0, 200.0, [_Broken()]) is None


class TestCalibrateWithDeltaMem:
    """End-to-end Δmem_peak accumulation through ``calibrate()``."""

    NOW = 1_700_000_000.0

    def test_accumulates_per_class_delta_mem_when_ring_covers_window(self):
        rows = [
            _row(1, self.NOW - 200, "sandbox_launched", "c1",
                 tier="t1", tenant_budget=1.0, memory="512m"),
            _row(2, self.NOW - 100, "sandbox_killed", "c1",
                 tier="t1", reason="lifetime"),
        ]
        snaps = [
            _snap(self.NOW - 200, 4.0),
            _snap(self.NOW - 150, 5.0),
            _snap(self.NOW - 110, 4.8),
        ]
        r = cal.calibrate(rows, window_days=7, now=self.NOW,
                          host_snapshots=snaps)
        gv = r.classes["gvisor_lightweight"]
        assert gv.delta_mem_sample_count == 1
        # 5.0 - 4.0 = 1 GB = 1024 MB
        assert gv.mean_delta_mem_peak_mb == pytest.approx(1024.0)

    def test_no_host_snapshots_falls_back_to_cpu_only_path(self):
        rows = [
            _row(1, self.NOW - 100, "sandbox_launched", "c1",
                 tier="t1", tenant_budget=1.0, memory="512m"),
            _row(2, self.NOW - 60, "sandbox_killed", "c1",
                 tier="t1", reason="lifetime"),
        ]
        # Default host_snapshots=None — Δmem stays at zero, weight
        # derivation works exactly like before this row landed.
        r = cal.calibrate(rows, window_days=7, now=self.NOW)
        gv = r.classes["gvisor_lightweight"]
        assert gv.delta_mem_sample_count == 0
        assert gv.mean_delta_mem_peak_mb == 0.0
        # Lightest sampled class still pinned to 1.0.
        assert r.new_weights["gvisor_lightweight"] == 1.0

    def test_window_without_ring_coverage_does_not_dilute_mean(self):
        # Two paired launches, only the first inside the ring window.
        # The ring-uncovered launch must NOT count as a zero-Δmem
        # sample (would halve the mean).
        rows = [
            _row(1, self.NOW - 1000, "sandbox_launched", "c1",
                 tier="t1", tenant_budget=1.0, memory="512m"),
            _row(2, self.NOW - 980, "sandbox_killed", "c1",
                 tier="t1", reason="lifetime"),
            _row(3, self.NOW - 200, "sandbox_launched", "c2",
                 tier="t1", tenant_budget=1.0, memory="512m"),
            _row(4, self.NOW - 100, "sandbox_killed", "c2",
                 tier="t1", reason="lifetime"),
        ]
        snaps = [
            # Only covers the SECOND launch's window.
            _snap(self.NOW - 200, 4.0),
            _snap(self.NOW - 150, 6.0),
            _snap(self.NOW - 110, 5.0),
        ]
        r = cal.calibrate(rows, window_days=7, now=self.NOW,
                          host_snapshots=snaps)
        gv = r.classes["gvisor_lightweight"]
        # CPU samples: 2 (both paired); Δmem samples: 1 (only second
        # was ring-covered) — the 6-4=2 GB observation isn't averaged
        # against a phantom 0 GB from the uncovered first launch.
        assert gv.sample_count == 2
        assert gv.delta_mem_sample_count == 1
        assert gv.mean_delta_mem_peak_mb == pytest.approx(2.0 * 1024.0)


class TestNormalisationWithMemAxis:
    """Weight derivation: mem axis takes max() with cpu axis when both
    are available, falls back gracefully when mem signal is missing."""

    NOW = 1_700_000_000.0

    def test_mem_dominated_class_gets_up_weighted(self):
        # A class with low CPU×time but big Δmem peak should be
        # weighted by its memory pressure, not its CPU footprint —
        # because that's the resource that binds AIMD admission.
        rows = [
            # gvisor: 1 token x 20s = 20 cpu_token_s, +1 GB mem.
            _row(1, self.NOW - 1000, "sandbox_launched", "lt",
                 tier="t1", tenant_budget=1.0, memory="512m"),
            _row(2, self.NOW - 980, "sandbox_killed", "lt",
                 tier="t1", reason="lifetime"),
            # local_compile: 4 tokens x 100s = 400 cpu_token_s,
            # +8 GB Δmem (eats most of the host RAM headroom).
            _row(3, self.NOW - 900, "sandbox_launched", "cc",
                 tier="t3-local", tenant_budget=4.0, memory="2048m"),
            _row(4, self.NOW - 800, "sandbox_killed", "cc",
                 tier="t3-local", reason="lifetime"),
        ]
        snaps = [
            # gvisor window: 4.0 → 5.0 GB (Δ = 1024 MB)
            _snap(self.NOW - 1000, 4.0),
            _snap(self.NOW - 990, 5.0),
            _snap(self.NOW - 980, 4.8),
            # compile window: 4.0 → 12.0 GB (Δ = 8192 MB)
            _snap(self.NOW - 900, 4.0),
            _snap(self.NOW - 850, 12.0),
            _snap(self.NOW - 800, 11.5),
        ]
        r = cal.calibrate(rows, window_days=7, now=self.NOW,
                          host_snapshots=snaps)
        # Reference is gvisor (lightest mean_cpu_token_s).
        assert r.new_weights["gvisor_lightweight"] == 1.0
        # cpu_score for compile = 400 / 20 = 20.0
        # mem_score for compile = 8192 / 1024 = 8.0
        # max() picks 20.0 — CPU wins here.
        assert r.new_weights["phase64c_local_compile"] == pytest.approx(20.0)

    def test_mem_axis_wins_when_cpu_score_is_lower(self):
        # Construct a case where mem_score > cpu_score so the mem
        # axis is the deciding factor (proves max() actually fires).
        # gvisor: 1 token x 200s = 200 cpu_token_s, +1 GB mem.
        # compile: 4 tokens x 100s = 400 cpu_token_s, +20 GB mem.
        # cpu ratio = 2.0; mem ratio = 20.0 → mem wins.
        rows = [
            _row(1, self.NOW - 1000, "sandbox_launched", "lt",
                 tier="t1", tenant_budget=1.0, memory="512m"),
            _row(2, self.NOW - 800, "sandbox_killed", "lt",
                 tier="t1", reason="lifetime"),
            _row(3, self.NOW - 700, "sandbox_launched", "cc",
                 tier="t3-local", tenant_budget=4.0, memory="2048m"),
            _row(4, self.NOW - 600, "sandbox_killed", "cc",
                 tier="t3-local", reason="lifetime"),
        ]
        snaps = [
            # gvisor window: 4.0 → 5.0 GB (Δ = 1024 MB)
            _snap(self.NOW - 1000, 4.0),
            _snap(self.NOW - 900, 5.0),
            _snap(self.NOW - 800, 4.8),
            # compile window: 4.0 → 24.0 GB (Δ = 20480 MB)
            _snap(self.NOW - 700, 4.0),
            _snap(self.NOW - 650, 24.0),
            _snap(self.NOW - 600, 23.5),
        ]
        r = cal.calibrate(rows, window_days=7, now=self.NOW,
                          host_snapshots=snaps)
        assert r.new_weights["gvisor_lightweight"] == 1.0
        # max(cpu=2.0, mem=20.0) = 20.0
        assert r.new_weights["phase64c_local_compile"] == pytest.approx(20.0)

    def test_mem_axis_dropped_when_reference_class_has_no_signal(self):
        # Reference class (gvisor) has no host_snapshots coverage —
        # mem axis must drop entirely (else divide-by-zero); CPU-only
        # path takes over.
        rows = [
            _row(1, self.NOW - 1000, "sandbox_launched", "lt",
                 tier="t1", tenant_budget=1.0, memory="512m"),
            _row(2, self.NOW - 980, "sandbox_killed", "lt",
                 tier="t1", reason="lifetime"),
            _row(3, self.NOW - 900, "sandbox_launched", "cc",
                 tier="t3-local", tenant_budget=4.0, memory="2048m"),
            _row(4, self.NOW - 800, "sandbox_killed", "cc",
                 tier="t3-local", reason="lifetime"),
        ]
        snaps = [
            # Only covers the compile window, not gvisor's.
            _snap(self.NOW - 900, 4.0),
            _snap(self.NOW - 850, 12.0),
        ]
        r = cal.calibrate(rows, window_days=7, now=self.NOW,
                          host_snapshots=snaps)
        # Falls back to CPU-only normalisation (400 / 20 = 20.0).
        assert r.new_weights["gvisor_lightweight"] == 1.0
        assert r.new_weights["phase64c_local_compile"] == pytest.approx(20.0)


class TestRenderersWithDeltaMem:
    """Renderers must surface Δmem_peak so operators see why a class
    got re-weighted (CPU vs mem axis)."""

    NOW = 1_700_000_000.0

    def _result_with_delta_mem(self):
        rows = [
            _row(1, self.NOW - 200, "sandbox_launched", "c1",
                 tier="t1", tenant_budget=1.0, memory="512m"),
            _row(2, self.NOW - 100, "sandbox_killed", "c1",
                 tier="t1", reason="lifetime"),
        ]
        snaps = [
            _snap(self.NOW - 200, 4.0),
            _snap(self.NOW - 150, 5.5),
            _snap(self.NOW - 110, 4.8),
        ]
        return cal.calibrate(rows, window_days=7, now=self.NOW,
                             host_snapshots=snaps)

    def test_text_renderer_includes_mean_delta_mem_column(self):
        text = cal.render_text(self._result_with_delta_mem())
        # Header column appears.
        assert "Mean Δmem (MB)" in text
        # Numeric value (1.5 GB → 1536 MB) renders.
        assert "1536" in text

    def test_text_renderer_shows_dash_for_classes_without_signal(self):
        # gvisor sampled with mem; compile sampled but no ring coverage
        # → compile shows "—" in the Δmem column.
        rows = [
            _row(1, self.NOW - 200, "sandbox_launched", "c1",
                 tier="t1", tenant_budget=1.0, memory="512m"),
            _row(2, self.NOW - 100, "sandbox_killed", "c1",
                 tier="t1", reason="lifetime"),
            _row(3, self.NOW - 1000, "sandbox_launched", "c2",
                 tier="t3-local", tenant_budget=4.0, memory="2048m"),
            _row(4, self.NOW - 900, "sandbox_killed", "c2",
                 tier="t3-local", reason="lifetime"),
        ]
        snaps = [
            _snap(self.NOW - 200, 4.0),
            _snap(self.NOW - 150, 5.5),
        ]
        r = cal.calibrate(rows, window_days=7, now=self.NOW,
                          host_snapshots=snaps)
        text = cal.render_text(r)
        # Find the compile row by name and assert its Δmem cell is "—".
        compile_line = [
            ln for ln in text.splitlines()
            if "phase64c_local_compile" in ln
        ]
        assert compile_line, "compile row missing from text render"
        # 11 columns (Class .. OOMs) — Δmem is the 9th cell.
        cells = [c.strip() for c in compile_line[0].split("|")[1:-1]]
        assert cells[8] == "—"

    def test_json_renderer_exposes_mean_delta_mem_peak(self):
        payload = json.loads(cal.render_json(self._result_with_delta_mem()))
        gv = payload["classes"]["gvisor_lightweight"]
        assert gv["mean_delta_mem_peak_mb"] == pytest.approx(1536.0)
        assert gv["delta_mem_sample_count"] == 1

    def test_yaml_renderer_writes_mean_delta_mem_peak(self):
        yaml_text = cal.render_yaml(self._result_with_delta_mem())
        # The sampled class records its observation count and mean Δ.
        assert "mean_delta_mem_peak_mb: 1536.0" in yaml_text
        assert "delta_mem_sample_count: 1" in yaml_text


class TestHostRingSnapshotsWiring:
    """CLI helper duck-types host_metrics — verify it tolerates a
    missing backend module without raising (operator running the
    script on a machine without the backend pip-installed)."""

    def test_returns_empty_when_backend_unimportable(self, monkeypatch):
        # Force the import to fail; helper must return [] not raise.
        import builtins
        real_import = builtins.__import__

        def _broken_import(name, *args, **kwargs):
            if name == "backend" or name.startswith("backend."):
                raise ModuleNotFoundError("simulated missing backend")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _broken_import)
        assert cal.host_ring_snapshots() == []
        assert cal.host_ring_depth() == 0


# ─────────────────────────────────────────────────────────────────────
#  H4b row 2590 — Human-review diff report
#
#  Adds a Summary section, per-class Status tag, drift-magnitude sort,
#  Recommendation footer, and persisted-yaml baseline comparison so the
#  operator can decide APPLY / REVIEW / SKIP at a glance.
# ─────────────────────────────────────────────────────────────────────

class TestDriftPctHelper:
    """Pure helper — pinning the boundary cases callers depend on."""

    def test_zero_to_zero_returns_zero(self):
        assert cal.drift_pct(0.0, 0.0) == 0.0

    def test_signed_relative_change(self):
        assert cal.drift_pct(2.0, 3.0) == pytest.approx(0.5)
        assert cal.drift_pct(2.0, 1.0) == pytest.approx(-0.5)

    def test_protects_against_divide_by_zero(self):
        # Operator hand-edited the yaml to 0 — calibrator must not raise.
        result = cal.drift_pct(0.0, 1.0)
        assert result > 0.0  # finite, large; not inf or NaN

    def test_negative_baseline_uses_absolute_value(self):
        # Defensive — negative weights are nonsensical but the helper
        # must still produce a sane number rather than blowing the sign.
        assert cal.drift_pct(-2.0, -1.0) == pytest.approx(0.5)


class TestClassifyDrift:
    """The five status buckets — operator-facing sanity check."""

    def _stats(self, samples: int) -> cal.ClassStats:
        return cal.ClassStats(name="x", tier="t1", old_tokens=1.0,
                              sample_count=samples)

    def test_no_data_when_zero_samples(self):
        # Even a 1000% drift on zero samples is meaningless.
        s = self._stats(0)
        assert cal.classify_drift(s, 1.0, 100.0) == cal.STATUS_NO_DATA

    def test_low_data_below_confidence_floor(self):
        s = self._stats(cal.MIN_SAMPLES_FOR_CONFIDENCE - 1)
        # Even if drift is huge, low samples win — noise not signal.
        assert cal.classify_drift(s, 1.0, 5.0) == cal.STATUS_LOW_DATA

    def test_large_drift_with_sufficient_samples(self):
        s = self._stats(cal.MIN_SAMPLES_FOR_CONFIDENCE)
        # 100% drift is well above LARGE_DRIFT_PCT (50%).
        assert cal.classify_drift(s, 1.0, 2.0) == cal.STATUS_LARGE

    def test_review_drift(self):
        s = self._stats(cal.MIN_SAMPLES_FOR_CONFIDENCE)
        # 20% drift → above MODERATE (10%), below LARGE (50%).
        assert cal.classify_drift(s, 1.0, 1.2) == cal.STATUS_REVIEW

    def test_ok_within_noise_floor(self):
        s = self._stats(cal.MIN_SAMPLES_FOR_CONFIDENCE)
        # 5% drift — below MODERATE_DRIFT_PCT.
        assert cal.classify_drift(s, 1.0, 1.05) == cal.STATUS_OK

    def test_low_data_gate_fires_before_drift_gate(self):
        # 200% drift but only 1 sample → LOW-DATA, not LARGE. This
        # ordering is the contract that prevents 1-sample noise from
        # panicking reviewers into rejecting a calibration.
        s = self._stats(1)
        assert cal.classify_drift(s, 1.0, 3.0) == cal.STATUS_LOW_DATA

    def test_threshold_boundary_is_inclusive(self):
        # Exactly LARGE_DRIFT_PCT → LARGE (>=, not >).
        s = self._stats(cal.MIN_SAMPLES_FOR_CONFIDENCE)
        old, new = 1.0, 1.0 + cal.LARGE_DRIFT_PCT
        assert cal.classify_drift(s, old, new) == cal.STATUS_LARGE


class TestSummariseCalibration:
    """Bucket counts drive the report's Summary section."""

    NOW = 1_700_000_000.0

    def _build_result_with_mixed_classes(self):
        """Construct a result with: 1 LARGE, 1 OK, 3 NO-DATA classes."""
        rows = []
        # gvisor: 5 samples, 1 token x 20s each = 1.0 ref → OK
        for i in range(5):
            rows.append(_row(2 * i + 1, self.NOW - 1000 + i * 5,
                             "sandbox_launched", f"lt{i}",
                             tier="t1", tenant_budget=1.0, memory="512m"))
            rows.append(_row(2 * i + 2, self.NOW - 980 + i * 5,
                             "sandbox_killed", f"lt{i}",
                             tier="t1", reason="lifetime"))
        # local_compile: 5 samples, 4 tokens x 100s each → 20.0 (LARGE
        # vs H4a hardcode of 4.0; +400% drift)
        for i in range(5):
            rows.append(_row(100 + 2 * i, self.NOW - 500 + i * 100,
                             "sandbox_launched", f"cc{i}",
                             tier="t3-local", tenant_budget=4.0,
                             memory="2048m"))
            rows.append(_row(101 + 2 * i, self.NOW - 400 + i * 100,
                             "sandbox_killed", f"cc{i}",
                             tier="t3-local", reason="lifetime"))
        return cal.calibrate(rows, window_days=7, now=self.NOW)

    def test_buckets_classes_correctly(self):
        r = self._build_result_with_mixed_classes()
        counts = cal.summarise_calibration(r)
        # 5 H4a classes total. gvisor → OK (==1.0 ref), compile →
        # LARGE (+400%), 3 unsampled → NO-DATA.
        assert counts[cal.STATUS_OK] >= 1
        assert counts[cal.STATUS_LARGE] >= 1
        assert counts[cal.STATUS_NO_DATA] == 3
        # Total must equal the H4a class count — every class accounted
        # for, no double-counting.
        assert sum(counts.values()) == len(r.classes)

    def test_returns_all_status_keys_even_when_zero(self):
        # Empty result — no paired launches → every class is NO-DATA.
        empty = cal.calibrate([], window_days=7, now=self.NOW)
        counts = cal.summarise_calibration(empty)
        for key in (cal.STATUS_OK, cal.STATUS_REVIEW, cal.STATUS_LARGE,
                    cal.STATUS_LOW_DATA, cal.STATUS_NO_DATA):
            assert key in counts


class TestRecommendAction:
    """The verdict heuristic — drives the Recommendation footer."""

    NOW = 1_700_000_000.0

    def test_skip_when_no_paired_launches(self):
        empty = cal.calibrate([], window_days=7, now=self.NOW)
        verdict, reason = cal.recommend_action(empty)
        assert verdict == cal.VERDICT_SKIP
        assert "no paired" in reason.lower()

    def test_review_when_any_class_has_large_drift(self):
        # 5 samples × big tokens → LARGE drift vs H4a hardcode.
        rows = []
        for i in range(5):
            rows.append(_row(100 + 2 * i, self.NOW - 500 + i * 50,
                             "sandbox_launched", f"cc{i}",
                             tier="t3-local", tenant_budget=4.0,
                             memory="2048m"))
            rows.append(_row(101 + 2 * i, self.NOW - 400 + i * 50,
                             "sandbox_killed", f"cc{i}",
                             tier="t3-local", reason="lifetime"))
        r = cal.calibrate(rows, window_days=7, now=self.NOW)
        verdict, reason = cal.recommend_action(r)
        assert verdict == cal.VERDICT_REVIEW
        assert "drift" in reason.lower() or "%" in reason

    def test_review_when_low_data_class_present(self):
        # 1 paired launch — below MIN_SAMPLES_FOR_CONFIDENCE.
        rows = [
            _row(1, self.NOW - 100, "sandbox_launched", "c1",
                 tier="t1", tenant_budget=1.0, memory="512m"),
            _row(2, self.NOW - 60, "sandbox_killed", "c1",
                 tier="t1", reason="lifetime"),
        ]
        r = cal.calibrate(rows, window_days=7, now=self.NOW)
        verdict, _reason = cal.recommend_action(r)
        assert verdict == cal.VERDICT_REVIEW

    def test_apply_when_all_classes_within_noise(self):
        # 5 samples per class, every class lands within 10% of its
        # H4a default. Use gvisor only — its mean cpu_token_s pins it
        # to 1.0 (= old_tokens), zero drift.
        rows = []
        for i in range(5):
            rows.append(_row(2 * i + 1, self.NOW - 500 + i * 50,
                             "sandbox_launched", f"lt{i}",
                             tier="t1", tenant_budget=1.0, memory="512m"))
            rows.append(_row(2 * i + 2, self.NOW - 480 + i * 50,
                             "sandbox_killed", f"lt{i}",
                             tier="t1", reason="lifetime"))
        r = cal.calibrate(rows, window_days=7, now=self.NOW)
        # gvisor: OK; everything else NO-DATA (untouched). No LARGE,
        # no LOW-DATA, no REVIEW → APPLY.
        verdict, _reason = cal.recommend_action(r)
        assert verdict == cal.VERDICT_APPLY


class TestRenderTextReportLayout:
    """The full text report — Summary / Status column / Recommendation
    block — is the actual deliverable for human review."""

    NOW = 1_700_000_000.0

    def _calibrate(self, rows):
        return cal.calibrate(rows, window_days=7, now=self.NOW)

    def test_summary_section_lists_bucket_counts(self):
        empty = self._calibrate([])
        text = cal.render_text(empty)
        assert "## Summary" in text
        # All five status keys appear in the bucket-count line.
        for tag in (cal.STATUS_OK, cal.STATUS_REVIEW, cal.STATUS_LARGE,
                    cal.STATUS_LOW_DATA, cal.STATUS_NO_DATA):
            assert tag in text

    def test_recommendation_line_renders(self):
        empty = self._calibrate([])
        text = cal.render_text(empty)
        # Verdict + reason appear in the Summary block.
        assert "Recommendation" in text
        assert cal.VERDICT_SKIP in text  # empty input → SKIP

    def test_baseline_source_line_renders(self):
        # Default baseline (no yaml) → H4a hardcode label.
        empty = self._calibrate([])
        text = cal.render_text(empty)
        assert "Comparison baseline" in text
        assert "H4a hardcode" in text

    def test_status_column_present_in_table_header(self):
        rows = [
            _row(1, self.NOW - 100, "sandbox_launched", "c1",
                 tier="t1", tenant_budget=1.0, memory="512m"),
            _row(2, self.NOW - 60, "sandbox_killed", "c1",
                 tier="t1", reason="lifetime"),
        ]
        text = cal.render_text(self._calibrate(rows))
        # Status is the 12th column — appended after OOMs.
        header_lines = [ln for ln in text.splitlines() if "Class" in ln
                        and "Tier" in ln]
        assert header_lines, "table header missing"
        assert "Status" in header_lines[0]

    def test_table_rows_carry_status_tag(self):
        # 1 paired launch → LOW-DATA on gvisor row.
        rows = [
            _row(1, self.NOW - 100, "sandbox_launched", "c1",
                 tier="t1", tenant_budget=1.0, memory="512m"),
            _row(2, self.NOW - 60, "sandbox_killed", "c1",
                 tier="t1", reason="lifetime"),
        ]
        text = cal.render_text(self._calibrate(rows))
        gv_lines = [ln for ln in text.splitlines()
                    if "gvisor_lightweight" in ln and "|" in ln]
        assert gv_lines, "gvisor row missing"
        # 12 columns now — Status is the last cell before trailing |.
        cells = [c.strip() for c in gv_lines[0].split("|")[1:-1]]
        assert len(cells) == 12
        assert cells[-1] == cal.STATUS_LOW_DATA

    def test_table_sorted_by_drift_magnitude_descending(self):
        # Build a result with: gvisor (5 samples, OK) + compile
        # (5 samples, LARGE drift). LARGE row must come BEFORE the
        # OK row even though alphabetical order would put gvisor
        # (which starts with 'g') before nothing useful — actually
        # alphabetically 'd' < 'g' < 'p', so this test pins the
        # *drift-first* order against the alphabetical fallback.
        rows = []
        for i in range(5):
            rows.append(_row(2 * i + 1, self.NOW - 1000 + i * 5,
                             "sandbox_launched", f"lt{i}",
                             tier="t1", tenant_budget=1.0, memory="512m"))
            rows.append(_row(2 * i + 2, self.NOW - 980 + i * 5,
                             "sandbox_killed", f"lt{i}",
                             tier="t1", reason="lifetime"))
        for i in range(5):
            rows.append(_row(100 + 2 * i, self.NOW - 500 + i * 100,
                             "sandbox_launched", f"cc{i}",
                             tier="t3-local", tenant_budget=4.0,
                             memory="2048m"))
            rows.append(_row(101 + 2 * i, self.NOW - 400 + i * 100,
                             "sandbox_killed", f"cc{i}",
                             tier="t3-local", reason="lifetime"))
        text = cal.render_text(self._calibrate(rows))
        compile_idx = text.find("phase64c_local_compile")
        gvisor_idx = text.find("gvisor_lightweight")
        assert compile_idx > 0 and gvisor_idx > 0
        # phase64c_local_compile (LARGE drift) renders before gvisor (OK).
        assert compile_idx < gvisor_idx, (
            "LARGE-drift row should sort above OK-drift row"
        )


class TestRenderJsonAddsReviewSurface:
    NOW = 1_700_000_000.0

    def test_json_exposes_summary_recommendation_and_baseline_source(self):
        rows = [
            _row(1, self.NOW - 100, "sandbox_launched", "c1",
                 tier="t1", tenant_budget=1.0, memory="512m"),
            _row(2, self.NOW - 60, "sandbox_killed", "c1",
                 tier="t1", reason="lifetime"),
        ]
        r = cal.calibrate(rows, window_days=7, now=self.NOW)
        payload = json.loads(cal.render_json(r))
        assert "summary" in payload
        assert "recommendation" in payload
        assert payload["recommendation"]["verdict"] in (
            cal.VERDICT_APPLY, cal.VERDICT_REVIEW, cal.VERDICT_SKIP,
        )
        assert "reason" in payload["recommendation"]
        assert "baseline_source" in payload
        # Summary keys mirror the STATUS_* constants.
        for tag in (cal.STATUS_OK, cal.STATUS_REVIEW, cal.STATUS_LARGE,
                    cal.STATUS_LOW_DATA, cal.STATUS_NO_DATA):
            assert tag in payload["summary"]

    def test_json_per_class_has_status_and_drift_pct(self):
        rows = [
            _row(1, self.NOW - 100, "sandbox_launched", "c1",
                 tier="t1", tenant_budget=1.0, memory="512m"),
            _row(2, self.NOW - 60, "sandbox_killed", "c1",
                 tier="t1", reason="lifetime"),
        ]
        r = cal.calibrate(rows, window_days=7, now=self.NOW)
        payload = json.loads(cal.render_json(r))
        gv = payload["classes"]["gvisor_lightweight"]
        assert "status" in gv
        assert gv["status"] == cal.STATUS_LOW_DATA  # 1 sample
        assert "drift_pct" in gv
        # gvisor pins to 1.0 baseline — drift is 0 against H4a.
        assert gv["drift_pct"] == pytest.approx(0.0)
        # Backwards-compat: baseline_tokens added; old_tokens preserved.
        assert "baseline_tokens" in gv
        assert "old_tokens" in gv


class TestLoadBaselineWeights:
    """Persisted-yaml load — the actual operator-review baseline."""

    def test_returns_none_when_file_missing(self, tmp_path):
        missing = tmp_path / "no_such.yaml"
        weights, source = cal.load_baseline_weights(missing)
        assert weights is None
        assert "H4a hardcode" in source
        assert "no_such.yaml" in source

    def test_loads_via_pyyaml_when_available(self, tmp_path):
        pytest.importorskip("yaml")
        path = tmp_path / "weights.yaml"
        result = cal.calibrate([], window_days=7, now=1_700_000_000.0)
        cal.write_yaml(result, path)
        weights, source = cal.load_baseline_weights(path)
        # All H4a classes round-trip through render_yaml → load.
        assert weights is not None
        assert "gvisor_lightweight" in weights
        assert weights["gvisor_lightweight"] == pytest.approx(1.0)
        assert str(path) in source

    def test_scanner_fallback_parses_handwritten_yaml(self, tmp_path):
        # Hand-craft a minimal yaml that matches our writer's shape but
        # bypass PyYAML — tests the fallback parser.
        path = tmp_path / "weights.yaml"
        path.write_text(
            "# operator notes\n"
            "weights:\n"
            "  gvisor_lightweight:\n"
            "    tokens: 0.8\n"
            "    memory_mb: 512\n"
            "  phase64c_local_compile:\n"
            "    tokens: 7.5\n"
            "    memory_mb: 2048\n"
            "trailing_top_level_key: ignored\n",
            encoding="utf-8",
        )
        # Force scanner path even when PyYAML is installed.
        weights = cal._parse_weights_via_scanner(path.read_text())
        assert weights == {
            "gvisor_lightweight": pytest.approx(0.8),
            "phase64c_local_compile": pytest.approx(7.5),
        }

    def test_returns_none_on_corrupt_file(self, tmp_path):
        path = tmp_path / "weights.yaml"
        # Garbage that yields no parseable weights either way.
        path.write_text("not a yaml at all :::\n", encoding="utf-8")
        weights, source = cal.load_baseline_weights(path)
        assert weights is None
        assert "H4a hardcode" in source

    def test_baseline_for_prefers_loaded_weights(self):
        rows = [
            _row(1, 1_700_000_000.0 - 100, "sandbox_launched", "c1",
                 tier="t1", tenant_budget=1.0, memory="512m"),
            _row(2, 1_700_000_000.0 - 60, "sandbox_killed", "c1",
                 tier="t1", reason="lifetime"),
        ]
        r = cal.calibrate(rows, window_days=7, now=1_700_000_000.0)
        # Override the baseline as if we loaded it from yaml.
        r.baseline_weights = {"gvisor_lightweight": 0.7}
        # baseline_for routes through the yaml when present.
        assert cal.baseline_for("gvisor_lightweight", r) == pytest.approx(0.7)
        # Class missing from yaml → fall back to H4a hardcode (4.0).
        assert cal.baseline_for("phase64c_local_compile", r) == pytest.approx(4.0)


class TestDiffReportComparesAgainstPersistedBaseline:
    """End-to-end: calibrator + loaded yaml baseline produces the
    correct status tags + recommendation."""

    NOW = 1_700_000_000.0

    def _calibrate_with_compile_drift(self):
        rows = []
        for i in range(5):
            rows.append(_row(2 * i + 1, self.NOW - 1000 + i * 5,
                             "sandbox_launched", f"lt{i}",
                             tier="t1", tenant_budget=1.0, memory="512m"))
            rows.append(_row(2 * i + 2, self.NOW - 980 + i * 5,
                             "sandbox_killed", f"lt{i}",
                             tier="t1", reason="lifetime"))
        for i in range(5):
            rows.append(_row(100 + 2 * i, self.NOW - 500 + i * 100,
                             "sandbox_launched", f"cc{i}",
                             tier="t3-local", tenant_budget=4.0,
                             memory="2048m"))
            rows.append(_row(101 + 2 * i, self.NOW - 400 + i * 100,
                             "sandbox_killed", f"cc{i}",
                             tier="t3-local", reason="lifetime"))
        return cal.calibrate(rows, window_days=7, now=self.NOW)

    def test_status_changes_when_baseline_yaml_already_close_to_new(self):
        r = self._calibrate_with_compile_drift()
        # New compile weight = 20.0 (5×400/5×20 ratio). Without
        # baseline override, this is LARGE vs H4a's 4.0.
        assert cal.classify_drift(
            r.classes["phase64c_local_compile"],
            r.classes["phase64c_local_compile"].old_tokens,
            r.new_weights["phase64c_local_compile"],
        ) == cal.STATUS_LARGE
        # Now pretend the yaml already converged to 19.5 — calibration
        # produces 20.0, which is 2.5% drift → OK status (well below
        # MODERATE_DRIFT_PCT). This is the critical "after first apply"
        # behaviour — re-running calibration shouldn't keep flagging
        # already-applied weights as LARGE.
        r.baseline_weights = {"phase64c_local_compile": 19.5}
        text = cal.render_text(r)
        compile_lines = [ln for ln in text.splitlines()
                         if "phase64c_local_compile" in ln and "|" in ln]
        cells = [c.strip() for c in compile_lines[0].split("|")[1:-1]]
        assert cells[-1] == cal.STATUS_OK

    def test_baseline_source_propagates_into_text_report(self):
        r = self._calibrate_with_compile_drift()
        r.baseline_source = "configs/sandbox_cost_weights.yaml (last calibrated 2026-04-25)"
        text = cal.render_text(r)
        assert "configs/sandbox_cost_weights.yaml" in text
        assert "2026-04-25" in text


class TestCliBaselineWiring:
    """The CLI flag plumbing — ``--baseline`` / ``--no-baseline``."""

    def test_parser_accepts_baseline_flag(self):
        p = cal.build_parser()
        ns = p.parse_args(["--baseline", "/tmp/foo.yaml"])
        assert ns.baseline == Path("/tmp/foo.yaml")

    def test_parser_accepts_no_baseline_flag(self):
        p = cal.build_parser()
        ns = p.parse_args(["--no-baseline"])
        assert ns.no_baseline is True

    def test_baseline_defaults_to_none(self):
        p = cal.build_parser()
        ns = p.parse_args([])
        assert ns.baseline is None
        assert ns.no_baseline is False


# ─────────────────────────────────────────────────────────────────────
#  H4b row 2592 — Audit hash-chain row contract
# ─────────────────────────────────────────────────────────────────────
#
# The chain row is the immutable receipt of every ``--apply`` run. The
# tests below pin the canonical payload schema (entity_kind / entity_id
# / before / after / actor) so downstream queriers
# (``audit.query(action="sandbox_cost_calibration")``) have a stable
# contract — and so a future renderer change to the diff report can't
# silently widen / narrow the audit row without triggering a test diff.
#
# We exercise both the pure-function payload builder
# (``build_audit_payload``) AND the async wrapper (``emit_audit_row``)
# via a monkeypatched ``audit.log`` spy — the spy approach lets us
# prove the wrapper passes the right kwargs, returns True/False
# correctly, and survives audit-side failures, without standing up an
# asyncpg pool. The pure-function tests pin the schema; the spy tests
# pin the wiring.


def _build_calibrated_result(now: float = 1_700_000_000.0) -> cal.CalibrationResult:
    """Build a CalibrationResult with one drifted + one zero-data class.

    Uses the same shape as TestRecommendAction's helpers — a
    lightweight class with ample samples (reference, pinned to 1.0),
    plus a heavier compile class with enough samples to clear
    MIN_SAMPLES_FOR_CONFIDENCE so the drift status surfaces.
    """
    rows: list[cal.AuditRow] = []
    # 6 lightweight launches (1 token × 20s = 20 cpu_token_s each).
    for i in range(6):
        rows.append(_row(2 * i + 1, now - 1000 + i, "sandbox_launched", f"lt{i}",
                         tier="t1", tenant_budget=1.0, memory="512m"))
        rows.append(_row(2 * i + 2, now - 980 + i, "sandbox_killed", f"lt{i}",
                         tier="t1", reason="lifetime"))
    # 6 compile launches (4 tokens × 100s = 400 cpu_token_s each).
    for i in range(6):
        rows.append(_row(100 + 2 * i + 1, now - 900 + i, "sandbox_launched",
                         f"cc{i}", tier="t3-local", tenant_budget=4.0,
                         memory="2048m"))
        rows.append(_row(100 + 2 * i + 2, now - 800 + i, "sandbox_killed",
                         f"cc{i}", tier="t3-local", reason="lifetime"))
    return cal.calibrate(rows, window_days=7, now=now)


class TestBuildAuditPayload:
    """Pure-function schema lock — no DB / no monkeypatch needed."""

    OUT = Path("/tmp/sandbox_cost_weights.yaml")

    def test_before_records_baseline_weights_for_every_class(self):
        result = _build_calibrated_result()
        before, _ = cal.build_audit_payload(result, self.OUT)
        # Every canonical class must have a baseline entry — the chain
        # row's `before.weights` is what downstream consumers read to
        # know the pre-apply state, so missing entries silently break
        # the diff query.
        for name in result.classes:
            assert name in before["weights"]
            assert isinstance(before["weights"][name], float)

    def test_before_baseline_source_propagates_from_result(self):
        result = _build_calibrated_result()
        result.baseline_source = "configs/sandbox_cost_weights.yaml " \
                                 "(last calibrated 2026-04-25T00:00:00+00:00)"
        before, _ = cal.build_audit_payload(result, self.OUT)
        assert before["baseline_source"] == result.baseline_source

    def test_before_records_config_path(self):
        result = _build_calibrated_result()
        before, after = cal.build_audit_payload(result, self.OUT)
        assert before["config_path"] == str(self.OUT)
        assert after["config_path"] == str(self.OUT)

    def test_after_records_post_apply_weights(self):
        result = _build_calibrated_result()
        _, after = cal.build_audit_payload(result, self.OUT)
        # gvisor pinned to 1.0 (lightest reference); compile drifts
        # 4.0 → 20.0 (5× the reference's mean cpu_token_s).
        assert after["weights"]["gvisor_lightweight"] == pytest.approx(1.0)
        assert after["weights"]["phase64c_local_compile"] == pytest.approx(20.0)

    def test_after_records_window_and_observation_counts(self):
        result = _build_calibrated_result()
        _, after = cal.build_audit_payload(result, self.OUT)
        assert after["window_days"] == 7
        assert after["window_start_ts"] == result.window_start_ts
        assert after["window_end_ts"] == result.window_end_ts
        assert after["total_paired"] == 12
        assert after["total_orphaned"] == 0
        assert after["host_ring_size"] == result.host_ring_size

    def test_after_weights_detail_includes_drift_and_status(self):
        result = _build_calibrated_result()
        _, after = cal.build_audit_payload(result, self.OUT)
        # Compile is drifted (4 → 20 = 400% drift) AND has 6 samples
        # → must appear with full drill-down fields.
        d = after["weights_detail"]["phase64c_local_compile"]
        assert d["old_tokens"] == pytest.approx(4.0)
        assert d["new_tokens"] == pytest.approx(20.0)
        assert d["sample_count"] == 6
        assert d["status"] == cal.STATUS_LARGE
        assert d["drift_pct"] == pytest.approx(4.0)  # (20-4)/4
        # Mean-CPU·s recorded per class so audit query can rebuild
        # the calibration math without re-fetching audit_log rows.
        assert d["mean_cpu_token_s"] == pytest.approx(400.0)

    def test_after_weights_detail_omits_unsampled_unchanged_classes(self):
        # qemu / ssh_remote weren't sampled and stay at H4a values →
        # neither evidence nor delta → drop from detail to keep the
        # row size sub-5KB even with future per-class field growth.
        result = _build_calibrated_result()
        _, after = cal.build_audit_payload(result, self.OUT)
        assert "phase64c_qemu_aarch64" not in after["weights_detail"]
        assert "phase64c_ssh_remote" not in after["weights_detail"]

    def test_after_includes_summary_buckets(self):
        result = _build_calibrated_result()
        _, after = cal.build_audit_payload(result, self.OUT)
        # summary surface must always include all 5 status keys so
        # downstream consumers can read positionally.
        for status in (cal.STATUS_OK, cal.STATUS_REVIEW, cal.STATUS_LARGE,
                       cal.STATUS_LOW_DATA, cal.STATUS_NO_DATA):
            assert status in after["summary"]
        # Compile drift is LARGE; gvisor is OK (pinned ref); the 3
        # unsampled classes are NO-DATA.
        assert after["summary"][cal.STATUS_LARGE] == 1
        assert after["summary"][cal.STATUS_OK] == 1
        assert after["summary"][cal.STATUS_NO_DATA] == 3

    def test_after_includes_recommendation_verdict(self):
        result = _build_calibrated_result()
        _, after = cal.build_audit_payload(result, self.OUT)
        rec = after["recommendation"]
        assert rec["verdict"] == cal.VERDICT_REVIEW
        assert isinstance(rec["reason"], str) and rec["reason"]

    def test_payload_is_json_serialisable(self):
        # The audit module canonicalises to JSON for the chain hash.
        # If a non-JSON-safe field (Path, set, dataclass, datetime)
        # ever leaks in, the chain write would silently fall back to
        # str() and corrupt forensic queries that try to round-trip.
        result = _build_calibrated_result()
        before, after = cal.build_audit_payload(result, self.OUT)
        json.dumps(before)
        json.dumps(after)

    def test_baseline_yaml_overrides_old_tokens_in_before(self):
        # Post-first-apply: yaml is now the source of truth → the
        # chain row's `before.weights` must reflect the yaml (not the
        # H4a hardcode) so the next calibration re-run records the
        # actual pre-apply state, not a constant.
        result = _build_calibrated_result()
        result.baseline_weights = {"phase64c_local_compile": 19.5}
        before, after = cal.build_audit_payload(result, self.OUT)
        assert before["weights"]["phase64c_local_compile"] == pytest.approx(19.5)
        # Detail's old_tokens uses the same baseline so drift_pct is
        # measured against live yaml, not against the hardcode.
        d = after["weights_detail"]["phase64c_local_compile"]
        assert d["old_tokens"] == pytest.approx(19.5)
        # 20.0 vs 19.5 = 2.5% drift → OK status (post-first-apply
        # behaviour: re-running calibration shouldn't keep flagging
        # already-applied weights as LARGE).
        assert d["status"] == cal.STATUS_OK

    def test_empty_window_payload_still_well_formed(self):
        # No paired launches → calibration produced no actionable
        # signal (verdict SKIP). The audit row should still be
        # well-formed (CI cron may invoke `--apply` defensively even
        # when SKIP) — the chain row then proves "we ran, found
        # nothing, did nothing".
        result = cal.calibrate([], window_days=7, now=1_700_000_000.0)
        before, after = cal.build_audit_payload(result, self.OUT)
        assert before["weights"]  # all 5 H4a classes recorded
        assert after["total_paired"] == 0
        assert after["weights_detail"] == {}
        assert after["recommendation"]["verdict"] == cal.VERDICT_SKIP


def _spy_audit_log(monkeypatch, *, raise_exc: Exception | None = None):
    """Install a spy on ``backend.audit.log`` and return the capture list.

    Each entry is the kwargs dict the production code passed to
    ``audit.log``. If ``raise_exc`` is given, the spy raises after
    capturing — used to prove ``emit_audit_row`` survives audit
    failures (best-effort contract). Mirrors the spy pattern in
    ``test_workspace_discard_recreate.py`` so future readers don't
    need to learn two idioms.
    """
    from backend import audit as _audit
    captured: list[dict] = []

    async def _spy(action, entity_kind, entity_id, before=None, after=None,
                   actor="system", session_id=None, conn=None):
        captured.append({
            "action": action,
            "entity_kind": entity_kind,
            "entity_id": entity_id,
            "before": before,
            "after": after,
            "actor": actor,
            "session_id": session_id,
        })
        if raise_exc is not None:
            raise raise_exc
        return None

    monkeypatch.setattr(_audit, "log", _spy, raising=True)
    return captured


class TestEmitAuditRow:
    """Async wrapper contract — wiring + best-effort failure path."""

    OUT = Path("/tmp/sandbox_cost_weights.yaml")

    def test_emits_row_with_canonical_action_kind_entity(self, monkeypatch):
        captured = _spy_audit_log(monkeypatch)
        result = _build_calibrated_result()
        ok = asyncio.run(cal.emit_audit_row(result, self.OUT))
        assert ok is True
        assert len(captured) == 1
        row = captured[0]
        # Three canonical fields downstream forensic queries pivot on.
        assert row["action"] == "sandbox_cost_calibration"
        assert row["entity_kind"] == "config"
        assert row["entity_id"] == "sandbox_cost_weights.yaml"

    def test_emits_actor_identifies_calibrator(self, monkeypatch):
        captured = _spy_audit_log(monkeypatch)
        asyncio.run(cal.emit_audit_row(
            _build_calibrated_result(), self.OUT,
        ))
        # Actor distinguishes scripted calibration from human edits in
        # forensic queries; the "system:" prefix matches the existing
        # convention used by other background-task audit rows.
        assert captured[0]["actor"] == "system:calibrate_sandbox_cost"

    def test_payload_matches_build_audit_payload(self, monkeypatch):
        # The async wrapper must not re-shape the payload — schema
        # lives in build_audit_payload() so contract changes happen in
        # one place.
        captured = _spy_audit_log(monkeypatch)
        result = _build_calibrated_result()
        asyncio.run(cal.emit_audit_row(result, self.OUT))
        expected_before, expected_after = cal.build_audit_payload(result, self.OUT)
        assert captured[0]["before"] == expected_before
        assert captured[0]["after"] == expected_after

    def test_returns_false_when_audit_log_raises(self, monkeypatch):
        # audit.py already swallows exceptions internally (returns
        # None), but the calibrator catches anything that escapes too
        # — yaml is the operator-visible truth, the chain row is best-
        # effort. A blown audit infra must NOT block --apply.
        captured = _spy_audit_log(
            monkeypatch, raise_exc=RuntimeError("simulated audit outage"),
        )
        ok = asyncio.run(cal.emit_audit_row(
            _build_calibrated_result(), self.OUT,
        ))
        assert ok is False
        # Spy still captured the call — proves we attempted the write
        # before the failure path kicked in.
        assert len(captured) == 1

    def test_returns_false_when_audit_module_unimportable(self, monkeypatch):
        # Simulate the dev-box / fresh-install scenario where the
        # backend package isn't installed at all. The calibrator
        # should log + return False, never crash the --apply.
        import builtins
        real_import = builtins.__import__

        def _no_audit(name, globals=None, locals=None, fromlist=(), level=0):
            if name == "backend" and fromlist and "audit" in fromlist:
                raise ImportError("backend.audit not installed")
            if name == "backend.audit":
                raise ImportError("backend.audit not installed")
            return real_import(name, globals, locals, fromlist, level)

        monkeypatch.setattr(builtins, "__import__", _no_audit)
        ok = asyncio.run(cal.emit_audit_row(
            _build_calibrated_result(), self.OUT,
        ))
        assert ok is False
