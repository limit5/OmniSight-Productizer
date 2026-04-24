"""D2 — SKILL-IPCAM: RTSP server scaffold tests (#219).

Offline coverage for the dual-backend (live555 / gstreamer / stub) RTSP
scaffold: backend detection & selection, config validation, mount
lifecycle, session FSM, transport parsing, SDP generation, Digest /
Basic authentication, RTSP method dispatch, and error-handling paths.
"""

from __future__ import annotations

import pytest

from backend import ipcam_rtsp_server as rtsp


# ═══════════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════════


@pytest.fixture
def stub_config():
    return rtsp.RTSPServerConfig(
        backend=rtsp.RTSPBackend.STUB,
        bind_address="127.0.0.1",
        port=8554,
        auth_scheme=rtsp.AuthScheme.DIGEST,
        auth_realm="OmniSightTest",
    )


@pytest.fixture
def no_auth_config():
    return rtsp.RTSPServerConfig(
        backend=rtsp.RTSPBackend.STUB,
        bind_address="127.0.0.1",
        port=8554,
        auth_scheme=rtsp.AuthScheme.NONE,
        auth_realm="OmniSightTest",
    )


@pytest.fixture
def h264_mount():
    return rtsp.StreamMount(
        path="live/main",
        codec=rtsp.VideoCodec.H264,
        width=1920,
        height=1080,
        fps=30,
        bitrate_kbps=4096,
        description="Main 1080p30 H.264",
        profile_level_id="42001F",
        sprop_parameter_sets="Z0KAH9oBQBboQAAA,aM48gA==",
    )


@pytest.fixture
def h265_mount():
    return rtsp.StreamMount(
        path="live/hevc",
        codec=rtsp.VideoCodec.H265,
        width=3840,
        height=2160,
        fps=15,
        bitrate_kbps=16384,
        description="4K15 H.265",
        sprop_vps="QAEMAf//",
        sprop_sps="QgEBAWAAAA==",
        sprop_pps="RAHAjA==",
    )


@pytest.fixture
def manager(stub_config, h264_mount, h265_mount):
    mgr = rtsp.RTSPServerManager(stub_config)
    mgr.add_mount(h264_mount)
    mgr.add_mount(h265_mount)
    mgr.add_credential("admin", "hunter2", role="admin")
    mgr.start()
    yield mgr
    mgr.stop()


# ═══════════════════════════════════════════════════════════════════════
# rtsp_backend — detection + selection
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.rtsp_backend
class TestBackendDetection:
    def test_stub_always_present(self):
        backends = rtsp.detect_available_backends(refresh=True)
        assert rtsp.RTSPBackend.STUB in backends

    def test_stub_is_last_choice(self):
        """STUB must always be appended last — real backends win preference."""
        backends = rtsp.detect_available_backends(refresh=True)
        assert backends[-1] == rtsp.RTSPBackend.STUB

    def test_cache_is_reused(self):
        a = rtsp.detect_available_backends(refresh=True)
        b = rtsp.detect_available_backends(refresh=False)
        assert a == b
        # Same list contents (not same object — we return a copy to
        # protect the cache from caller mutation).
        assert a is not rtsp._BACKEND_CACHE

    def test_refresh_forces_reprobe(self, monkeypatch):
        monkeypatch.setattr(rtsp, "_BACKEND_CACHE", None)
        backends = rtsp.detect_available_backends(refresh=True)
        assert backends  # non-empty
        assert rtsp._BACKEND_CACHE is not None

    def test_select_backend_honours_preference(self):
        selected = rtsp.select_backend(rtsp.RTSPBackend.STUB)
        assert selected == rtsp.RTSPBackend.STUB

    def test_select_backend_auto(self, monkeypatch):
        monkeypatch.delenv("OMNISIGHT_IPCAM_RTSP_BACKEND", raising=False)
        selected = rtsp.select_backend(None)
        # Must be one of the enum values
        assert selected in set(rtsp.RTSPBackend)

    def test_select_backend_env_override(self, monkeypatch):
        monkeypatch.setenv("OMNISIGHT_IPCAM_RTSP_BACKEND", "stub")
        selected = rtsp.select_backend(None)
        assert selected == rtsp.RTSPBackend.STUB

    def test_select_backend_env_invalid(self, monkeypatch):
        monkeypatch.setenv("OMNISIGHT_IPCAM_RTSP_BACKEND", "bogus")
        with pytest.raises(rtsp.RTSPBackendUnavailable):
            rtsp.select_backend(None)

    def test_select_backend_preference_unavailable(self, monkeypatch):
        """Forcing a backend that probe says is missing must raise."""
        monkeypatch.setattr(
            rtsp, "_BACKEND_CACHE", [rtsp.RTSPBackend.STUB]
        )
        with pytest.raises(rtsp.RTSPBackendUnavailable):
            rtsp.select_backend(rtsp.RTSPBackend.LIVE555)


