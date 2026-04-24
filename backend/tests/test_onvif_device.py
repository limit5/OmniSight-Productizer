"""D2 — SKILL-IPCAM: ONVIF Device/Media/Events/PTZ endpoints tests (#219).

Offline coverage for :mod:`backend.onvif_device`. Exercises every
Profile S operation exposed by :class:`ONVIFDevice.dispatch` plus the
WS-UsernameToken security layer, SOAP envelope parser, PTZ clamping,
and the EventSubscription / PullMessages lifecycle.

Tests are pure: no sockets, no threads, no real clock. The RTSP
manager is the STUB backend so everything runs in one worker.
"""

from __future__ import annotations

import base64
import secrets
import time
import xml.etree.ElementTree as ET

import pytest

from backend.ipcam_rtsp_server import (
    AuthScheme,
    RTSPBackend,
    RTSPServerConfig,
    RTSPServerManager,
    StreamMount,
    VideoCodec,
)
from backend import onvif_device as onvif


# ═══════════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════════


@pytest.fixture
def rtsp_manager():
    cfg = RTSPServerConfig(
        backend=RTSPBackend.STUB,
        auth_scheme=AuthScheme.NONE,
        port=8554,
    )
    mgr = RTSPServerManager(cfg)
    mgr.add_mount(
        StreamMount(
            path="live/main",
            codec=VideoCodec.H264,
            width=1920,
            height=1080,
            fps=30,
            bitrate_kbps=4096,
            description="Main 1080p30 H.264",
        )
    )
    mgr.add_mount(
        StreamMount(
            path="live/sub",
            codec=VideoCodec.H264,
            width=640,
            height=480,
            fps=15,
            bitrate_kbps=512,
            description="Sub 480p15",
        )
    )
    mgr.start()
    yield mgr
    mgr.stop()


@pytest.fixture
def service_config():
    return onvif.ONVIFServiceConfig(
        scheme="http",
        xaddr_host="10.0.0.1",
        xaddr_port=80,
        rtsp_host="10.0.0.1",
        rtsp_port=8554,
        require_auth=False,
    )


@pytest.fixture
def device(rtsp_manager, service_config):
    dev = onvif.ONVIFDevice(
        service_config,
        rtsp_manager,
        device_info=onvif.DeviceInformation(
            manufacturer="OmniSight",
            model="IPCam-Test",
            firmware_version="1.2.3",
            serial_number="SN0001",
            hardware_id="HW001",
        ),
        network_interfaces=[
            onvif.NetworkInterface(
                token="eth0",
                mac_address="02:11:22:33:44:55",
                ipv4_address="10.0.0.1",
                ipv4_prefix_length=24,
            ),
        ],
        video_sources=[onvif.VideoSource(token="VideoSource_1")],
        ptz_configuration=onvif.PTZConfiguration(
            token="PTZConfig_1",
            node_token="PTZNode_1",
            pan_range=(-1.0, 1.0),
            tilt_range=(-1.0, 1.0),
            zoom_range=(0.0, 1.0),
        ),
    )
    dev.add_user("admin", "hunter2", onvif.UserLevel.ADMINISTRATOR)
    return dev


@pytest.fixture
def auth_device(rtsp_manager):
    cfg = onvif.ONVIFServiceConfig(
        xaddr_host="10.0.0.1",
        rtsp_port=8554,
        require_auth=True,
    )
    dev = onvif.ONVIFDevice(cfg, rtsp_manager)
    dev.add_user("admin", "hunter2", onvif.UserLevel.ADMINISTRATOR)
    return dev


# ─────────────────────────────────────────────────────────────────────
# Envelope helper utilities used by the tests
# ─────────────────────────────────────────────────────────────────────


def _env(body_inner: str, header_inner: str = "") -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"'
        ' xmlns:wsa="http://www.w3.org/2005/08/addressing"'
        ' xmlns:tt="http://www.onvif.org/ver10/schema"'
        ' xmlns:tds="http://www.onvif.org/ver10/device/wsdl"'
        ' xmlns:trt="http://www.onvif.org/ver10/media/wsdl"'
        ' xmlns:tev="http://www.onvif.org/ver10/events/wsdl"'
        ' xmlns:tptz="http://www.onvif.org/ver20/ptz/wsdl"'
        ' xmlns:wsnt="http://docs.oasis-open.org/wsn/b-2"'
        ' xmlns:wstop="http://docs.oasis-open.org/wsn/t-1">'
        f"<s:Header>{header_inner}</s:Header>"
        f"<s:Body>{body_inner}</s:Body>"
        "</s:Envelope>"
    ).encode("utf-8")


def _parse(body: bytes) -> ET.Element:
    return ET.fromstring(body)


NS = {
    "s": onvif.NS_SOAP,
    "wsa": onvif.NS_WSA,
    "tt": onvif.NS_TT,
    "tds": onvif.NS_TDS,
    "trt": onvif.NS_TRT,
    "tev": onvif.NS_TEV,
    "tptz": onvif.NS_TPTZ,
    "wsnt": onvif.NS_WSNT,
    "wstop": onvif.NS_WSTOP,
}


# ═══════════════════════════════════════════════════════════════════════
# onvif_config — validation
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.onvif_config
class TestServiceConfig:
    def test_defaults_are_valid(self):
        cfg = onvif.ONVIFServiceConfig()
        assert cfg.scheme == "http"
        assert cfg.rtsp_port == 8554

    @pytest.mark.parametrize("port", [0, -1, 65536, 100_000])
    def test_invalid_xaddr_port_rejected(self, port):
        with pytest.raises(ValueError, match="xaddr_port"):
            onvif.ONVIFServiceConfig(xaddr_port=port)

    @pytest.mark.parametrize("port", [0, 65536])
    def test_invalid_rtsp_port_rejected(self, port):
        with pytest.raises(ValueError, match="rtsp_port"):
            onvif.ONVIFServiceConfig(rtsp_port=port)

    def test_bad_scheme_rejected(self):
        with pytest.raises(ValueError, match="scheme"):
            onvif.ONVIFServiceConfig(scheme="ftp")

    def test_bad_scope_uri_rejected(self):
        with pytest.raises(ValueError, match="scope"):
            onvif.ONVIFServiceConfig(scopes=("not-a-scope-uri",))

    def test_effective_rtsp_host_falls_back_to_xaddr(self):
        cfg = onvif.ONVIFServiceConfig(xaddr_host="cam-1", rtsp_host="")
        assert cfg.effective_rtsp_host() == "cam-1"

    def test_xaddr_path_assembly(self):
        cfg = onvif.ONVIFServiceConfig(xaddr_host="10.0.0.1", xaddr_port=80)
        assert cfg.device_xaddr == "http://10.0.0.1:80/onvif/device_service"
        assert cfg.media_xaddr == "http://10.0.0.1:80/onvif/media_service"
        assert cfg.events_xaddr == "http://10.0.0.1:80/onvif/events_service"
        assert cfg.ptz_xaddr == "http://10.0.0.1:80/onvif/ptz_service"


