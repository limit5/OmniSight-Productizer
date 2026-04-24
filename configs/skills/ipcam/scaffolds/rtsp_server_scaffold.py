"""Scaffold: IPCam RTSP Server — live555 / gstreamer dual-backend (#219).

Usage:
    from backend.ipcam_rtsp_server import (
        RTSPBackend, RTSPServerConfig, StreamMount, VideoCodec,
        RTSPServerManager, detect_available_backends, build_sdp,
    )

    # 1. Pick a backend (auto-selects the first available library)
    backend = detect_available_backends()[0]  # e.g. RTSPBackend.GSTREAMER

    # 2. Build server config (8554 is the IANA-reserved RTSP alt port;
    #    many NVRs probe 554 and 8554 by default)
    config = RTSPServerConfig(
        backend=backend,
        bind_address="0.0.0.0",
        port=8554,
        max_sessions=32,
        session_timeout_s=60,
        auth_realm="OmniSight-IPCam",
    )

    # 3. Register mount points (paths under rtsp://host:port/<path>)
    manager = RTSPServerManager(config)
    manager.add_mount(
        StreamMount(
            path="live/main",
            codec=VideoCodec.H264,
            width=1920,
            height=1080,
            fps=30,
            bitrate_kbps=4096,
            description="Main stream (1080p30 H.264)",
        )
    )
    manager.add_mount(
        StreamMount(
            path="live/sub",
            codec=VideoCodec.H264,
            width=640,
            height=480,
            fps=15,
            bitrate_kbps=512,
            description="Sub stream (480p15 for mobile / preview)",
        )
    )

    # 4. Set up Digest authentication (plaintext credentials must stay server-side)
    manager.add_credential(username="admin", password="hunter2", role="admin")

    # 5. Start the server — in tests this is a dry-run (no socket bind)
    manager.start()

    # 6. SDP is generated on DESCRIBE — you can also build one manually
    sdp = build_sdp(
        mount=manager.get_mount("live/main"),
        bind_address="192.168.1.100",
        session_id="1234567890",
    )
    print(sdp)

    # 7. Feed encoded NAL units in (from hw_codec_binding task) — sessions route
    #    the data out through either live555's OnDemandServerMediaSubsession queue
    #    or the gstreamer appsrc attached to the mount's launch pipeline.
    manager.push_access_unit("live/main", nal_units=encoded_nals, pts_90khz=pts)

    # 8. Query status
    print(manager.status())
    # -> {"backend": "gstreamer", "active_sessions": 2, "mounts": ["live/main", "live/sub"]}

    # 9. Clean shutdown
    manager.stop()
"""