# ═══════════════════════════════════════════════════════════════════════
# rtsp_config — validation
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.rtsp_config
class TestConfigValidation:
    def test_defaults_are_valid(self):
        cfg = rtsp.RTSPServerConfig()
        assert cfg.port == rtsp.RTSP_DEFAULT_PORT
        assert cfg.auth_scheme == rtsp.AuthScheme.DIGEST
        assert cfg.max_sessions == 32

    @pytest.mark.parametrize("port", [0, -1, 65536, 100000])
    def test_invalid_port_rejected(self, port):
        with pytest.raises(ValueError, match="RTSP port out of range"):
            rtsp.RTSPServerConfig(port=port)

    @pytest.mark.parametrize("port", [1, 554, 8554, 65535])
    def test_valid_port_accepted(self, port):
        cfg = rtsp.RTSPServerConfig(port=port)
        assert cfg.port == port

    def test_max_sessions_zero_rejected(self):
        with pytest.raises(ValueError, match="max_sessions"):
            rtsp.RTSPServerConfig(max_sessions=0)

    def test_max_sessions_above_hard_cap_rejected(self):
        with pytest.raises(ValueError, match="max_sessions"):
            rtsp.RTSPServerConfig(max_sessions=10_000)

    def test_session_timeout_must_be_positive(self):
        with pytest.raises(ValueError, match="session_timeout_s"):
            rtsp.RTSPServerConfig(session_timeout_s=0)

    def test_empty_realm_rejected(self):
        with pytest.raises(ValueError, match="auth_realm"):
            rtsp.RTSPServerConfig(auth_realm="")


# ═══════════════════════════════════════════════════════════════════════
# rtsp_mount — registration, removal, path normalisation
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.rtsp_mount
class TestMountManagement:
    def test_add_and_list(self, manager):
        assert sorted(manager.list_mounts()) == ["live/hevc", "live/main"]

    def test_get_mount_by_path(self, manager):
        m = manager.get_mount("live/main")
        assert m.codec == rtsp.VideoCodec.H264
        assert m.width == 1920

    def test_get_mount_leading_slash(self, manager):
        """DESCRIBE uri arrives as "/live/main" — must normalise."""
        m = manager.get_mount("/live/main")
        assert m is not None

    def test_get_unknown_mount_raises(self, manager):
        with pytest.raises(rtsp.RTSPMountNotFound):
            manager.get_mount("does/not/exist")

    def test_duplicate_mount_rejected(self, manager, h264_mount):
        with pytest.raises(ValueError, match="already registered"):
            manager.add_mount(h264_mount)

    def test_remove_mount(self, manager):
        ok = manager.remove_mount("live/main")
        assert ok is True
        assert "live/main" not in manager.list_mounts()

    def test_remove_unknown_mount_is_noop(self, manager):
        ok = manager.remove_mount("never/existed")
        assert ok is False

    @pytest.mark.parametrize(
        "bad",
        ["", "/", " ", "has space", "a" * 200, ";rogue", ".hidden/only"],
    )
    def test_invalid_mount_path_rejected(self, bad):
        with pytest.raises(ValueError):
            rtsp.StreamMount(path=bad)

    def test_mount_fps_range(self):
        with pytest.raises(ValueError, match="fps"):
            rtsp.StreamMount(path="x/y", fps=0)
        with pytest.raises(ValueError, match="fps"):
            rtsp.StreamMount(path="x/y", fps=121)

    def test_mount_rtp_payload_type(self, h264_mount, h265_mount):
        assert h264_mount.rtp_payload_type == rtsp.PAYLOAD_TYPE_H264
        assert h265_mount.rtp_payload_type == rtsp.PAYLOAD_TYPE_H265

    def test_mount_rtp_encoding_name(self, h264_mount, h265_mount):
        assert h264_mount.rtp_encoding_name == "H264"
        assert h265_mount.rtp_encoding_name == "H265"


