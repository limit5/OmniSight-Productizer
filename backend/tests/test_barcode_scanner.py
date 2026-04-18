"""C22 — L4-CORE-22 Barcode/scanning SDK abstraction tests (#243).

Covers: unified interface, all 4 vendor adapters, symbology decode,
decode modes (HID/SPP/API), frame samples, error handling, test recipes,
artifacts, and gate validation.
"""

from __future__ import annotations


import pytest

from backend import barcode_scanner as bs


# ═══════════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════════

@pytest.fixture(autouse=True)
def _reset_config():
    bs._cfg = None
    yield
    bs._cfg = None


@pytest.fixture
def qr_frame():
    return bs._generate_synthetic_frame("qr_code", "https://omnisight.dev", 200, 200)


@pytest.fixture
def upc_a_frame():
    return bs._generate_synthetic_frame("upc_a", "012345678905", 200, 100)


@pytest.fixture
def ean_13_frame():
    return bs._generate_synthetic_frame("ean_13", "4006381333931", 200, 100)


@pytest.fixture
def code_128_frame():
    return bs._generate_synthetic_frame("code_128", "Hello World", 300, 100)


@pytest.fixture
def data_matrix_frame():
    return bs._generate_synthetic_frame("data_matrix", "SN-2026041500001", 150, 150)


@pytest.fixture
def pdf417_frame():
    return bs._generate_synthetic_frame("pdf417", "OMNISIGHT-PRODUCTIZER-C22", 300, 100)


@pytest.fixture
def aztec_frame():
    return bs._generate_synthetic_frame("aztec", "TICKET-20260415-001", 150, 150)


# ═══════════════════════════════════════════════════════════════════════
# 1. Config loading
# ═══════════════════════════════════════════════════════════════════════

class TestConfigLoading:
    def test_load_config(self):
        cfg = bs._load_config()
        assert isinstance(cfg, dict)
        assert "vendors" in cfg
        assert "symbologies" in cfg
        assert "decode_modes" in cfg
        assert "frame_samples" in cfg

    def test_config_cached(self):
        cfg1 = bs._load_config()
        cfg2 = bs._load_config()
        assert cfg1 is cfg2


# ═══════════════════════════════════════════════════════════════════════
# 2. Vendor listing & creation
# ═══════════════════════════════════════════════════════════════════════

class TestVendors:
    def test_list_vendors(self):
        vendors = bs.list_vendors()
        assert len(vendors) == 4
        ids = {v.vendor_id for v in vendors}
        assert ids == {"zebra_snapi", "honeywell", "datalogic", "newland"}

    def test_vendor_fields(self):
        vendors = bs.list_vendors()
        for v in vendors:
            assert v.name
            assert v.sdk_name
            assert len(v.transport) > 0
            assert len(v.supported_symbologies) > 0
            assert len(v.decode_modes) > 0

    def test_create_scanner_all_vendors(self):
        for vid in bs.VendorId:
            scanner = bs.create_scanner(vid.value)
            assert scanner.vendor_id == vid.value
            assert scanner.state == bs.ScannerState.disconnected

    def test_create_scanner_unknown_vendor(self):
        with pytest.raises(ValueError, match="Unknown vendor"):
            bs.create_scanner("unknown_vendor")

    def test_create_scanner_with_config(self):
        config = bs.ScannerConfig(
            vendor_id="zebra_snapi",
            decode_mode="api",
            enabled_symbologies=["qr_code", "code_128"],
        )
        scanner = bs.create_scanner("zebra_snapi", config)
        assert scanner.config.decode_mode == "api"
        assert "qr_code" in scanner.config.enabled_symbologies


# ═══════════════════════════════════════════════════════════════════════
# 3. Scanner lifecycle (per vendor)
# ═══════════════════════════════════════════════════════════════════════