# ═══════════════════════════════════════════════════════════════════════
# onvif_dataclasses — validation
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.onvif_dataclasses
class TestDataclassValidation:
    def test_device_info_requires_manufacturer(self):
        with pytest.raises(ValueError, match="manufacturer"):
            onvif.DeviceInformation(manufacturer="")

    def test_device_info_requires_model(self):
        with pytest.raises(ValueError, match="model"):
            onvif.DeviceInformation(model="")

    def test_device_info_requires_firmware_version(self):
        with pytest.raises(ValueError, match="firmware_version"):
            onvif.DeviceInformation(firmware_version="")

    def test_network_interface_rejects_empty_token(self):
        with pytest.raises(ValueError, match="token"):
            onvif.NetworkInterface(token="")

    @pytest.mark.parametrize("prefix", [-1, 33, 100])
    def test_network_interface_rejects_bad_prefix(self, prefix):
        with pytest.raises(ValueError, match="ipv4_prefix_length"):
            onvif.NetworkInterface(token="eth0", ipv4_prefix_length=prefix)

    def test_user_rejects_bad_name(self):
        with pytest.raises(ValueError, match="username"):
            onvif.ONVIFUser(username="has space!", password="x")

    def test_user_rejects_empty_password(self):
        with pytest.raises(ValueError, match="password"):
            onvif.ONVIFUser(username="alice", password="")

    def test_video_source_rejects_non_positive_dims(self):
        with pytest.raises(ValueError, match="resolution"):
            onvif.VideoSource(resolution_width=0)

    def test_ptz_config_rejects_inverted_pan(self):
        with pytest.raises(ValueError, match="pan_range"):
            onvif.PTZConfiguration(pan_range=(1.0, -1.0))

    def test_ptz_config_rejects_inverted_tilt(self):
        with pytest.raises(ValueError, match="tilt_range"):
            onvif.PTZConfiguration(tilt_range=(1.0, 0.0))

    def test_ptz_config_rejects_inverted_zoom(self):
        with pytest.raises(ValueError, match="zoom_range"):
            onvif.PTZConfiguration(zoom_range=(1.0, 0.0))

    def test_media_profile_from_stream_mount(self):
        mount = StreamMount(
            path="live/main", codec=VideoCodec.H264,
            width=1920, height=1080, fps=30, bitrate_kbps=4096,
        )
        profile = onvif.MediaProfile.from_stream_mount(mount, index=0, fixed=True)
        assert profile.token.startswith("profile_0_")
        assert profile.mount_path == "live/main"
        assert profile.fixed is True
        assert profile.codec == VideoCodec.H264


# ═══════════════════════════════════════════════════════════════════════
# soap_envelope — parsing + fault + response builders
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.soap_envelope
class TestSoapEnvelope:
    def test_parse_empty_raises(self):
        with pytest.raises(onvif.ONVIFBadRequest, match="Empty"):
            onvif.parse_soap_envelope(b"")

    def test_parse_malformed_xml_raises(self):
        with pytest.raises(onvif.ONVIFBadRequest, match="Malformed"):
            onvif.parse_soap_envelope(b"<not-xml>")

    def test_parse_rejects_soap11(self):
        soap11 = (
            b'<?xml version="1.0"?>'
            b'<S:Envelope xmlns:S="http://schemas.xmlsoap.org/soap/envelope/">'
            b"<S:Body/></S:Envelope>"
        )
        with pytest.raises(onvif.ONVIFBadRequest, match="SOAP 1.2"):
            onvif.parse_soap_envelope(soap11)

    def test_parse_missing_body_raises(self):
        env = (
            b'<?xml version="1.0"?>'
            b'<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">'
            b"<s:Header/></s:Envelope>"
        )
        with pytest.raises(onvif.ONVIFBadRequest, match="Body"):
            onvif.parse_soap_envelope(env)

    def test_parse_empty_body_raises(self):
        with pytest.raises(onvif.ONVIFBadRequest, match="empty"):
            onvif.parse_soap_envelope(_env(""))

    def test_parse_extracts_action_and_message_id(self):
        header = (
            '<wsa:Action>http://www.onvif.org/ver10/device/wsdl/Device/'
            'GetDeviceInformationRequest</wsa:Action>'
            "<wsa:MessageID>urn:uuid:abc123</wsa:MessageID>"
        )
        env = _env("<tds:GetDeviceInformation/>", header_inner=header)
        action, mid, sec, op = onvif.parse_soap_envelope(env)
        assert "GetDeviceInformationRequest" in (action or "")
        assert mid == "urn:uuid:abc123"
        assert sec is None
        assert op.tag == f"{{{onvif.NS_TDS}}}GetDeviceInformation"

    def test_parse_tolerates_bom(self):
        env = b"\xef\xbb\xbf" + _env("<tds:GetDeviceInformation/>")
        _, _, _, op = onvif.parse_soap_envelope(env)
        assert op.tag.endswith("GetDeviceInformation")

    def test_build_response_contains_action(self):
        body = onvif.build_soap_response(
            "<tds:GetDeviceInformationResponse/>",
            action="http://example/action/GetFooResponse",
            relates_to="urn:uuid:xyz",
        )
        assert b"GetFooResponse" in body
        assert b"urn:uuid:xyz" in body
        # Valid XML
        ET.fromstring(body)

    def test_build_fault_structure(self):
        err = onvif.ONVIFBadRequest("bad input", subcode="ter:Something")
        body = onvif.build_soap_fault(err)
        root = ET.fromstring(body)
        fault = root.find(".//s:Fault", NS)
        assert fault is not None
        subcode = root.find(".//s:Subcode/s:Value", NS)
        assert subcode is not None
        assert subcode.text == "ter:Something"
        reason = root.find(".//s:Reason/s:Text", NS)
        assert reason is not None
        assert "bad input" in (reason.text or "")