# ═══════════════════════════════════════════════════════════════════════
# rtsp_session — FSM + timeout
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.rtsp_session
class TestSessionLifecycle:
    def test_new_session_is_ready(self, manager):
        transport = rtsp.parse_transport("RTP/AVP;unicast;client_port=8000-8001")
        session = manager.create_session("live/main", transport)
        assert session.state == rtsp.SessionState.READY

    def test_valid_transitions(self, manager):
        transport = rtsp.parse_transport("RTP/AVP;unicast;client_port=8000-8001")
        session = manager.create_session("live/main", transport)
        session.transition(rtsp.SessionState.PLAYING)
        session.transition(rtsp.SessionState.PAUSED)
        session.transition(rtsp.SessionState.PLAYING)
        session.transition(rtsp.SessionState.READY)
        session.transition(rtsp.SessionState.TEARDOWN)

    def test_invalid_transition_raises(self, manager):
        transport = rtsp.parse_transport("RTP/AVP;unicast;client_port=8000-8001")
        session = manager.create_session("live/main", transport)
        # READY → PAUSED is not permitted (must go via PLAYING)
        with pytest.raises(rtsp.RTSPSessionStateError):
            session.transition(rtsp.SessionState.PAUSED)

    def test_teardown_is_terminal(self, manager):
        transport = rtsp.parse_transport("RTP/AVP;unicast;client_port=8000-8001")
        session = manager.create_session("live/main", transport)
        session.transition(rtsp.SessionState.TEARDOWN)
        with pytest.raises(rtsp.RTSPSessionStateError):
            session.transition(rtsp.SessionState.PLAYING)

    def test_max_sessions_enforced(self, stub_config, h264_mount):
        stub_config = rtsp.RTSPServerConfig(
            backend=rtsp.RTSPBackend.STUB,
            max_sessions=2,
            auth_scheme=rtsp.AuthScheme.NONE,
        )
        mgr = rtsp.RTSPServerManager(stub_config)
        mgr.add_mount(h264_mount)
        mgr.start()
        transport = rtsp.parse_transport("RTP/AVP;unicast;client_port=8000-8001")
        mgr.create_session("live/main", transport)
        mgr.create_session(
            "live/main",
            rtsp.parse_transport("RTP/AVP;unicast;client_port=9000-9001"),
        )
        with pytest.raises(rtsp.RTSPError, match="max_sessions"):
            mgr.create_session(
                "live/main",
                rtsp.parse_transport("RTP/AVP;unicast;client_port=10000-10001"),
            )
        mgr.stop()

    def test_session_timeout_detection(self, manager):
        import time
        transport = rtsp.parse_transport("RTP/AVP;unicast;client_port=8000-8001")
        session = manager.create_session("live/main", transport)
        session.timeout_s = 0  # force immediate expiry
        time.sleep(0.02)
        assert session.is_expired() is True

    def test_purge_expired_sessions(self, manager):
        import time
        transport = rtsp.parse_transport("RTP/AVP;unicast;client_port=8000-8001")
        session = manager.create_session("live/main", transport)
        session.timeout_s = 0
        time.sleep(0.02)
        purged = manager.purge_expired_sessions()
        assert session.session_id in purged
        assert session.session_id not in [s.session_id for s in manager.list_sessions()]

    def test_mount_not_found_on_setup(self, manager):
        transport = rtsp.parse_transport("RTP/AVP;unicast;client_port=8000-8001")
        with pytest.raises(rtsp.RTSPMountNotFound):
            manager.create_session("ghost/path", transport)


# ═══════════════════════════════════════════════════════════════════════
# rtsp_transport — header parsing
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.rtsp_transport
class TestTransportParsing:
    def test_basic_unicast_udp(self):
        spec = rtsp.parse_transport("RTP/AVP;unicast;client_port=8000-8001")
        assert spec.protocol == rtsp.TransportProtocol.RTP_AVP_UDP
        assert spec.unicast is True
        assert spec.client_port_rtp == 8000
        assert spec.client_port_rtcp == 8001

    def test_interleaved_tcp(self):
        spec = rtsp.parse_transport("RTP/AVP/TCP;unicast;interleaved=0-1")
        assert spec.protocol == rtsp.TransportProtocol.RTP_AVP_TCP
        assert spec.interleaved_rtp == 0
        assert spec.interleaved_rtcp == 1

    def test_avpf_udp(self):
        spec = rtsp.parse_transport("RTP/AVPF;unicast;client_port=5000-5001")
        assert spec.protocol == rtsp.TransportProtocol.RTP_AVPF_UDP

    def test_multicast(self):
        spec = rtsp.parse_transport(
            "RTP/AVP;multicast;destination=239.1.2.3;ttl=8;client_port=7000-7001"
        )
        assert spec.unicast is False
        assert spec.multicast_address == "239.1.2.3"
        assert spec.ttl == 8

    def test_ssrc(self):
        spec = rtsp.parse_transport("RTP/AVP;unicast;client_port=8000-8001;ssrc=DEADBEEF")
        assert spec.ssrc == 0xDEADBEEF

    def test_mode_record(self):
        spec = rtsp.parse_transport(
            'RTP/AVP;unicast;client_port=8000-8001;mode="RECORD"'
        )
        assert spec.mode == "RECORD"

    def test_unknown_protocol_rejected(self):
        with pytest.raises(rtsp.RTSPUnsupportedTransport):
            rtsp.parse_transport("RAW/TLS;unicast;client_port=8000-8001")

    def test_empty_header_rejected(self):
        with pytest.raises(rtsp.RTSPBadRequest):
            rtsp.parse_transport("")

    def test_to_header_roundtrip_udp(self):
        spec = rtsp.TransportSpec(
            protocol=rtsp.TransportProtocol.RTP_AVP_UDP,
            unicast=True,
            client_port_rtp=8000,
            client_port_rtcp=8001,
        )
        header = spec.to_header()
        assert "RTP/AVP" in header
        assert "unicast" in header
        assert "client_port=8000-8001" in header

    def test_to_header_roundtrip_tcp(self):
        spec = rtsp.TransportSpec(
            protocol=rtsp.TransportProtocol.RTP_AVP_TCP,
            interleaved_rtp=2,
            interleaved_rtcp=3,
        )
        header = spec.to_header()
        assert "RTP/AVP/TCP" in header
        assert "interleaved=2-3" in header

    def test_unknown_attribute_ignored(self):
        """RFC 2326 §12.39: unknown Transport attributes MUST be ignored."""
        spec = rtsp.parse_transport(
            "RTP/AVP;unicast;client_port=8000-8001;made_up_attr=xyz"
        )
        assert spec.client_port_rtp == 8000


