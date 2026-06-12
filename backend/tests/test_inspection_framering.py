"""OP-2132 - C8.A4 frame ring zero-copy handoff."""

from __future__ import annotations

import pytest

from backend.inspection.camera import (
    CameraBus,
    CameraConfig,
    CameraFrame,
    FixtureCameraSource,
    FixtureFrameGenerator,
)
from backend.inspection.framering import FrameRingBuffer


class FakeClock:
    def __init__(self, ticks: list[float]) -> None:
        self._ticks = list(ticks)

    def __call__(self) -> float:
        return self._ticks.pop(0)


def _frame(frame_id: int, payload: bytes | None = None) -> CameraFrame:
    return CameraFrame(
        frame_id=frame_id,
        timestamp_s=100.0 + frame_id,
        width=2,
        height=2,
        pixel_format="MONO8",
        payload=payload
        or bytes([frame_id, frame_id + 1, frame_id + 2, frame_id + 3]),
        source_id="cam-1",
        bus=CameraBus.fixture,
        metadata={"frame": frame_id},
    )


class TestFrameRingBuffer:
    def test_publish_and_borrow_returns_memoryview_backed_lease(self) -> None:
        ring = FrameRingBuffer(capacity=2, slot_size_bytes=8)

        assert ring.publish(_frame(1)) is True
        lease = ring.borrow()

        assert lease is not None
        assert lease.frame_id == 1
        assert lease.timestamp_s == pytest.approx(101.0)
        assert lease.width == 2
        assert lease.height == 2
        assert lease.pixel_format == "MONO8"
        assert lease.source_id == "cam-1"
        assert lease.bus == CameraBus.fixture
        assert lease.metadata == {"frame": 1}
        assert isinstance(lease.payload, memoryview)
        assert lease.payload.obj is ring._slots[0].payload
        assert bytes(lease.payload) == bytes([1, 2, 3, 4])
        assert lease.size_bytes == 4

        stats = ring.stats()
        assert stats.published_frames == 1
        assert stats.borrowed_frames == 1
        assert stats.ready_frames == 0
        assert stats.leased_frames == 1

        lease.release()
        assert lease.released is True
        with pytest.raises(RuntimeError, match="released"):
            lease.payload
        assert ring.stats().leased_frames == 0

    def test_context_manager_releases_slot_for_reuse(self) -> None:
        ring = FrameRingBuffer(capacity=1, slot_size_bytes=8)
        assert ring.publish(_frame(1)) is True

        lease = ring.borrow()
        assert lease is not None
        with lease:
            assert bytes(lease.payload) == bytes([1, 2, 3, 4])

        assert ring.stats().leased_frames == 0
        assert ring.publish(_frame(2)) is True
        next_lease = ring.borrow()
        assert next_lease is not None
        assert next_lease.frame_id == 2
        next_lease.release()

    def test_drop_oldest_ready_frame_when_producer_laps_consumer(self) -> None:
        ring = FrameRingBuffer(capacity=2, slot_size_bytes=8)

        assert ring.publish(_frame(1)) is True
        assert ring.publish(_frame(2)) is True
        assert ring.publish(_frame(3)) is True

        first = ring.borrow()
        second = ring.borrow()

        assert first is not None
        assert second is not None
        assert [first.frame_id, second.frame_id] == [2, 3]
        stats = ring.stats()
        assert stats.dropped_frames == 1
        assert stats.lag_events == 1
        assert stats.ready_frames == 0
        first.release()
        second.release()

    def test_all_leased_slots_drop_incoming_without_overwriting_borrowed_memory(self) -> None:
        ring = FrameRingBuffer(capacity=1, slot_size_bytes=8)
        assert ring.publish(_frame(1, b"abcd")) is True
        lease = ring.borrow()
        assert lease is not None
        borrowed_payload = lease.payload

        assert ring.publish(_frame(2, b"WXYZ")) is False

        assert bytes(borrowed_payload) == b"abcd"
        stats = ring.stats()
        assert stats.published_frames == 1
        assert stats.dropped_frames == 1
        assert stats.lag_events == 1
        assert stats.leased_frames == 1
        lease.release()

    def test_grab_from_camera_source_publishes_camera_frame(self) -> None:
        source = FixtureCameraSource(
            source_id="fixture-1",
            generator=FixtureFrameGenerator(
                clock=FakeClock([12.5]),
                source_id="fixture-1",
            ),
        )
        source.open()
        source.configure(CameraConfig(width=2, height=1, pixel_format="MONO8"))
        ring = FrameRingBuffer(capacity=1, slot_size_bytes=4)

        assert ring.grab_from(source) is True
        lease = ring.borrow()

        assert lease is not None
        assert lease.frame_id == 1
        assert lease.timestamp_s == pytest.approx(12.5)
        assert bytes(lease.payload) == bytes([1, 2])
        lease.release()

    @pytest.mark.parametrize(
        ("kwargs", "message"),
        [
            ({"capacity": 0, "slot_size_bytes": 8}, "capacity"),
            ({"capacity": 1, "slot_size_bytes": 0}, "slot_size_bytes"),
        ],
    )
    def test_ring_rejects_invalid_configuration(
        self,
        kwargs: dict[str, int],
        message: str,
    ) -> None:
        with pytest.raises(ValueError, match=message):
            FrameRingBuffer(**kwargs)

    def test_publish_rejects_frame_larger_than_slot(self) -> None:
        ring = FrameRingBuffer(capacity=1, slot_size_bytes=3)

        with pytest.raises(ValueError, match="slot_size_bytes"):
            ring.publish(_frame(1, b"abcd"))