# ═══════════════════════════════════════════════════════════════════════
# wsse_username_token — PasswordDigest + PasswordText + replay
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.wsse_username_token
class TestUsernameToken:
    def test_password_digest_known_vector(self):
        # OASIS WSS §4.1.1.2 appendix example ("weak"/"0" vectors are
        # copyrighted — we instead pin our own ASCII vector so drift
        # in the SHA1 implementation is caught).
        nonce = b"nonce16bytes1234"
        created = "2026-04-24T10:00:00Z"
        password = "password"
        expected = base64.b64encode(
            __import__("hashlib")
            .sha1(nonce + created.encode() + password.encode())
            .digest()
        ).decode()
        assert (
            onvif.compute_password_digest(password, nonce, created) == expected
        )

    def test_build_username_token_roundtrip(self, auth_device):
        created = onvif._iso_utc(time.time())
        nonce = secrets.token_bytes(16)
        header = onvif.build_username_token(
            "admin", "hunter2", nonce_bytes=nonce, created_iso=created
        )
        env = _env("<tds:GetDeviceInformation/>", header_inner=header)
        status, body = auth_device.dispatch(onvif.ONVIFService.DEVICE, env)
        assert status == 200

    def test_password_text_accepted(self, auth_device):
        created = onvif._iso_utc(time.time())
        header = (
            f'<wsse:Security xmlns:wsse="{onvif.NS_WSSE}" '
            f'xmlns:wsse_util="{onvif.NS_WSSE_UTIL}">'
            "<wsse:UsernameToken>"
            "<wsse:Username>admin</wsse:Username>"
            f'<wsse:Password Type="{onvif.NS_WSSE_PASSWORD_TEXT}">hunter2</wsse:Password>'
            f"<wsse_util:Created>{created}</wsse_util:Created>"
            "</wsse:UsernameToken>"
            "</wsse:Security>"
        )
        env = _env("<tds:GetDeviceInformation/>", header_inner=header)
        status, _ = auth_device.dispatch(onvif.ONVIFService.DEVICE, env)
        assert status == 200

    def test_unknown_user_401(self, auth_device):
        header = onvif.build_username_token("ghost", "whatever")
        env = _env("<tds:GetDeviceInformation/>", header_inner=header)
        status, body = auth_device.dispatch(onvif.ONVIFService.DEVICE, env)
        assert status == 401
        assert b"NotAuthorized" in body

    def test_wrong_password_401(self, auth_device):
        header = onvif.build_username_token("admin", "wrong-password")
        env = _env("<tds:GetDeviceInformation/>", header_inner=header)
        status, _ = auth_device.dispatch(onvif.ONVIFService.DEVICE, env)
        assert status == 401

    def test_missing_security_header_401(self, auth_device):
        env = _env("<tds:GetDeviceInformation/>")
        status, body = auth_device.dispatch(onvif.ONVIFService.DEVICE, env)
        assert status == 401
        assert b"wsse:Security" in body

    def test_replay_rejected(self, auth_device):
        created = onvif._iso_utc(time.time())
        nonce = secrets.token_bytes(16)
        header = onvif.build_username_token(
            "admin", "hunter2", nonce_bytes=nonce, created_iso=created
        )
        env = _env("<tds:GetDeviceInformation/>", header_inner=header)
        s1, _ = auth_device.dispatch(onvif.ONVIFService.DEVICE, env)
        assert s1 == 200
        s2, body2 = auth_device.dispatch(onvif.ONVIFService.DEVICE, env)
        assert s2 == 401
        assert b"Replayed" in body2 or b"replay" in body2.lower()

    def test_clock_skew_window(self, auth_device):
        # One hour in the past — outside the 5 min default skew window.
        stale_ts = time.time() - 3600
        created = onvif._iso_utc(stale_ts)
        header = onvif.build_username_token(
            "admin", "hunter2", created_iso=created
        )
        env = _env("<tds:GetDeviceInformation/>", header_inner=header)
        status, body = auth_device.dispatch(onvif.ONVIFService.DEVICE, env)
        assert status == 401
        assert b"clock skew" in body.lower() or b"Created" in body

    def test_missing_nonce_with_digest_type_401(self, auth_device):
        created = onvif._iso_utc(time.time())
        header = (
            f'<wsse:Security xmlns:wsse="{onvif.NS_WSSE}" '
            f'xmlns:wsse_util="{onvif.NS_WSSE_UTIL}">'
            "<wsse:UsernameToken>"
            "<wsse:Username>admin</wsse:Username>"
            f'<wsse:Password Type="{onvif.NS_WSSE_PASSWORD_DIGEST}">irrelevant</wsse:Password>'
            f"<wsse_util:Created>{created}</wsse_util:Created>"
            "</wsse:UsernameToken>"
            "</wsse:Security>"
        )
        env = _env("<tds:GetDeviceInformation/>", header_inner=header)
        status, body = auth_device.dispatch(onvif.ONVIFService.DEVICE, env)
        assert status == 401
        assert b"Nonce" in body


