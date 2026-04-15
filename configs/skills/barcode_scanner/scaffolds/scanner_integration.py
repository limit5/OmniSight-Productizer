"""Scaffold: Barcode Scanner Integration Example.

Usage:
    from backend.barcode_scanner import create_scanner, ScannerConfig

    config = ScannerConfig(
        vendor_id="zebra_snapi",
        decode_mode="api",
        enabled_symbologies=["qr_code", "code_128", "ean_13"],
    )
    scanner = create_scanner("zebra_snapi", config)
    scanner.connect()
    scanner.configure(config)
    result = scanner.scan(frame_data)
    print(f"Decoded: {result.symbology} -> {result.data}")
    scanner.disconnect()
"""
