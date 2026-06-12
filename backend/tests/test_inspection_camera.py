"""OP-2126 - C8.A2 camera abstraction for inspection acquisition."""

from __future__ import annotations

import pytest

from backend.inspection.camera import (
    CameraBus,
    CameraConfig,
    CameraFrame,
    CameraSource,
    FixtureCameraSource,
    FixtureFrameGenerator,
    GigEVisionSimulatorClient,
    TriggerMode,
    V4L2CameraSource,
)


class FakeClock:
    def __init__(self, ticks: list[float]) -> None:
        self._ticks = list(ticks)

    def __call__(self) -> float:
        return self._ticks.pop(0)


class TestCameraSourceSurface:
    def test_interface_is_abstract(self) -> None:
        with pytest.raises(TypeError):
            CameraSource()  # type: ignore[abstract]

    @pytest.mark.parametrize(
        ("kwargs", "message"),
        [
            ({"width": 0}, "width"),
            ({"height": 0}, "height"),
            ({"pixel_format": ""}, "pixel_format"),
            ({"fps": 0.0}, "fps"),
            ({"exposure_us": 0}, "exposure_us"),
        ],
    )
    def test_config_rejects_invalid_values(
        self,
        kwargs: dict[str, object],
        message: str,
    ) -> None:
        values = {
            "width": 4,
            "height": 3,
            "pixel_format": "MONO8",
            "fps": 30.0,
            "exposure_us": 1000,
        }
        values.update(kwargs)
        config = CameraConfig(**values)

        with pytest.raises(ValueError, match=message):
            config.validate()


class TestFixtureFrameGenerator:
    def test_fixture_generator_returns_deterministic_ci_frame(self) -> None:
        generator = FixtureFrameGenerator(clock=FakeClock([12.5]), source_id="ci-cam")
        config = CameraConfig(width=3, height=2, pixel_format="MONO8")

        frame = generator.make_frame(1, config)

        assert frame == CameraFrame(
            frame_id=1,
            timestamp_s=12.5,
            width=3,
            height=2,
            pixel_format="MONO8",
            payload=bytes([1, 2, 3, 4, 5, 6]),
            source_id="ci-cam",
            bus=CameraBus.fixture,
        )
        assert frame.size_bytes == 6

    def test_fixture_camera_source_uses_open_configure_grab_sequence(self) -> None:
        source = FixtureCameraSource(
            source_id="fixture-1",
            generator=FixtureFrameGenerator(clock=FakeClock([20.0]), source_id="fixture-1"),
        )

        with pytest.raises(RuntimeError, match="opened"):
            source.grab()

        source.open()
        source.configure(CameraConfig(width=2, height=2, pixel_format="RGB24"))
        source.set_trigger_mode(TriggerMode.software)
        frame = source.grab()

        assert frame.frame_id == 1
        assert frame.bus == CameraBus.fixture
        assert frame.size_bytes == 12
        assert frame.source_id == "fixture-1"


class TestV4L2CameraSource:
    def test_v4l2_binding_wraps_injected_capture_function(self) -> None:
        calls: list[tuple[str, CameraConfig, int]] = []

        def capture(device_path: str, config: CameraConfig, frame_id: int) -> bytes:
            calls.append((device_path, config, frame_id))
            return b"\x10\x11\x12\x13"

        source = V4L2CameraSource(
            device_path="/dev/video-test0",
            source_id="mipi-left",
            capture_frame=capture,
            clock=FakeClock([30.0]),
        )
        config = CameraConfig(width=2, height=2, pixel_format="MONO8")

        source.open()
        source.configure(config)
        source.set_trigger_mode(TriggerMode.hardware)
        frame = source.grab()

        assert calls == [
            (
                "/dev/video-test0",
                CameraConfig(
                    width=2,
                    height=2,
                    pixel_format="MONO8",
                    trigger_mode=TriggerMode.hardware,
                ),
                1,
            )
        ]
        assert frame.bus == CameraBus.v4l2_mipi
        assert frame.source_id == "mipi-left"
        assert frame.timestamp_s == pytest.approx(30.0)
        assert frame.payload == b"\x10\x11\x12\x13"

    def test_v4l2_binding_reports_missing_device_without_capture_backend(self) -> None:
        source = V4L2CameraSource(device_path="/dev/video-definitely-missing-op2126")

        with pytest.raises(FileNotFoundError):
            source.open()


class TestGigEVisionSimulatorClient:
    def test_discover_and_configure_use_protocol_shaped_commands(self) -> None:
        client = GigEVisionSimulatorClient(camera_id="line-sim-1")
        config = CameraConfig(width=4, height=1, pixel_format="MONO8", fps=60.0)

        discovered = client.discover()
        client.open()
        client.configure(config)
        frame = client.grab()

        assert discovered["protocol"] == "gige_vision_sim"
        assert [entry["command"] for entry in client.command_log] == [
            "DISCOVER",
            "OPEN",
            "CONFIGURE",
            "GRAB",
        ]
        assert client.command_log[2]["width"] == 4
        assert client.command_log[2]["fps"] == 60.0
        assert frame.bus == CameraBus.gige_vision_sim
        assert frame.metadata["simulated"] is True
        assert frame.metadata["camera_id"] == "line-sim-1"

    def test_software_trigger_mode_requires_pending_trigger_before_grab(self) -> None:
        client = GigEVisionSimulatorClient(
            generator=FixtureFrameGenerator(clock=FakeClock([40.0]), source_id="gige-1"),
        )
        client.open()
        client.configure(
            CameraConfig(
                width=2,
                height=1,
                pixel_format="MONO8",
                trigger_mode=TriggerMode.software,
            )
        )

        with pytest.raises(RuntimeError, match="pending trigger"):
            client.grab()

        client.software_trigger()
        frame = client.grab()

        assert frame.frame_id == 1
        assert frame.payload == bytes([1, 2])
        assert [entry["command"] for entry in client.command_log] == [
            "OPEN",
            "CONFIGURE",
            "TRIGGER",
            "GRAB",
        ]

    def test_hardware_trigger_mode_rejects_software_trigger_call(self) -> None:
        client = GigEVisionSimulatorClient()
        client.open()
        client.configure(
            CameraConfig(
                width=2,
                height=1,
                trigger_mode=TriggerMode.hardware,
            )
        )

        with pytest.raises(RuntimeError, match="software trigger requires"):
            client.software_trigger()

        client.hardware_trigger()
        frame = client.grab()

        assert frame.frame_id == 1
        assert frame.bus == CameraBus.gige_vision_sim