class TestScannerLifecycle:
    @pytest.mark.parametrize("vendor_id", [v.value for v in bs.VendorId])
    def test_connect_disconnect(self, vendor_id):
        scanner = bs.create_scanner(vendor_id)
        assert scanner.state == bs.ScannerState.disconnected
        assert scanner.connect()
        assert scanner.state == bs.ScannerState.connected
        assert scanner.disconnect()
        assert scanner.state == bs.ScannerState.disconnected

    @pytest.mark.parametrize("vendor_id", [v.value for v in bs.VendorId])
    def test_configure(self, vendor_id):
        scanner = bs.create_scanner(vendor_id)
        scanner.connect()
        config = bs.ScannerConfig(vendor_id=vendor_id, decode_mode="api")
        assert scanner.configure(config)
        assert scanner.state == bs.ScannerState.configured

    @pytest.mark.parametrize("vendor_id", [v.value for v in bs.VendorId])
    def test_connect_twice_fails(self, vendor_id):
        scanner = bs.create_scanner(vendor_id)
        assert scanner.connect()
        assert not scanner.connect()

    @pytest.mark.parametrize("vendor_id", [v.value for v in bs.VendorId])
    def test_disconnect_when_disconnected(self, vendor_id):
        scanner = bs.create_scanner(vendor_id)
        assert not scanner.disconnect()

    @pytest.mark.parametrize("vendor_id", [v.value for v in bs.VendorId])
    def test_configure_when_disconnected(self, vendor_id):
        scanner = bs.create_scanner(vendor_id)
        config = bs.ScannerConfig(vendor_id=vendor_id)
        assert not scanner.configure(config)

    @pytest.mark.parametrize("vendor_id", [v.value for v in bs.VendorId])
    def test_get_capabilities(self, vendor_id):
        scanner = bs.create_scanner(vendor_id)
        caps = scanner.get_capabilities()
        assert "vendor" in caps
        assert caps["vendor"] == vendor_id

    @pytest.mark.parametrize("vendor_id", [v.value for v in bs.VendorId])
    def test_get_status(self, vendor_id):
        scanner = bs.create_scanner(vendor_id)
        status = scanner.get_status()
        assert status["vendor_id"] == vendor_id
        assert status["state"] == "disconnected"
        assert status["scan_count"] == 0


# ═══════════════════════════════════════════════════════════════════════
# 4. Scanning / decoding
# ═══════════════════════════════════════════════════════════════════════

class TestScanning:
    @pytest.mark.parametrize("vendor_id", [v.value for v in bs.VendorId])
    def test_scan_qr(self, vendor_id, qr_frame):
        result = bs.decode_frame(vendor_id, qr_frame)
        assert result.status == "success"
        assert result.symbology == "qr_code"
        assert result.data == "https://omnisight.dev"
        assert result.confidence > 0.0
        assert result.vendor_id == vendor_id

    @pytest.mark.parametrize("vendor_id", [v.value for v in bs.VendorId])
    def test_scan_upc_a(self, vendor_id, upc_a_frame):
        result = bs.decode_frame(vendor_id, upc_a_frame)
        assert result.status == "success"
        assert result.symbology == "upc_a"
        assert result.data == "012345678905"

    @pytest.mark.parametrize("vendor_id", [v.value for v in bs.VendorId])
    def test_scan_ean_13(self, vendor_id, ean_13_frame):
        result = bs.decode_frame(vendor_id, ean_13_frame)
        assert result.status == "success"
        assert result.data == "4006381333931"

    @pytest.mark.parametrize("vendor_id", [v.value for v in bs.VendorId])
    def test_scan_code_128(self, vendor_id, code_128_frame):
        result = bs.decode_frame(vendor_id, code_128_frame)
        assert result.status == "success"
        assert result.data == "Hello World"

    @pytest.mark.parametrize("vendor_id", [v.value for v in bs.VendorId])
    def test_scan_data_matrix(self, vendor_id, data_matrix_frame):
        result = bs.decode_frame(vendor_id, data_matrix_frame)
        assert result.status == "success"
        assert result.data == "SN-2026041500001"

    @pytest.mark.parametrize("vendor_id", [v.value for v in bs.VendorId])
    def test_scan_pdf417(self, vendor_id, pdf417_frame):
        result = bs.decode_frame(vendor_id, pdf417_frame)
        assert result.status == "success"
        assert result.data == "OMNISIGHT-PRODUCTIZER-C22"

    @pytest.mark.parametrize("vendor_id", [v.value for v in bs.VendorId])
    def test_scan_aztec(self, vendor_id, aztec_frame):
        result = bs.decode_frame(vendor_id, aztec_frame)
        assert result.status == "success"
        assert result.data == "TICKET-20260415-001"

    def test_scan_increments_count(self, qr_frame):
        config = bs.ScannerConfig(vendor_id="zebra_snapi")
        scanner = bs.create_scanner("zebra_snapi", config)
        scanner.connect()
        scanner.configure(config)
        assert scanner.scan_count == 0
        scanner.scan(qr_frame)
        assert scanner.scan_count == 1
        scanner.scan(qr_frame)
        assert scanner.scan_count == 2

    def test_scan_returns_frame_hash(self, qr_frame):
        result = bs.decode_frame("zebra_snapi", qr_frame)
        assert result.frame_hash is not None
        assert len(result.frame_hash) == 16

    def test_scan_returns_decode_time(self, qr_frame):
        result = bs.decode_frame("zebra_snapi", qr_frame)
        assert result.decode_time_ms >= 0