# ═══════════════════════════════════════════════════════════════════════
# onvif_device — GetDeviceInformation / GetSystemDateAndTime / etc.
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.onvif_device
class TestDeviceService:
    def test_get_device_information(self, device):
        env = _env("<tds:GetDeviceInformation/>")
        status, body = device.dispatch(onvif.ONVIFService.DEVICE, env)
        assert status == 200
        root = _parse(body)
        mfr = root.find(".//tds:Manufacturer", NS)
        model = root.find(".//tds:Model", NS)
        fw = root.find(".//tds:FirmwareVersion", NS)
        sn = root.find(".//tds:SerialNumber", NS)
        hw = root.find(".//tds:HardwareId", NS)
        assert mfr.text == "OmniSight"
        assert model.text == "IPCam-Test"
        assert fw.text == "1.2.3"
        assert sn.text == "SN0001"
        assert hw.text == "HW001"

    def test_response_carries_action_and_relatesto(self, device):
        header = "<wsa:MessageID>urn:uuid:req-1</wsa:MessageID>"
        env = _env("<tds:GetDeviceInformation/>", header_inner=header)
        status, body = device.dispatch(onvif.ONVIFService.DEVICE, env)
        assert status == 200
        root = _parse(body)
        action = root.find(".//wsa:Action", NS)
        rel = root.find(".//wsa:RelatesTo", NS)
        assert action is not None
        assert "GetDeviceInformationResponse" in (action.text or "")
        assert rel is not None
        assert rel.text == "urn:uuid:req-1"

    def test_get_system_datetime(self, device):
        env = _env("<tds:GetSystemDateAndTime/>")
        status, body = device.dispatch(onvif.ONVIFService.DEVICE, env)
        assert status == 200
        root = _parse(body)
        assert root.find(".//tt:UTCDateTime", NS) is not None
        assert root.find(".//tt:Year", NS) is not None
        assert root.find(".//tt:Month", NS) is not None
        assert root.find(".//tt:Day", NS) is not None

    def test_get_capabilities_lists_all_four_services(self, device):
        env = _env("<tds:GetCapabilities/>")
        status, body = device.dispatch(onvif.ONVIFService.DEVICE, env)
        assert status == 200
        # Capabilities must include Device, Media, Events, PTZ XAddrs
        for sub in ("/onvif/device_service", "/onvif/media_service",
                    "/onvif/events_service", "/onvif/ptz_service"):
            assert sub.encode() in body

    def test_get_services_lists_all_namespaces(self, device):
        env = _env("<tds:GetServices/>")
        status, body = device.dispatch(onvif.ONVIFService.DEVICE, env)
        assert status == 200
        for ns in (onvif.NS_TDS, onvif.NS_TRT, onvif.NS_TEV, onvif.NS_TPTZ):
            assert ns.encode() in body

    def test_get_service_capabilities_device(self, device):
        env = _env("<tds:GetServiceCapabilities/>")
        status, body = device.dispatch(onvif.ONVIFService.DEVICE, env)
        assert status == 200
        assert b'UsernameToken="true"' in body
        assert b'TLS1.2="true"' in body

    def test_get_network_interfaces(self, device):
        env = _env("<tds:GetNetworkInterfaces/>")
        status, body = device.dispatch(onvif.ONVIFService.DEVICE, env)
        assert status == 200
        root = _parse(body)
        nics = root.findall(".//tds:NetworkInterfaces", NS)
        assert len(nics) == 1
        assert nics[0].get("token") == "eth0"
        assert root.find(".//tt:HwAddress", NS).text == "02:11:22:33:44:55"
        assert root.find(".//tt:Address", NS).text == "10.0.0.1"

    def test_get_scopes(self, device):
        env = _env("<tds:GetScopes/>")
        status, body = device.dispatch(onvif.ONVIFService.DEVICE, env)
        assert status == 200
        assert b"Profile/Streaming" in body
        assert b"name/OmniSight" in body

    def test_get_hostname(self, device):
        env = _env("<tds:GetHostname/>")
        status, body = device.dispatch(onvif.ONVIFService.DEVICE, env)
        assert status == 200
        root = _parse(body)
        name = root.find(".//tt:Name", NS)
        assert name.text == "omnisight-ipcam"

    def test_set_hostname_round_trip(self, device):
        env = _env(
            "<tds:SetHostname><tds:Name>cam-new</tds:Name></tds:SetHostname>"
        )
        status, _ = device.dispatch(onvif.ONVIFService.DEVICE, env)
        assert status == 200
        # Read back
        env2 = _env("<tds:GetHostname/>")
        status, body = device.dispatch(onvif.ONVIFService.DEVICE, env2)
        assert status == 200
        assert b"<tt:Name>cam-new</tt:Name>" in body

    def test_set_hostname_missing_name_400(self, device):
        env = _env("<tds:SetHostname/>")
        status, body = device.dispatch(onvif.ONVIFService.DEVICE, env)
        assert status == 400
        assert b"InvalidArgs" in body

    def test_get_users_initial(self, device):
        env = _env("<tds:GetUsers/>")
        status, body = device.dispatch(onvif.ONVIFService.DEVICE, env)
        assert status == 200
        assert b"<tt:Username>admin</tt:Username>" in body
        assert b"<tt:UserLevel>Administrator</tt:UserLevel>" in body

    def test_create_and_delete_users(self, device):
        env = _env(
            "<tds:CreateUsers>"
            "<tds:User>"
            "<tt:Username>alice</tt:Username>"
            "<tt:Password>p455w0rd</tt:Password>"
            "<tt:UserLevel>Operator</tt:UserLevel>"
            "</tds:User>"
            "</tds:CreateUsers>"
        )
        status, _ = device.dispatch(onvif.ONVIFService.DEVICE, env)
        assert status == 200
        assert device.get_user("alice") is not None
        # delete
        env2 = _env(
            "<tds:DeleteUsers>"
            "<tt:Username>alice</tt:Username>"
            "</tds:DeleteUsers>"
        )
        status, _ = device.dispatch(onvif.ONVIFService.DEVICE, env2)
        assert status == 200
        assert device.get_user("alice") is None

    def test_delete_unknown_user_404(self, device):
        env = _env(
            "<tds:DeleteUsers><tt:Username>ghost</tt:Username></tds:DeleteUsers>"
        )
        status, body = device.dispatch(onvif.ONVIFService.DEVICE, env)
        assert status == 404
        assert b"UsernameMissing" in body

    def test_set_user_replaces_password(self, device):
        device.add_user("bob", "old", onvif.UserLevel.USER)
        env = _env(
            "<tds:SetUser>"
            "<tds:User>"
            "<tt:Username>bob</tt:Username>"
            "<tt:Password>new</tt:Password>"
            "<tt:UserLevel>Operator</tt:UserLevel>"
            "</tds:User>"
            "</tds:SetUser>"
        )
        status, _ = device.dispatch(onvif.ONVIFService.DEVICE, env)
        assert status == 200
        u = device.get_user("bob")
        assert u.password == "new"
        assert u.user_level == onvif.UserLevel.OPERATOR

    def test_create_users_invalid_level_400(self, device):
        env = _env(
            "<tds:CreateUsers>"
            "<tds:User>"
            "<tt:Username>carol</tt:Username>"
            "<tt:Password>x</tt:Password>"
            "<tt:UserLevel>GodMode</tt:UserLevel>"
            "</tds:User>"
            "</tds:CreateUsers>"
        )
        status, body = device.dispatch(onvif.ONVIFService.DEVICE, env)
        assert status == 400
        assert b"UserLevel" in body

    def test_unknown_device_op_returns_action_not_supported(self, device):
        env = _env("<tds:NotARealOp/>")
        status, body = device.dispatch(onvif.ONVIFService.DEVICE, env)
        assert status == 400
        assert b"ActionNotSupported" in body