# ═══════════════════════════════════════════════════════════════════════
# rtsp_method — OPTIONS / DESCRIBE / SETUP / PLAY / PAUSE / TEARDOWN
# ═══════════════════════════════════════════════════════════════════════


def _req(mgr, raw: bytes) -> dict:
    req = rtsp.parse_rtsp_request(raw)
    return mgr.handle_request(req)


@pytest.mark.rtsp_method
class TestMethodDispatch:
    def _open(self, manager):
        """Return a manager with auth off for request-flow tests."""
        from dataclasses import replace
        mgr = rtsp.RTSPServerManager(
            replace(manager._config, auth_scheme=rtsp.AuthScheme.NONE)
        )
        for p, m in manager._mounts.items():
            mgr.add_mount(m)
        mgr.start()
        return mgr

    def test_options_returns_public_header(self, manager):
        mgr = self._open(manager)
        try:
            resp = _req(
                mgr,
                b"OPTIONS rtsp://1.2.3.4:8554/ RTSP/1.0\r\nCSeq: 1\r\n\r\n",
            )
            assert resp["status"] == 200
            assert "DESCRIBE" in resp["headers"]["Public"]
            assert "SETUP" in resp["headers"]["Public"]
        finally:
            mgr.stop()

    def test_describe_returns_sdp(self, manager):
        mgr = self._open(manager)
        try:
            resp = _req(
                mgr,
                b"DESCRIBE rtsp://1.2.3.4:8554/live/main RTSP/1.0\r\nCSeq: 2\r\n\r\n",
            )
            assert resp["status"] == 200
            assert resp["headers"]["Content-Type"] == "application/sdp"
            assert "m=video" in resp["body"]
            assert "H264" in resp["body"]
        finally:
            mgr.stop()

    def test_describe_unknown_mount_404(self, manager):
        mgr = self._open(manager)
        try:
            with pytest.raises(rtsp.RTSPMountNotFound):
                _req(
                    mgr,
                    b"DESCRIBE rtsp://1.2.3.4:8554/ghost RTSP/1.0\r\nCSeq: 1\r\n\r\n",
                )
        finally:
            mgr.stop()

    def test_setup_creates_session(self, manager):
        mgr = self._open(manager)
        try:
            resp = _req(
                mgr,
                b"SETUP rtsp://1.2.3.4:8554/live/main RTSP/1.0\r\n"
                b"CSeq: 3\r\n"
                b"Transport: RTP/AVP;unicast;client_port=8000-8001\r\n\r\n",
            )
            assert resp["status"] == 200
            sess = resp["headers"]["Session"]
            assert ";timeout=" in sess
            assert len(mgr.list_sessions()) == 1
        finally:
            mgr.stop()

    def test_play_transitions_session(self, manager):
        mgr = self._open(manager)
        try:
            setup = _req(
                mgr,
                b"SETUP rtsp://1.2.3.4:8554/live/main RTSP/1.0\r\n"
                b"CSeq: 3\r\n"
                b"Transport: RTP/AVP;unicast;client_port=8000-8001\r\n\r\n",
            )
            session_id = setup["headers"]["Session"].split(";", 1)[0]
            resp = _req(
                mgr,
                f"PLAY rtsp://1.2.3.4:8554/live/main RTSP/1.0\r\n"
                f"CSeq: 4\r\nSession: {session_id}\r\n\r\n".encode(),
            )
            assert resp["status"] == 200
            assert (
                mgr.get_session(session_id).state == rtsp.SessionState.PLAYING
            )
        finally:
            mgr.stop()

    def test_pause_from_playing(self, manager):
        mgr = self._open(manager)
        try:
            setup = _req(
                mgr,
                b"SETUP rtsp://1.2.3.4:8554/live/main RTSP/1.0\r\n"
                b"CSeq: 3\r\n"
                b"Transport: RTP/AVP;unicast;client_port=8000-8001\r\n\r\n",
            )
            sid = setup["headers"]["Session"].split(";", 1)[0]
            _req(
                mgr,
                f"PLAY rtsp://1.2.3.4:8554/live/main RTSP/1.0\r\nCSeq: 4\r\nSession: {sid}\r\n\r\n".encode(),
            )
            resp = _req(
                mgr,
                f"PAUSE rtsp://1.2.3.4:8554/live/main RTSP/1.0\r\nCSeq: 5\r\nSession: {sid}\r\n\r\n".encode(),
            )
            assert resp["status"] == 200
            assert mgr.get_session(sid).state == rtsp.SessionState.PAUSED
        finally:
            mgr.stop()

    def test_teardown_drops_session(self, manager):
        mgr = self._open(manager)
        try:
            setup = _req(
                mgr,
                b"SETUP rtsp://1.2.3.4:8554/live/main RTSP/1.0\r\n"
                b"CSeq: 3\r\n"
                b"Transport: RTP/AVP;unicast;client_port=8000-8001\r\n\r\n",
            )
            sid = setup["headers"]["Session"].split(";", 1)[0]
            resp = _req(
                mgr,
                f"TEARDOWN rtsp://1.2.3.4:8554/live/main RTSP/1.0\r\nCSeq: 99\r\nSession: {sid}\r\n\r\n".encode(),
            )
            assert resp["status"] == 200
            assert not mgr.list_sessions()
        finally:
            mgr.stop()

    def test_get_parameter_keepalive(self, manager):
        mgr = self._open(manager)
        try:
            setup = _req(
                mgr,
                b"SETUP rtsp://1.2.3.4:8554/live/main RTSP/1.0\r\n"
                b"CSeq: 3\r\n"
                b"Transport: RTP/AVP;unicast;client_port=8000-8001\r\n\r\n",
            )
            sid = setup["headers"]["Session"].split(";", 1)[0]
            resp = _req(
                mgr,
                f"GET_PARAMETER rtsp://1.2.3.4:8554/ RTSP/1.0\r\nCSeq: 10\r\nSession: {sid}\r\n\r\n".encode(),
            )
            assert resp["status"] == 200
        finally:
            mgr.stop()

    def test_unknown_method_501(self, manager):
        mgr = self._open(manager)
        try:
            resp = _req(
                mgr,
                b"NONSENSE rtsp://1.2.3.4:8554/ RTSP/1.0\r\nCSeq: 1\r\n\r\n",
            )
            assert resp["status"] == 501
        finally:
            mgr.stop()

    def test_teardown_unknown_session_454(self, manager):
        mgr = self._open(manager)
        try:
            with pytest.raises(rtsp.RTSPSessionNotFound):
                _req(
                    mgr,
                    b"TEARDOWN rtsp://1.2.3.4:8554/ RTSP/1.0\r\n"
                    b"CSeq: 99\r\nSession: nonexistent\r\n\r\n",
                )
        finally:
            mgr.stop()