# ═══════════════════════════════════════════════════════════════════════
# 5. Decode modes
# ═══════════════════════════════════════════════════════════════════════

class TestDecodeModes:
    def test_list_decode_modes(self):
        modes = bs.list_decode_modes()
        assert len(modes) == 3
        ids = {m["mode_id"] for m in modes}
        assert ids == {"hid_wedge", "spp", "api"}

    def test_hid_wedge_mode(self, qr_frame):
        config = bs.ScannerConfig(
            vendor_id="zebra_snapi",
            decode_mode="hid_wedge",
            prefix="PRE:",
            suffix=":SUF",
        )
        scanner = bs.create_scanner("zebra_snapi", config)
        scanner.connect()
        scanner.configure(config)
        result = scanner.scan(qr_frame)
        assert result.status == "success"
        assert "hid_output" in result.metadata
        assert result.metadata["hid_output"] == "PRE:https://omnisight.dev:SUF"

    def test_spp_mode(self, qr_frame):
        config = bs.ScannerConfig(
            vendor_id="honeywell",
            decode_mode="spp",
            prefix="[",
            suffix="]",
        )
        scanner = bs.create_scanner("honeywell", config)
        scanner.connect()
        scanner.configure(config)
        result = scanner.scan(qr_frame)
        assert result.status == "success"
        assert "spp_output" in result.metadata
        assert result.metadata["spp_output"] == "[https://omnisight.dev]\r\n"

    def test_api_mode(self, qr_frame):
        config = bs.ScannerConfig(vendor_id="datalogic", decode_mode="api")
        scanner = bs.create_scanner("datalogic", config)
        scanner.connect()
        scanner.configure(config)
        result = scanner.scan(qr_frame)
        assert result.status == "success"
        assert "api_decode_event" in result.metadata
        evt = result.metadata["api_decode_event"]
        assert evt["symbology"] == "qr_code"
        assert evt["data"] == "https://omnisight.dev"

    def test_set_decode_mode(self):
        scanner = bs.create_scanner("zebra_snapi")
        assert scanner.set_decode_mode("hid_wedge")
        assert scanner.config.decode_mode == "hid_wedge"
        assert scanner.set_decode_mode("spp")
        assert scanner.config.decode_mode == "spp"
        assert not scanner.set_decode_mode("invalid_mode")

    def test_decode_mode_fields(self):
        modes = bs.list_decode_modes()
        for mode in modes:
            assert "mode_id" in mode
            assert "name" in mode
            assert "transport" in mode
            assert "features" in mode


# ═══════════════════════════════════════════════════════════════════════
# 6. Symbology support
# ═══════════════════════════════════════════════════════════════════════

