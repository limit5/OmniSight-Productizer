"""OP-2131 - C8.A3 trigger and exposure-sync contracts."""

from __future__ import annotations

import pytest

from backend.inspection.trigger import (
    ExposureSyncContract,
    ExposureSyncTimeline,
    GpioTriggerSource,
    SoftwareTriggerSource,
    TriggerEvent,
    TriggerKind,
    TriggerSource,
)


class FakeClock:
    def __init__(self, ticks: list[float]) -> None:
        self._ticks = list(ticks)

    def __call__(self) -> float:
        return self._ticks.pop(0)


class PinScript:
    def __init__(self, levels: list[bool]) -> None:
        self._levels = list(levels)

    def __call__(self) -> bool:
        return self._levels.pop(0)


class TestTriggerSourceSurface:
    def test_interface_is_abstract(self) -> None:
        with pytest.raises(TypeError):
            TriggerSource()  # type: ignore[abstract]

    def test_software_trigger_requires_arm_before_fire(self) -> None:
        source = SoftwareTriggerSource(clock=FakeClock([10.0]))

        with pytest.raises(RuntimeError, match="must be armed"):
            source.fire()

    def test_software_trigger_queues_events_with_stable_sequence(self) -> None:
        source = SoftwareTriggerSource(clock=FakeClock([10.0, 10.5]))
        source.arm()

        first = source.fire(metadata={"part_id": "part-1"})
        second = source.fire()

        assert first.kind == TriggerKind.software
        assert first.triggered_at_s == pytest.approx(10.0)
        assert first.sequence == 1
        assert first.metadata == {"part_id": "part-1"}
        assert second.sequence == 2
        assert source.next_trigger() == first
        assert source.next_trigger() == second
        assert source.next_trigger() is None

    def test_gpio_trigger_reports_only_rising_edges_and_invokes_callback(self) -> None:
        observed: list[TriggerEvent] = []
        source = GpioTriggerSource(
            read_pin=PinScript([False, False, True, True, False, True]),
            clock=FakeClock([20.0, 21.0]),
            callback=observed.append,
            metadata={"gpio": "DI0"},
        )

        source.arm()
        assert source.next_trigger() is None
        first = source.next_trigger()
        assert source.next_trigger() is None
        assert source.next_trigger() is None
        second = source.next_trigger()

        assert first == TriggerEvent(
            kind=TriggerKind.hardware,
            triggered_at_s=20.0,
            sequence=1,
            metadata={"gpio": "DI0"},
        )
        assert second == TriggerEvent(
            kind=TriggerKind.hardware,
            triggered_at_s=21.0,
            sequence=2,
            metadata={"gpio": "DI0"},
        )
        assert observed == [first, second]


class TestExposureSyncContract:
    def test_valid_contract_simulates_strobe_inside_exposure_before_frame_grab(self) -> None:
        trigger = TriggerEvent(
            kind=TriggerKind.software,
            triggered_at_s=100.0,
            sequence=1,
        )
        contract = ExposureSyncContract(
            trigger_to_exposure_delay_s=0.002,
            exposure_duration_s=0.010,
            strobe_fire_offset_s=0.001,
            strobe_duration_s=0.004,
            frame_grab_offset_s=0.003,
        )

        timeline = contract.simulate(trigger)

        assert timeline.trigger == trigger
        assert timeline.exposure_start_s == pytest.approx(100.002)
        assert timeline.exposure_end_s == pytest.approx(100.012)
        assert timeline.strobe_fire_s == pytest.approx(100.003)
        assert timeline.strobe_end_s == pytest.approx(100.007)
        assert timeline.frame_grab_s == pytest.approx(100.015)
        assert timeline.contract == contract

    @pytest.mark.parametrize(
        ("kwargs", "message"),
        [
            ({"trigger_to_exposure_delay_s": -0.001}, "trigger_to_exposure_delay_s"),
            ({"exposure_duration_s": 0.0}, "exposure_duration_s"),
            ({"strobe_fire_offset_s": -0.001}, "strobe_fire_offset_s"),
            ({"strobe_duration_s": 0.0}, "strobe_duration_s"),
            ({"frame_grab_offset_s": -0.001}, "frame_grab_offset_s"),
            ({"tolerance_s": -0.001}, "tolerance_s"),
        ],
    )
    def test_contract_rejects_invalid_timing_values(
        self,
        kwargs: dict[str, float],
        message: str,
    ) -> None:
        values = {
            "trigger_to_exposure_delay_s": 0.001,
            "exposure_duration_s": 0.010,
            "strobe_fire_offset_s": 0.001,
            "strobe_duration_s": 0.004,
            "frame_grab_offset_s": 0.002,
            "tolerance_s": 0.0,
        }
        values.update(kwargs)
        contract = ExposureSyncContract(**values)

        with pytest.raises(ValueError, match=message):
            contract.validate()

    def test_contract_rejects_strobe_extending_past_exposure(self) -> None:
        contract = ExposureSyncContract(
            trigger_to_exposure_delay_s=0.001,
            exposure_duration_s=0.010,
            strobe_fire_offset_s=0.008,
            strobe_duration_s=0.004,
            frame_grab_offset_s=0.002,
        )

        with pytest.raises(ValueError, match="strobe window"):
            contract.validate()

    def test_timeline_rejects_frame_grab_before_exposure_end(self) -> None:
        contract = ExposureSyncContract(
            trigger_to_exposure_delay_s=0.001,
            exposure_duration_s=0.010,
            strobe_fire_offset_s=0.001,
            strobe_duration_s=0.004,
            frame_grab_offset_s=0.002,
        )
        timeline = ExposureSyncTimeline(
            trigger=TriggerEvent(TriggerKind.hardware, 50.0, 1),
            exposure_start_s=50.001,
            exposure_end_s=50.011,
            strobe_fire_s=50.002,
            strobe_end_s=50.006,
            frame_grab_s=50.010,
            contract=contract,
        )

        with pytest.raises(ValueError, match="frame grab"):
            timeline.validate()