# ═══════════════════════════════════════════════════════════════════════
# rtsp_auth — Digest + Basic
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.rtsp_auth
class TestAuthentication:
    def test_no_auth_passes(self, no_auth_config, h264_mount):
        mgr = rtsp.RTSPServerManager(no_auth_config)
        mgr.add_mount(h264_mount)
        mgr.start()
        try:
            req = rtsp.parse_rtsp_request(
                b"DESCRIBE rtsp://host/live/main RTSP/1.0\r\nCSeq: 1\r\n\r\n"
            )
            assert mgr.authenticate(req) is None
        finally:
            mgr.stop()

    def test_digest_challenge_format(self):
        challenge = rtsp.build_digest_challenge(
            realm="OmniSight", nonce="abc123", qop="auth"
        )
        assert challenge.startswith("Digest ")
        assert 'realm="OmniSight"' in challenge
        assert 'nonce="abc123"' in challenge
        assert "algorithm=MD5" in challenge
        assert 'qop="auth"' in challenge

    def test_digest_challenge_stale(self):
        challenge = rtsp.build_digest_challenge(
            realm="r", nonce="n", stale=True
        )
        assert "stale=true" in challenge

    def test_digest_response_computation(self):
        # RFC 7616 §3.9.1 test vector (adapted): verify deterministic MD5
        r1 = rtsp.compute_digest_response(
            username="Mufasa",
            password="Circle of Life",
            realm="http-auth@example.org",
            method="GET",
            uri="/dir/index.html",
            nonce="7ypf/xlj9XXwfDPEoM4URrv/xwf94BcCAzFZH4GiTo0v",
            cnonce="f2/wE4q74E6zIJEtWaHKaf5wv/H5QzzpXusqGemxURZJ",
            nc="00000001",
            qop="auth",
        )
        r2 = rtsp.compute_digest_response(
            username="Mufasa",
            password="Circle of Life",
            realm="http-auth@example.org",
            method="GET",
            uri="/dir/index.html",
            nonce="7ypf/xlj9XXwfDPEoM4URrv/xwf94BcCAzFZH4GiTo0v",
            cnonce="f2/wE4q74E6zIJEtWaHKaf5wv/H5QzzpXusqGemxURZJ",
            nc="00000001",
            qop="auth",
        )
        assert r1 == r2  # deterministic
        assert len(r1) == 32  # MD5 hex

    def test_digest_roundtrip_through_manager(self, manager):
        nonce = manager.issue_nonce()
        response = rtsp.compute_digest_response(
            username="admin",
            password="hunter2",
            realm="OmniSightTest",
            method="DESCRIBE",
            uri="rtsp://host/live/main",
            nonce=nonce,
            cnonce="cnonceXYZ",
            nc="00000001",
            qop="auth",
        )
        authz = (
            f'Digest username="admin", realm="OmniSightTest", '
            f'nonce="{nonce}", uri="rtsp://host/live/main", '
            f'response="{response}", qop=auth, nc=00000001, '
            f'cnonce="cnonceXYZ", algorithm=MD5'
        )
        raw = (
            b"DESCRIBE rtsp://host/live/main RTSP/1.0\r\n"
            b"CSeq: 1\r\n"
            b"Authorization: " + authz.encode() + b"\r\n\r\n"
        )
        req = rtsp.parse_rtsp_request(raw)
        cred = manager.authenticate(req)
        assert cred is not None
        assert cred.username == "admin"
        assert cred.role == "admin"

    def test_digest_missing_authorization(self, manager):
        req = rtsp.parse_rtsp_request(
            b"DESCRIBE rtsp://host/live/main RTSP/1.0\r\nCSeq: 1\r\n\r\n"
        )
        with pytest.raises(rtsp.RTSPAuthError, match="Missing"):
            manager.authenticate(req)

    def test_digest_wrong_password(self, manager):
        nonce = manager.issue_nonce()
        bad = rtsp.compute_digest_response(
            username="admin",
            password="WRONG",
            realm="OmniSightTest",
            method="DESCRIBE",
            uri="rtsp://host/live/main",
            nonce=nonce,
            cnonce="c",
            nc="00000001",
            qop="auth",
        )
        authz = (
            f'Digest username="admin", realm="OmniSightTest", '
            f'nonce="{nonce}", uri="rtsp://host/live/main", '
            f'response="{bad}", qop=auth, nc=00000001, cnonce="c"'
        )
        req = rtsp.parse_rtsp_request(
            b"DESCRIBE rtsp://host/live/main RTSP/1.0\r\n"
            b"CSeq: 1\r\n"
            b"Authorization: " + authz.encode() + b"\r\n\r\n"
        )
        with pytest.raises(rtsp.RTSPAuthError, match="mismatch"):
            manager.authenticate(req)

    def test_digest_unknown_user(self, manager):
        nonce = manager.issue_nonce()
        authz = (
            f'Digest username="ghost", realm="OmniSightTest", '
            f'nonce="{nonce}", uri="rtsp://host/", '
            f'response="00", qop=auth, nc=00000001, cnonce="c"'
        )
        req = rtsp.parse_rtsp_request(
            b"DESCRIBE rtsp://host/live/main RTSP/1.0\r\n"
            b"CSeq: 1\r\n"
            b"Authorization: " + authz.encode() + b"\r\n\r\n"
        )
        with pytest.raises(rtsp.RTSPAuthError, match="Unknown user"):
            manager.authenticate(req)

    def test_digest_realm_mismatch(self, manager):
        nonce = manager.issue_nonce()
        authz = (
            f'Digest username="admin", realm="WrongRealm", '
            f'nonce="{nonce}", uri="rtsp://host/", '
            f'response="00", qop=auth, nc=00000001, cnonce="c"'
        )
        req = rtsp.parse_rtsp_request(
            b"DESCRIBE rtsp://host/live/main RTSP/1.0\r\n"
            b"CSeq: 1\r\n"
            b"Authorization: " + authz.encode() + b"\r\n\r\n"
        )
        with pytest.raises(rtsp.RTSPAuthError, match="Realm"):
            manager.authenticate(req)

    def test_digest_invalid_nonce(self, manager):
        authz = (
            'Digest username="admin", realm="OmniSightTest", '
            'nonce="unknownfakenonce", uri="rtsp://host/", '
            'response="00", qop=auth, nc=00000001, cnonce="c"'
        )
        req = rtsp.parse_rtsp_request(
            b"DESCRIBE rtsp://host/ RTSP/1.0\r\n"
            b"CSeq: 1\r\n"
            b"Authorization: " + authz.encode() + b"\r\n\r\n"
        )
        with pytest.raises(rtsp.RTSPAuthError, match="Invalid nonce"):
            manager.authenticate(req)

    def test_basic_auth_success(self, h264_mount):
        import base64
        cfg = rtsp.RTSPServerConfig(
            backend=rtsp.RTSPBackend.STUB,
            auth_scheme=rtsp.AuthScheme.BASIC,
        )
        mgr = rtsp.RTSPServerManager(cfg)
        mgr.add_mount(h264_mount)
        mgr.add_credential("bob", "s3cret", role="viewer")
        mgr.start()
        try:
            token = base64.b64encode(b"bob:s3cret").decode()
            req = rtsp.parse_rtsp_request(
                b"DESCRIBE rtsp://host/live/main RTSP/1.0\r\n"
                b"CSeq: 1\r\n"
                b"Authorization: Basic " + token.encode() + b"\r\n\r\n"
            )
            cred = mgr.authenticate(req)
            assert cred.username == "bob"
        finally:
            mgr.stop()

    def test_basic_auth_wrong_password(self, h264_mount):
        import base64
        cfg = rtsp.RTSPServerConfig(
            backend=rtsp.RTSPBackend.STUB,
            auth_scheme=rtsp.AuthScheme.BASIC,
        )
        mgr = rtsp.RTSPServerManager(cfg)
        mgr.add_mount(h264_mount)
        mgr.add_credential("bob", "s3cret", role="viewer")
        mgr.start()
        try:
            token = base64.b64encode(b"bob:WRONG").decode()
            req = rtsp.parse_rtsp_request(
                b"DESCRIBE rtsp://host/live/main RTSP/1.0\r\n"
                b"CSeq: 1\r\n"
                b"Authorization: Basic " + token.encode() + b"\r\n\r\n"
            )
            with pytest.raises(rtsp.RTSPAuthError):
                mgr.authenticate(req)
        finally:
            mgr.stop()

    def test_credential_empty_rejected(self):
        with pytest.raises(ValueError):
            rtsp.Credential(username="", password="p")
        with pytest.raises(ValueError):
            rtsp.Credential(username="u", password="")

    def test_remove_credential(self, manager):
        ok = manager.remove_credential("admin")
        assert ok is True
        ok = manager.remove_credential("admin")  # already gone
        assert ok is False

    def test_nonce_nc_monotonic(self, manager):
        """Replay with same nc must fail the second time."""
        nonce = manager.issue_nonce()
        # First call succeeds
        valid, _ = manager._nonce_store.verify(nonce, "00000001")
        assert valid is True
        # Same nc must now fail
        valid2, _ = manager._nonce_store.verify(nonce, "00000001")
        assert valid2 is False