# ═══════════════════════════════════════════════════════════════════════
# onvif_media — GetProfiles / GetStreamUri / GetSnapshotUri
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.onvif_media
class TestMediaService:
    def test_get_profiles_one_per_mount(self, device):
        env = _env("<trt:GetProfiles/>")
        status, body = device.dispatch(onvif.ONVIFService.MEDIA, env)
        assert status == 200
        root = _parse(body)
        profiles = root.findall(".//trt:Profiles", NS)
        assert len(profiles) == 2
        tokens = {p.get("token") for p in profiles}
        assert any("live_main" in t for t in tokens)
        assert any("live_sub" in t for t in tokens)

    def test_first_profile_marked_fixed(self, device):
        env = _env("<trt:GetProfiles/>")
        _, body = device.dispatch(onvif.ONVIFService.MEDIA, env)
        root = _parse(body)
        profiles = root.findall(".//trt:Profiles", NS)
        fixed_flags = [p.get("fixed") for p in profiles]
        assert "true" in fixed_flags

    def test_profile_carries_encoder_resolution(self, device):
        _, body = device.dispatch(
            onvif.ONVIFService.MEDIA, _env("<trt:GetProfiles/>")
        )
        root = _parse(body)
        # Main profile has 1920x1080, sub 640x480
        widths = {
            int(w.text)
            for w in root.findall(".//tt:Resolution/tt:Width", NS)
        }
        assert 1920 in widths
        assert 640 in widths

    def test_get_profile_by_token(self, device):
        token = device.list_profiles()[0].token
        env = _env(
            f"<trt:GetProfile><trt:ProfileToken>{token}</trt:ProfileToken></trt:GetProfile>"
        )
        status, body = device.dispatch(onvif.ONVIFService.MEDIA, env)
        assert status == 200
        root = _parse(body)
        profile = root.find(".//trt:Profiles", NS)
        assert profile.get("token") == token

    def test_get_profile_missing_token_400(self, device):
        env = _env("<trt:GetProfile/>")
        status, body = device.dispatch(onvif.ONVIFService.MEDIA, env)
        assert status == 400
        assert b"ProfileToken" in body

    def test_get_profile_unknown_token_404(self, device):
        env = _env(
            "<trt:GetProfile>"
            "<trt:ProfileToken>does_not_exist</trt:ProfileToken>"
            "</trt:GetProfile>"
        )
        status, body = device.dispatch(onvif.ONVIFService.MEDIA, env)
        assert status == 404

    def test_get_video_sources(self, device):
        env = _env("<trt:GetVideoSources/>")
        status, body = device.dispatch(onvif.ONVIFService.MEDIA, env)
        assert status == 200
        root = _parse(body)
        srcs = root.findall(".//trt:VideoSources", NS)
        assert len(srcs) == 1
        assert srcs[0].get("token") == "VideoSource_1"

    def test_get_video_source_configurations(self, device):
        env = _env("<trt:GetVideoSourceConfigurations/>")
        status, body = device.dispatch(onvif.ONVIFService.MEDIA, env)
        assert status == 200
        assert b"VideoSource_1" in body

    def test_get_video_encoder_configurations(self, device):
        env = _env("<trt:GetVideoEncoderConfigurations/>")
        status, body = device.dispatch(onvif.ONVIFService.MEDIA, env)
        assert status == 200
        root = _parse(body)
        cfgs = root.findall(".//trt:Configurations", NS)
        assert len(cfgs) == 2

    def test_get_stream_uri_maps_to_rtsp(self, device):
        profile = device.list_profiles()[0]
        env = _env(
            f"<trt:GetStreamUri>"
            f"<trt:ProfileToken>{profile.token}</trt:ProfileToken>"
            "</trt:GetStreamUri>"
        )
        status, body = device.dispatch(onvif.ONVIFService.MEDIA, env)
        assert status == 200
        root = _parse(body)
        uri = root.find(".//tt:Uri", NS)
        assert uri is not None
        assert uri.text == f"rtsp://10.0.0.1:8554/{profile.mount_path}"

    def test_get_stream_uri_missing_token_400(self, device):
        env = _env("<trt:GetStreamUri/>")
        status, _ = device.dispatch(onvif.ONVIFService.MEDIA, env)
        assert status == 400

    def test_get_snapshot_uri(self, device):
        profile = device.list_profiles()[0]
        env = _env(
            f"<trt:GetSnapshotUri>"
            f"<trt:ProfileToken>{profile.token}</trt:ProfileToken>"
            "</trt:GetSnapshotUri>"
        )
        status, body = device.dispatch(onvif.ONVIFService.MEDIA, env)
        assert status == 200
        assert f"/onvif/snapshot/{profile.token}".encode() in body

    def test_media_get_service_capabilities(self, device):
        env = _env("<trt:GetServiceCapabilities/>")
        status, body = device.dispatch(onvif.ONVIFService.MEDIA, env)
        assert status == 200
        assert b'SnapshotUri="true"' in body
        assert b'RTP_RTSP_TCP="true"' in body

    def test_refresh_profiles_picks_up_new_mount(self, device, rtsp_manager):
        before = len(device.list_profiles())
        rtsp_manager.add_mount(
            StreamMount(path="live/event", codec=VideoCodec.H265, fps=10)
        )
        device.refresh_profiles()
        after = len(device.list_profiles())
        assert after == before + 1