class TestSymbologies:
    def test_list_all_symbologies(self):
        syms = bs.list_symbologies()
        assert len(syms) >= 16

    def test_list_1d_symbologies(self):
        syms = bs.list_symbologies(category="one_d")
        assert all(s["category"] == "one_d" for s in syms)
        assert len(syms) >= 10

    def test_list_2d_symbologies(self):
        syms = bs.list_symbologies(category="two_d")
        assert all(s["category"] == "two_d" for s in syms)
        assert len(syms) >= 6

    def test_symbology_fields(self):
        syms = bs.list_symbologies()
        for s in syms:
            assert "symbology_id" in s
            assert "name" in s
            assert "category" in s

    def test_enable_disable_symbology(self):
        scanner = bs.create_scanner("zebra_snapi")
        assert scanner.enable_symbology("qr_code")
        assert "qr_code" in scanner.config.enabled_symbologies
        assert scanner.disable_symbology("qr_code")
        assert "qr_code" not in scanner.config.enabled_symbologies

    def test_enable_invalid_symbology(self):
        scanner = bs.create_scanner("zebra_snapi")
        assert not scanner.enable_symbology("invalid_symbology")

    def test_disable_not_enabled(self):
        scanner = bs.create_scanner("zebra_snapi")
        assert not scanner.disable_symbology("qr_code")

    def test_symbology_filter(self, qr_frame):
        result = bs.decode_frame("zebra_snapi", qr_frame, symbology_filter=["upc_a"])
        assert result.status == "unsupported_symbology"

    def test_symbology_filter_allows(self, qr_frame):
        result = bs.decode_frame("zebra_snapi", qr_frame, symbology_filter=["qr_code"])
        assert result.status == "success"


# ═══════════════════════════════════════════════════════════════════════
# 7. Symbology validation
# ═══════════════════════════════════════════════════════════════════════

class TestSymbologyValidation:
    def test_upc_a_valid(self):
        valid, msg = bs.validate_symbology_data("upc_a", "012345678905")
        assert valid

    def test_upc_a_invalid_length(self):
        valid, msg = bs.validate_symbology_data("upc_a", "0123")
        assert not valid

    def test_upc_a_invalid_check_digit(self):
        valid, msg = bs.validate_symbology_data("upc_a", "012345678900")
        assert not valid

    def test_ean_13_valid(self):
        valid, msg = bs.validate_symbology_data("ean_13", "4006381333931")
        assert valid

    def test_ean_13_invalid(self):
        valid, msg = bs.validate_symbology_data("ean_13", "4006381333930")
        assert not valid

    def test_ean_8_valid(self):
        valid, msg = bs.validate_symbology_data("ean_8", "96385074")
        assert valid

    def test_upc_e_valid(self):
        valid, msg = bs.validate_symbology_data("upc_e", "01234565")
        assert valid

    def test_code_128_valid(self):
        valid, msg = bs.validate_symbology_data("code_128", "Hello World")
        assert valid

    def test_code_128_non_ascii(self):
        valid, msg = bs.validate_symbology_data("code_128", "Hello\x80")
        assert not valid

    def test_code_39_valid(self):
        valid, msg = bs.validate_symbology_data("code_39", "HELLO-123")
        assert valid

    def test_code_39_invalid(self):
        valid, msg = bs.validate_symbology_data("code_39", "hello@world")
        assert not valid

    def test_codabar_valid(self):
        valid, msg = bs.validate_symbology_data("codabar", "A12345B")
        assert valid

    def test_interleaved_2of5_valid(self):
        valid, msg = bs.validate_symbology_data("interleaved_2of5", "1234")
        assert valid

    def test_interleaved_2of5_odd_digits(self):
        valid, msg = bs.validate_symbology_data("interleaved_2of5", "123")
        assert not valid

    def test_qr_code_valid(self):
        valid, msg = bs.validate_symbology_data("qr_code", "https://omnisight.dev")
        assert valid

    def test_data_matrix_valid(self):
        valid, msg = bs.validate_symbology_data("data_matrix", "SN-001")
        assert valid

    def test_pdf417_valid(self):
        valid, msg = bs.validate_symbology_data("pdf417", "TEST DATA")
        assert valid

    def test_aztec_valid(self):
        valid, msg = bs.validate_symbology_data("aztec", "TICKET-001")
        assert valid

    def test_unknown_symbology(self):
        valid, msg = bs.validate_symbology_data("invalid_sym", "test")
        assert not valid


# ═══════════════════════════════════════════════════════════════════════
# 8. Frame samples
# ═══════════════════════════════════════════════════════════════════════