# ═══════════════════════════════════════════════════════════════════════
# rtsp_sdp — SDP generation
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.rtsp_sdp
class TestSDPGeneration:
    def test_h264_sdp_shape(self, h264_mount):
        sdp = rtsp.build_sdp(h264_mount, "10.0.0.1", "1234", ntp_start=111)
        assert sdp.startswith("v=0\r\n")
        assert "o=- 1234 111 IN IP4 10.0.0.1" in sdp
        assert f"m=video 0 RTP/AVP {rtsp.PAYLOAD_TYPE_H264}" in sdp
        assert f"a=rtpmap:{rtsp.PAYLOAD_TYPE_H264} H264/90000" in sdp
        assert "profile-level-id=42001F" in sdp
        assert "sprop-parameter-sets=Z0KAH9oBQBboQAAA,aM48gA==" in sdp
        assert "packetization-mode=1" in sdp

    def test_h264_sdp_framerate(self, h264_mount):
        sdp = rtsp.build_sdp(h264_mount, "10.0.0.1", "1234")
        assert "a=framerate:30" in sdp

    def test_h264_sdp_cliprect(self, h264_mount):
        sdp = rtsp.build_sdp(h264_mount, "10.0.0.1", "1234")
        assert "a=cliprect:0,0,1080,1920" in sdp

    def test_h265_sdp_shape(self, h265_mount):
        sdp = rtsp.build_sdp(h265_mount, "10.0.0.1", "1234")
        assert f"m=video 0 RTP/AVP {rtsp.PAYLOAD_TYPE_H265}" in sdp
        assert f"a=rtpmap:{rtsp.PAYLOAD_TYPE_H265} H265/90000" in sdp
        assert "sprop-vps=QAEMAf//" in sdp
        assert "sprop-sps=QgEBAWAAAA==" in sdp
        assert "sprop-pps=RAHAjA==" in sdp

    def test_h265_sdp_no_h264_fields(self, h265_mount):
        sdp = rtsp.build_sdp(h265_mount, "10.0.0.1", "1234")
        assert "sprop-parameter-sets=" not in sdp
        assert "packetization-mode=1" not in sdp

    def test_sdp_has_crlf_lines(self, h264_mount):
        sdp = rtsp.build_sdp(h264_mount, "10.0.0.1", "1234")
        # Every line (including terminator) must end with CRLF
        assert sdp.endswith("\r\n")
        for line in sdp.split("\r\n")[:-1]:
            assert "\n" not in line  # no bare LF inside a line


