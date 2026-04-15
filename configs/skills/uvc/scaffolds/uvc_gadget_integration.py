"""Scaffold: UVC 1.5 Gadget Integration Example.

Usage:
    from backend.uvc_gadget import (
        UVCGadgetManager, GadgetConfig, StreamFormat,
        UVCDescriptorBuilder, UVCH264PayloadGenerator,
    )

    # 1. Build gadget config
    config = GadgetConfig(
        gadget_name="g_uvc",
        vendor_id=0x1d6b,
        product_id=0x0104,
        manufacturer="OmniSight",
        product="UVC Camera",
        serial="000000000001",
    )

    # 2. Create and bind gadget
    manager = UVCGadgetManager(config)
    manager.create_gadget()
    manager.bind_udc()

    # 3. Start H.264 streaming
    manager.start_stream(StreamFormat.H264, width=1920, height=1080, fps=30)

    # 4. Feed frames via payload generator
    gen = UVCH264PayloadGenerator(max_payload_size=3072)
    for nal_unit in h264_encoder.get_nal_units():
        payloads = gen.generate(nal_unit)
        for payload in payloads:
            manager.send_payload(payload)

    # 5. Capture still image
    snapshot = manager.capture_still()
    print(f"Still: {snapshot.path} ({snapshot.size} bytes)")

    # 6. Extension unit controls
    fw_version = manager.xu_get(selector=1)
    manager.xu_set(selector=2, value=128)  # ISP brightness

    # 7. Cleanup
    manager.stop_stream()
    manager.unbind_udc()
    manager.destroy_gadget()
"""