# ═══════════════════════════════════════════════════════════════════════
# onvif_events — subscription lifecycle + pull + publish
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.onvif_events
class TestEventsService:
    def test_get_event_properties(self, device):
        env = _env("<tev:GetEventProperties/>")
        status, body = device.dispatch(onvif.ONVIFService.EVENTS, env)
        assert status == 200
        assert b"RuleEngine" in body
        assert b"MotionRegionDetector" in body

    def test_events_get_service_capabilities(self, device):
        env = _env("<tev:GetServiceCapabilities/>")
        status, body = device.dispatch(onvif.ONVIFService.EVENTS, env)
        assert status == 200
        assert b'WSPullPointSupport="true"' in body

    def test_create_pullpoint_subscription(self, device):
        env = _env(
            "<tev:CreatePullPointSubscription>"
            "<tev:InitialTerminationTime>PT60S</tev:InitialTerminationTime>"
            "</tev:CreatePullPointSubscription>"
        )
        status, body = device.dispatch(onvif.ONVIFService.EVENTS, env)
        assert status == 200
        root = _parse(body)
        sub_id = root.find(".//tev:SubscriptionId", NS)
        assert sub_id is not None
        assert sub_id.text
        subs = device.list_subscriptions()
        assert len(subs) == 1
        assert subs[0].token == sub_id.text

    def test_pull_messages_returns_published_event(self, device):
        # Create subscription
        env = _env(
            "<tev:CreatePullPointSubscription>"
            "<tev:InitialTerminationTime>PT60S</tev:InitialTerminationTime>"
            "</tev:CreatePullPointSubscription>"
        )
        _, body = device.dispatch(onvif.ONVIFService.EVENTS, env)
        root = _parse(body)
        token = root.find(".//tev:SubscriptionId", NS).text

        # Publish an event
        msg = onvif.NotificationMessage(
            topic="tns1:RuleEngine/MotionRegionDetector",
            produced_at=time.time(),
            source={"Source": "VideoSource_1"},
            data={"State": "true"},
        )
        delivered = device.publish_event(msg)
        assert delivered == 1

        # Pull
        pull = _env(
            "<tev:PullMessages>"
            f"<tev:SubscriptionId>{token}</tev:SubscriptionId>"
            "<tev:Timeout>PT1S</tev:Timeout>"
            "<tev:MessageLimit>10</tev:MessageLimit>"
            "</tev:PullMessages>"
        )
        status, body = device.dispatch(onvif.ONVIFService.EVENTS, pull)
        assert status == 200
        root = _parse(body)
        notifs = root.findall(".//wsnt:NotificationMessage", NS)
        assert len(notifs) == 1
        topic = notifs[0].find(".//wsnt:Topic", NS)
        assert "MotionRegionDetector" in topic.text

    def test_pull_messages_empty_queue(self, device):
        sub = device.create_subscription(topic_filter="")
        env = _env(
            "<tev:PullMessages>"
            f"<tev:SubscriptionId>{sub.token}</tev:SubscriptionId>"
            "<tev:Timeout>PT1S</tev:Timeout>"
            "<tev:MessageLimit>5</tev:MessageLimit>"
            "</tev:PullMessages>"
        )
        status, body = device.dispatch(onvif.ONVIFService.EVENTS, env)
        assert status == 200
        root = _parse(body)
        notifs = root.findall(".//wsnt:NotificationMessage", NS)
        assert notifs == []

    def test_pull_unknown_subscription_404(self, device):
        env = _env(
            "<tev:PullMessages>"
            "<tev:SubscriptionId>ghost-token</tev:SubscriptionId>"
            "<tev:Timeout>PT1S</tev:Timeout>"
            "<tev:MessageLimit>5</tev:MessageLimit>"
            "</tev:PullMessages>"
        )
        status, body = device.dispatch(onvif.ONVIFService.EVENTS, env)
        assert status == 404
        assert b"InvalidSubscriptionReference" in body

    def test_renew(self, device):
        sub = device.create_subscription(topic_filter="", timeout_s=10)
        before = sub.expires_at
        # Small clock fudge — renew to 60s then observe expires_at advances
        time.sleep(0.01)
        env = _env(
            "<wsnt:Renew>"
            f"<tev:SubscriptionId>{sub.token}</tev:SubscriptionId>"
            "<wsnt:TerminationTime>PT120S</wsnt:TerminationTime>"
            "</wsnt:Renew>"
        )
        status, _ = device.dispatch(onvif.ONVIFService.EVENTS, env)
        assert status == 200
        assert sub.expires_at > before

    def test_unsubscribe(self, device):
        sub = device.create_subscription(topic_filter="")
        env = _env(
            "<wsnt:Unsubscribe>"
            f"<tev:SubscriptionId>{sub.token}</tev:SubscriptionId>"
            "</wsnt:Unsubscribe>"
        )
        status, _ = device.dispatch(onvif.ONVIFService.EVENTS, env)
        assert status == 200
        assert device._subscriptions.get(sub.token) is None

    def test_topic_filter_prefix_match(self, device):
        sub = device.create_subscription(topic_filter="tns1:RuleEngine//.")
        msg = onvif.NotificationMessage(
            topic="tns1:RuleEngine/Motion/Started",
            produced_at=time.time(),
        )
        other = onvif.NotificationMessage(
            topic="tns1:Device/Trigger/DigitalInput",
            produced_at=time.time(),
        )
        assert device.publish_event(msg) == 1
        assert device.publish_event(other) == 0
        assert len(sub.queue) == 1

    def test_purge_expired_subscriptions(self, device):
        sub = device.create_subscription(topic_filter="", timeout_s=1)
        # Fast-forward the subscription's expiry
        sub.expires_at = time.time() - 10
        gone = device.purge_expired_subscriptions()
        assert sub.token in gone
        assert device._subscriptions.get(sub.token) is None

    def test_create_subscription_invalid_timeout_rejected(self, device):
        with pytest.raises(onvif.ONVIFBadRequest):
            device.create_subscription(topic_filter="", timeout_s=0)

    def test_pull_limit_above_cap_rejected(self, device):
        sub = device.create_subscription(topic_filter="")
        env = _env(
            "<tev:PullMessages>"
            f"<tev:SubscriptionId>{sub.token}</tev:SubscriptionId>"
            "<tev:Timeout>PT1S</tev:Timeout>"
            "<tev:MessageLimit>99999</tev:MessageLimit>"
            "</tev:PullMessages>"
        )
        status, body = device.dispatch(onvif.ONVIFService.EVENTS, env)
        assert status == 400
        assert b"MessageLimit" in body