# ═══════════════════════════════════════════════════════════════════════
# rtsp_error — bad-input / boundary cases
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.rtsp_error
class TestErrorHandling:
    def test_parse_empty_request(self):
        with pytest.raises(rtsp.RTSPBadRequest):
            rtsp.parse_rtsp_request(b"")

    def test_parse_malformed_request_line(self):
        with pytest.raises(rtsp.RTSPBadRequest):
            rtsp.parse_rtsp_request(b"garbage\r\n\r\n")

    def test_parse_missing_cseq(self):
        with pytest.raises(rtsp.RTSPBadRequest, match="CSeq"):
            rtsp.parse_rtsp_request(
                b"OPTIONS rtsp://h/ RTSP/1.0\r\nNo-CSeq: 1\r\n\r\n"
            )

    def test_parse_invalid_cseq(self):
        with pytest.raises(rtsp.RTSPBadRequest, match="CSeq"):
            rtsp.parse_rtsp_request(
                b"OPTIONS rtsp://h/ RTSP/1.0\r\nCSeq: notanumber\r\n\r\n"
            )

    def test_parse_malformed_header(self):
        with pytest.raises(rtsp.RTSPBadRequest, match="header"):
            rtsp.parse_rtsp_request(
                b"OPTIONS rtsp://h/ RTSP/1.0\r\nCSeq: 1\r\nNoColonHeader\r\n\r\n"
            )

    def test_handle_request_while_stopped(self, stub_config, h264_mount):
        mgr = rtsp.RTSPServerManager(stub_config)
        mgr.add_mount(h264_mount)
        # Not started
        req = rtsp.parse_rtsp_request(
            b"OPTIONS rtsp://h/ RTSP/1.0\r\nCSeq: 1\r\n\r\n"
        )
        with pytest.raises(rtsp.RTSPError, match="not running"):
            mgr.handle_request(req)

    def test_setup_without_transport(self, stub_config, h264_mount):
        cfg = rtsp.RTSPServerConfig(
            backend=rtsp.RTSPBackend.STUB,
            auth_scheme=rtsp.AuthScheme.NONE,
        )
        mgr = rtsp.RTSPServerManager(cfg)
        mgr.add_mount(h264_mount)
        mgr.start()
        try:
            req = rtsp.parse_rtsp_request(
                b"SETUP rtsp://h/live/main RTSP/1.0\r\nCSeq: 3\r\n\r\n"
            )
            with pytest.raises(rtsp.RTSPBadRequest, match="Transport"):
                mgr.handle_request(req)
        finally:
            mgr.stop()

    def test_session_header_missing(self, stub_config, h264_mount):
        cfg = rtsp.RTSPServerConfig(
            backend=rtsp.RTSPBackend.STUB,
            auth_scheme=rtsp.AuthScheme.NONE,
        )
        mgr = rtsp.RTSPServerManager(cfg)
        mgr.add_mount(h264_mount)
        mgr.start()
        try:
            req = rtsp.parse_rtsp_request(
                b"PLAY rtsp://h/live/main RTSP/1.0\r\nCSeq: 4\r\n\r\n"
            )
            with pytest.raises(rtsp.RTSPBadRequest, match="Session"):
                mgr.handle_request(req)
        finally:
            mgr.stop()

    def test_basic_header_malformed(self):
        with pytest.raises(rtsp.RTSPAuthError):
            rtsp._parse_basic_header("Basic not_base64!!")
        with pytest.raises(rtsp.RTSPAuthError):
            rtsp._parse_basic_header("Basic " + __import__("base64").b64encode(b"nocolon").decode())

    def test_double_start_is_idempotent(self, manager):
        b1 = manager.start()  # manager fixture already started once
        b2 = manager.start()
        assert b1 == b2

    def test_stop_before_start_is_noop(self, stub_config):
        mgr = rtsp.RTSPServerManager(stub_config)
        mgr.stop()  # must not raise

    def test_push_access_unit_unknown_mount(self, manager):
        with pytest.raises(rtsp.RTSPMountNotFound):
            manager.push_access_unit("ghost", [b"\x00\x00\x00\x01foo"], 0)

    def test_push_and_drain(self, manager):
        manager.push_access_unit("live/main", [b"\x00\x00\x00\x01AU1"], 900)
        manager.push_access_unit("live/main", [b"\x00\x00\x00\x01AU2"], 1800)
        drained = manager.drain_access_units("live/main")
        assert len(drained) == 2
        assert drained[0][1] == 900
        assert drained[1][1] == 1800
        # Drained cache is now empty
        assert manager.drain_access_units("live/main") == []

    def test_status_shape(self, manager):
        status = manager.status()
        assert status["running"] is True
        assert status["backend"] == rtsp.RTSPBackend.STUB.value
        assert "live/main" in status["mounts"]
        assert status["credential_count"] == 1
        assert status["auth_scheme"] == "digest"