class TestFrameSamples:
    def test_list_frame_samples(self):
        samples = bs.list_frame_samples()
        assert len(samples) == 7
        ids = {s["sample_id"] for s in samples}
        assert "upc_a_sample" in ids
        assert "qr_code_sample" in ids

    def test_generate_frame_sample(self):
        frame, meta = bs.generate_frame_sample("qr_code_sample")
        assert isinstance(frame, bytes)
        assert len(frame) > 0
        assert meta["symbology"] == "qr_code"
        assert meta["expected_data"] == "https://omnisight.dev"

    def test_generate_unknown_sample(self):
        with pytest.raises(ValueError, match="Unknown frame sample"):
            bs.generate_frame_sample("nonexistent")

    @pytest.mark.parametrize("sample_id", [
        "upc_a_sample",
        "ean_13_sample",
        "code_128_sample",
        "qr_code_sample",
        "data_matrix_sample",
        "pdf417_sample",
        "aztec_sample",
    ])
    def test_validate_frame_sample_all_vendors(self, sample_id):
        for vid in bs.VendorId:
            result = bs.validate_frame_sample(sample_id, vid.value)
            assert result["match"], f"{sample_id} failed on {vid.value}: {result}"
            assert result["status"] == "passed"

    def test_frame_sample_fields(self):
        samples = bs.list_frame_samples()
        for s in samples:
            assert "sample_id" in s
            assert "symbology" in s
            assert "expected_data" in s
            assert "width" in s
            assert "height" in s


# ═══════════════════════════════════════════════════════════════════════
# 9. Error handling
# ═══════════════════════════════════════════════════════════════════════

class TestErrorHandling:
    def test_scan_empty_frame(self):
        result = bs.decode_frame("zebra_snapi", b"")
        assert result.status == "no_decode"

    def test_scan_random_bytes(self):
        result = bs.decode_frame("zebra_snapi", b"\xff" * 100)
        assert result.status == "no_decode"

    def test_scan_short_frame(self):
        result = bs.decode_frame("zebra_snapi", b"\x01\x02\x03")
        assert result.status == "no_decode"

    def test_scan_when_disconnected(self):
        scanner = bs.create_scanner("zebra_snapi")
        result = scanner.scan(b"test")
        assert result.status == "error"

    def test_scan_truncated_marker(self):
        result = bs.decode_frame("zebra_snapi", b"\x00\x01\xba\x5c")
        assert result.status == "no_decode"


# ═══════════════════════════════════════════════════════════════════════
# 10. Test recipes
# ═══════════════════════════════════════════════════════════════════════

class TestRecipes:
    def test_list_test_recipes(self):
        recipes = bs.list_test_recipes()
        assert len(recipes) == 6
        ids = {r["recipe_id"] for r in recipes}
        assert "vendor_adapter_lifecycle" in ids
        assert "symbology_decode" in ids
        assert "decode_mode_switch" in ids

    def test_run_vendor_lifecycle(self):
        result = bs.run_test_recipe("vendor_adapter_lifecycle")
        assert result.status == "passed"
        assert result.total == 4
        assert result.passed == 4
        assert result.failed == 0

    def test_run_symbology_decode(self):
        result = bs.run_test_recipe("symbology_decode")
        assert result.status == "passed"
        assert result.passed == 7
        assert result.failed == 0

    def test_run_decode_mode_switch(self):
        result = bs.run_test_recipe("decode_mode_switch")
        assert result.status == "passed"
        assert result.passed == 3
        assert result.failed == 0

    def test_run_frame_sample_validation(self):
        result = bs.run_test_recipe("frame_sample_validation")
        assert result.status == "passed"
        assert result.passed == 28  # 7 samples × 4 vendors

    def test_run_multi_vendor_roundtrip(self):
        result = bs.run_test_recipe("multi_vendor_roundtrip")
        assert result.status == "passed"
        assert result.passed == 7

    def test_run_error_handling(self):
        result = bs.run_test_recipe("error_handling")
        assert result.status == "passed"
        assert result.passed == 5
        assert result.failed == 0

    def test_run_unknown_recipe(self):
        result = bs.run_test_recipe("nonexistent")
        assert result.status == "error"


