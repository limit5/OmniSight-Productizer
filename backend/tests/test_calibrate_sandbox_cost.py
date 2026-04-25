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
