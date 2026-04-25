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