# ═══════════════════════════════════════════════════════════════════════
# 11. Artifacts & gate
# ═══════════════════════════════════════════════════════════════════════

class TestArtifactsAndGate:
    def test_list_artifacts(self):
        arts = bs.list_artifacts()
        assert len(arts) == 5
        kinds = {a["kind"] for a in arts}
        assert kinds == {"tasks", "scaffolds", "tests", "hil", "docs"}

    def test_validate_gate(self):
        result = bs.validate_gate()
        assert result["verdict"] == "passed"
        assert result["total_recipes"] == 6
        assert result["total_failed"] == 0
        assert result["total_passed"] > 0


# ═══════════════════════════════════════════════════════════════════════
# 12. Multi-vendor consistency
# ═══════════════════════════════════════════════════════════════════════

class TestMultiVendorConsistency:
    @pytest.mark.parametrize("sample_id", [
        "upc_a_sample",
        "ean_13_sample",
        "code_128_sample",
        "qr_code_sample",
        "data_matrix_sample",
        "pdf417_sample",
        "aztec_sample",
    ])
    def test_all_vendors_same_result(self, sample_id):
        frame, meta = bs.generate_frame_sample(sample_id)
        results = {}
        for vid in bs.VendorId:
            r = bs.decode_frame(vid.value, frame)
            results[vid.value] = r.data
        values = list(results.values())
        assert all(v == values[0] for v in values), f"Inconsistent: {results}"
        assert values[0] == meta["expected_data"]


# ═══════════════════════════════════════════════════════════════════════
# 13. Synthetic frame generation
# ═══════════════════════════════════════════════════════════════════════

class TestSyntheticFrames:
    def test_frame_has_marker(self):
        frame = bs._generate_synthetic_frame("qr_code", "test", 100, 100)
        assert b"\xba\x5c" in frame

    def test_frame_hash_deterministic(self):
        f1 = bs._generate_synthetic_frame("qr_code", "test", 100, 100)
        f2 = bs._generate_synthetic_frame("qr_code", "test", 100, 100)
        assert bs._frame_hash(f1) == bs._frame_hash(f2)

    def test_frame_hash_different_data(self):
        f1 = bs._generate_synthetic_frame("qr_code", "test1", 100, 100)
        f2 = bs._generate_synthetic_frame("qr_code", "test2", 100, 100)
        assert bs._frame_hash(f1) != bs._frame_hash(f2)


# ═══════════════════════════════════════════════════════════════════════
# 14. Adapter-specific features
# ═══════════════════════════════════════════════════════════════════════

class TestAdapterSpecific:
    def test_zebra_capabilities(self):
        scanner = bs.create_scanner("zebra_snapi")
        caps = scanner.get_capabilities()
        assert caps["sdk"] == "CoreScanner"
        assert caps["beeper_control"] is True
        assert caps["led_control"] is True

    def test_honeywell_capabilities(self):
        scanner = bs.create_scanner("honeywell")
        caps = scanner.get_capabilities()
        assert caps["sdk"] == "FreeScan"
        assert caps["aim_control"] is True

    def test_datalogic_capabilities(self):
        scanner = bs.create_scanner("datalogic")
        caps = scanner.get_capabilities()
        assert caps["sdk"] == "Aladdin"
        assert caps["green_spot_aim"] is True

    def test_newland_capabilities(self):
        scanner = bs.create_scanner("newland")
        caps = scanner.get_capabilities()
        assert caps["sdk"] == "NLS"
        assert caps["illumination_control"] is True


# ═══════════════════════════════════════════════════════════════════════
# 15. Enums
# ═══════════════════════════════════════════════════════════════════════

class TestEnums:
    def test_vendor_id_enum(self):
        assert len(bs.VendorId) == 4

    def test_symbology_id_enum(self):
        assert len(bs.SymbologyId) >= 16

    def test_decode_mode_enum(self):
        assert len(bs.DecodeMode) == 3

    def test_scanner_state_enum(self):
        assert len(bs.ScannerState) == 5

    def test_scan_result_status_enum(self):
        assert len(bs.ScanResultStatus) == 5

    def test_barcode_domain_enum(self):
        assert len(bs.BarcodeDomain) == 6