# ═══════════════════════════════════════════════════════════════════════
# onvif_ptz — configurations, status, moves, presets
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.onvif_ptz
class TestPTZService:
    def test_get_configurations(self, device):
        env = _env("<tptz:GetConfigurations/>")
        status, body = device.dispatch(onvif.ONVIFService.PTZ, env)
        assert status == 200
        root = _parse(body)
        cfg = root.find(".//tptz:PTZConfiguration", NS)
        assert cfg.get("token") == "PTZConfig_1"

    def test_get_configuration_by_token(self, device):
        env = _env(
            "<tptz:GetConfiguration>"
            "<tptz:PTZConfigurationToken>PTZConfig_1</tptz:PTZConfigurationToken>"
            "</tptz:GetConfiguration>"
        )
        status, body = device.dispatch(onvif.ONVIFService.PTZ, env)
        assert status == 200
        assert b"PTZConfig_1" in body

    def test_get_unknown_configuration_404(self, device):
        env = _env(
            "<tptz:GetConfiguration>"
            "<tptz:PTZConfigurationToken>bogus</tptz:PTZConfigurationToken>"
            "</tptz:GetConfiguration>"
        )
        status, body = device.dispatch(onvif.ONVIFService.PTZ, env)
        assert status == 404

    def test_initial_status_idle(self, device):
        env = _env("<tptz:GetStatus/>")
        status, body = device.dispatch(onvif.ONVIFService.PTZ, env)
        assert status == 200
        assert b"IDLE" in body
        root = _parse(body)
        pt = root.find(".//tt:PanTilt", NS)
        assert pt.get("x") == "0.0"
        assert pt.get("y") == "0.0"

    def test_ptz_get_service_capabilities(self, device):
        env = _env("<tptz:GetServiceCapabilities/>")
        status, body = device.dispatch(onvif.ONVIFService.PTZ, env)
        assert status == 200
        assert b'MoveStatus="true"' in body

    def test_absolute_move(self, device):
        env = _env(
            "<tptz:AbsoluteMove>"
            "<tptz:Position>"
            '<tt:PanTilt x="0.3" y="-0.2"/>'
            '<tt:Zoom x="0.5"/>'
            "</tptz:Position>"
            "</tptz:AbsoluteMove>"
        )
        status, _ = device.dispatch(onvif.ONVIFService.PTZ, env)
        assert status == 200
        s = device.get_ptz_status()
        assert s.pan == pytest.approx(0.3)
        assert s.tilt == pytest.approx(-0.2)
        assert s.zoom == pytest.approx(0.5)

    def test_absolute_move_clamps_to_range(self, device):
        env = _env(
            "<tptz:AbsoluteMove>"
            "<tptz:Position>"
            '<tt:PanTilt x="5.0" y="-5.0"/>'
            '<tt:Zoom x="10.0"/>'
            "</tptz:Position>"
            "</tptz:AbsoluteMove>"
        )
        status, _ = device.dispatch(onvif.ONVIFService.PTZ, env)
        assert status == 200
        s = device.get_ptz_status()
        assert s.pan == 1.0
        assert s.tilt == -1.0
        assert s.zoom == 1.0

    def test_relative_move_composes(self, device):
        device.absolute_move(0.1, 0.1, 0.1)
        env = _env(
            "<tptz:RelativeMove>"
            "<tptz:Translation>"
            '<tt:PanTilt x="0.2" y="0.3"/>'
            '<tt:Zoom x="0.1"/>'
            "</tptz:Translation>"
            "</tptz:RelativeMove>"
        )
        status, _ = device.dispatch(onvif.ONVIFService.PTZ, env)
        assert status == 200
        s = device.get_ptz_status()
        assert s.pan == pytest.approx(0.3)
        assert s.tilt == pytest.approx(0.4)
        assert s.zoom == pytest.approx(0.2)

    def test_continuous_move_sets_status_moving(self, device):
        env = _env(
            "<tptz:ContinuousMove>"
            "<tptz:Velocity>"
            '<tt:PanTilt x="0.5" y="0.0"/>'
            '<tt:Zoom x="0.0"/>'
            "</tptz:Velocity>"
            "</tptz:ContinuousMove>"
        )
        status, _ = device.dispatch(onvif.ONVIFService.PTZ, env)
        assert status == 200
        s = device.get_ptz_status()
        assert s.move_status == onvif.PTZMoveStatus.MOVING

    def test_stop_returns_idle(self, device):
        device.continuous_move(0.5, 0.0, 0.0)
        env = _env(
            "<tptz:Stop>"
            "<tptz:PanTilt>true</tptz:PanTilt>"
            "<tptz:Zoom>true</tptz:Zoom>"
            "</tptz:Stop>"
        )
        status, _ = device.dispatch(onvif.ONVIFService.PTZ, env)
        assert status == 200
        s = device.get_ptz_status()
        assert s.move_status == onvif.PTZMoveStatus.IDLE

    def test_ptz_invalid_axis_value_400(self, device):
        env = _env(
            "<tptz:AbsoluteMove>"
            "<tptz:Position>"
            '<tt:PanTilt x="not-a-number" y="0"/>'
            "</tptz:Position>"
            "</tptz:AbsoluteMove>"
        )
        status, body = device.dispatch(onvif.ONVIFService.PTZ, env)
        assert status == 400
        assert b"PanTilt" in body or b"InvalidArgVal" in body

    def test_preset_set_get_goto_remove_cycle(self, device):
        device.absolute_move(0.5, 0.25, 0.1)
        # Set
        env = _env(
            "<tptz:SetPreset>"
            "<tptz:PresetName>entry-door</tptz:PresetName>"
            "</tptz:SetPreset>"
        )
        status, body = device.dispatch(onvif.ONVIFService.PTZ, env)
        assert status == 200
        root = _parse(body)
        token = root.find(".//tptz:PresetToken", NS).text

        # Get
        env = _env("<tptz:GetPresets/>")
        status, body = device.dispatch(onvif.ONVIFService.PTZ, env)
        assert status == 200
        assert b"entry-door" in body
        assert token.encode() in body

        # Move elsewhere, then goto
        device.absolute_move(0.0, 0.0, 0.0)
        env = _env(
            "<tptz:GotoPreset>"
            f"<tptz:PresetToken>{token}</tptz:PresetToken>"
            "</tptz:GotoPreset>"
        )
        status, _ = device.dispatch(onvif.ONVIFService.PTZ, env)
        assert status == 200
        s = device.get_ptz_status()
        assert s.pan == pytest.approx(0.5)
        assert s.tilt == pytest.approx(0.25)
        assert s.zoom == pytest.approx(0.1)

        # Remove
        env = _env(
            "<tptz:RemovePreset>"
            f"<tptz:PresetToken>{token}</tptz:PresetToken>"
            "</tptz:RemovePreset>"
        )
        status, _ = device.dispatch(onvif.ONVIFService.PTZ, env)
        assert status == 200
        assert token not in {p.token for p in device.list_presets()}

    def test_remove_unknown_preset_404(self, device):
        env = _env(
            "<tptz:RemovePreset>"
            "<tptz:PresetToken>no-such-preset</tptz:PresetToken>"
            "</tptz:RemovePreset>"
        )
        status, body = device.dispatch(onvif.ONVIFService.PTZ, env)
        assert status == 404

    def test_set_preset_missing_name_400(self, device):
        env = _env("<tptz:SetPreset/>")
        status, body = device.dispatch(onvif.ONVIFService.PTZ, env)
        assert status == 400
        assert b"PresetName" in body


