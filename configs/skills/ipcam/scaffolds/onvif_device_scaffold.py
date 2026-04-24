"""Scaffold: IPCam ONVIF Device/Media/Events/PTZ endpoints (#219).

Usage:
    from backend.ipcam_rtsp_server import (
        AuthScheme, RTSPBackend, RTSPServerConfig, RTSPServerManager,
        StreamMount, VideoCodec,
    )
    from backend.onvif_device import (
        ONVIFDevice, ONVIFService, ONVIFServiceConfig,
        DeviceInformation, NetworkInterface, PTZConfiguration,
        VideoSource, NotificationMessage, UserLevel,
    )

    # 1. Bring up the RTSP scaffold first (it owns the mount registry)
    rtsp_cfg = RTSPServerConfig(backend=RTSPBackend.STUB, port=8554)
    mgr = RTSPServerManager(rtsp_cfg)
    mgr.add_mount(StreamMount(path="live/main", codec=VideoCodec.H264,
                              width=1920, height=1080, fps=30,
                              bitrate_kbps=4096))
    mgr.add_mount(StreamMount(path="live/sub", codec=VideoCodec.H264,
                              width=640, height=480, fps=15,
                              bitrate_kbps=512))
    mgr.start()

    # 2. Build ONVIF service config — xaddr is the HTTP endpoint NVRs
    #    probe; rtsp_host / rtsp_port is the URI ONVIF advertises via
    #    GetStreamUri. Usually rtsp_host == xaddr_host on the device.
    onvif_cfg = ONVIFServiceConfig(
        scheme="http",
        xaddr_host="10.0.0.1",
        xaddr_port=80,
        rtsp_port=8554,
        require_auth=True,          # WS-UsernameToken mandatory
    )

    # 3. Assemble the device — every RTSP mount becomes a MediaProfile
    device = ONVIFDevice(
        config=onvif_cfg,
        rtsp_manager=mgr,
        device_info=DeviceInformation(
            manufacturer="OmniSight", model="IPCam-Reference",
            firmware_version="1.0.0", serial_number="SN0001",
        ),
        network_interfaces=[NetworkInterface(token="eth0",
                                             mac_address="02:00:00:00:00:01",
                                             ipv4_address="10.0.0.1")],
        video_sources=[VideoSource(token="VideoSource_1")],
        ptz_configuration=PTZConfiguration(),
    )

    # 4. Seed the first admin credential — the NVR will WS-UsernameToken
    #    against it; the same credential doubles as the RTSP Digest user.
    device.add_user("admin", "hunter2", UserLevel.ADMINISTRATOR)

    # 5. Dispatch raw SOAP 1.2 bytes — bind this to /onvif/device_service,
    #    /onvif/media_service, /onvif/events_service, /onvif/ptz_service
    #    in any web framework (FastAPI / WSGI / ASGI).
    status, body = device.dispatch(
        ONVIFService.DEVICE,
        raw_soap_bytes,
        remote_address="10.0.0.99",
    )
    # → (200, <s:Envelope>...<tds:GetDeviceInformationResponse>...)

    # 6. Publish events to subscribers (e.g. motion detector triggers)
    device.publish_event(NotificationMessage(
        topic="tns1:RuleEngine/MotionRegionDetector",
        produced_at=time.time(),
        source={"Source": "VideoSource_1"},
        data={"State": "true"},
    ))

    # 7. PTZ coordinate frame: pan ∈ [-1, 1], tilt ∈ [-1, 1], zoom ∈ [0, 1]
    #    (all matching the default PTZConfiguration ranges). Values
    #    outside the configured range are *clamped*, not rejected.
"""
