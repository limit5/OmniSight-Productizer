"""Inspection contracts + runtime helpers shared by production-line vision components."""

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
from backend.inspection.fieldbus import (
    D3_SAFETY_INVARIANT,
    ConnectionState,
    FieldbusAdapter,
    FieldbusConnectionState,
    ModbusFieldbusAdapter,
    PollCycleResult,
    RegisterRead,
    RegisterWrite,
    SimulatedModbusEndpoint,
)
from backend.inspection.golden import (
    DiffScorer,
    GoldenSample,
    GoldenSampleRegistry,
    PixelFeatureDiffScorer,
)
from backend.inspection.linescan import (
    CameraSourceLike,
    LineScanFrame,
    LineScanStitcher,
    LineScanStrip,
    LineScanStripSource,
    SyntheticLineScanSource,
)
from backend.inspection.verdict import Defect, InspectionVerdict, verdict_json_schema

__all__ = [
    "CameraBus",
    "CameraConfig",
    "CameraFrame",
    "CameraSource",
    "CameraSourceLike",
    "D3_SAFETY_INVARIANT",
    "ConnectionState",
    "Defect",
    "DiffScorer",
    "FieldbusAdapter",
    "FieldbusConnectionState",
    "FixtureCameraSource",
    "FixtureFrameGenerator",
    "GigEVisionSimulatorClient",
    "GoldenSample",
    "GoldenSampleRegistry",
    "InspectionVerdict",
    "LineScanFrame",
    "LineScanStitcher",
    "LineScanStrip",
    "LineScanStripSource",
    "ModbusFieldbusAdapter",
    "PixelFeatureDiffScorer",
    "PollCycleResult",
    "RegisterRead",
    "RegisterWrite",
    "SimulatedModbusEndpoint",
    "SyntheticLineScanSource",
    "TriggerMode",
    "V4L2CameraSource",
    "verdict_json_schema",
]