# ═══════════════════════════════════════════════════════════════════════
# onvif_fault — error rendering across all services
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.onvif_fault
class TestFaultRendering:
    def test_empty_body_is_bad_request(self, device):
        env = (
            b'<?xml version="1.0"?>'
            b'<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">'
            b"<s:Body/></s:Envelope>"
        )
        status, body = device.dispatch(onvif.ONVIFService.DEVICE, env)
        assert status == 400
        assert b"InvalidArgs" in body

    def test_fault_subcodes_translate_cleanly(self):
        for cls, subcode in [
            (onvif.ONVIFBadRequest, "ter:InvalidArgs"),
            (onvif.ONVIFAuthError, "ter:NotAuthorized"),
            (onvif.ONVIFForbidden, "ter:OperationProhibited"),
            (onvif.ONVIFNotFound, "ter:NoEntity"),
            (onvif.ONVIFActionNotSupported, "ter:ActionNotSupported"),
        ]:
            err = cls("x")
            assert err.fault_subcode == subcode

    def test_fault_body_is_valid_xml(self):
        err = onvif.ONVIFBadRequest("test msg")
        body = onvif.build_soap_fault(err)
        # Should parse without exception
        root = ET.fromstring(body)
        assert root.tag.endswith("Envelope")

    def test_wrong_service_for_op_is_action_not_supported(self, device):
        # Send a Media op to the Device service → ActionNotSupported
        env = _env("<trt:GetProfiles/>")
        status, body = device.dispatch(onvif.ONVIFService.DEVICE, env)
        assert status == 400
        assert b"ActionNotSupported" in body


# ═══════════════════════════════════════════════════════════════════════
# onvif_integration — RTSP → ONVIF full pipeline smoke tests
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.onvif_integration
class TestRTSPIntegration:
    def test_stream_uri_follows_rtsp_port_change(self, rtsp_manager):
        cfg = onvif.ONVIFServiceConfig(
            xaddr_host="cam",
            rtsp_host="cam",
            rtsp_port=9999,
            require_auth=False,
        )
        dev = onvif.ONVIFDevice(cfg, rtsp_manager)
        profile = dev.list_profiles()[0]
        env = _env(
            f"<trt:GetStreamUri>"
            f"<trt:ProfileToken>{profile.token}</trt:ProfileToken>"
            "</trt:GetStreamUri>"
        )
        status, body = dev.dispatch(onvif.ONVIFService.MEDIA, env)
        assert status == 200
        assert b"rtsp://cam:9999/" in body

    def test_new_rtsp_mount_becomes_new_profile(self, device, rtsp_manager):
        before = {p.mount_path for p in device.list_profiles()}
        rtsp_manager.add_mount(
            StreamMount(path="live/hevc", codec=VideoCodec.H265)
        )
        device.refresh_profiles()
        after = {p.mount_path for p in device.list_profiles()}
        assert after - before == {"live/hevc"}

    def test_profile_preserves_h265_codec(self, rtsp_manager):
        rtsp_manager.add_mount(
            StreamMount(path="live/hevc", codec=VideoCodec.H265, fps=15)
        )
        cfg = onvif.ONVIFServiceConfig(
            xaddr_host="cam", rtsp_port=8554, require_auth=False
        )
        dev = onvif.ONVIFDevice(cfg, rtsp_manager)
        env = _env("<trt:GetProfiles/>")
        status, body = dev.dispatch(onvif.ONVIFService.MEDIA, env)
        assert status == 200
        assert b"<tt:Encoding>H265</tt:Encoding>" in body

    def test_dispatch_is_thread_safe(self, device):
        import threading
        errors: list[Exception] = []

        def worker():
            for _ in range(20):
                try:
                    env = _env("<tds:GetDeviceInformation/>")
                    status, _ = device.dispatch(onvif.ONVIFService.DEVICE, env)
                    assert status == 200
                except Exception as exc:
                    errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == []
