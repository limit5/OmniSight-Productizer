"""OP-2128 - C8.A5 line-scan stitching stub."""

from __future__ import annotations

import pytest

from backend.inspection.linescan import (
    CameraSourceLike,
    LineScanStitcher,
    LineScanStrip,
    LineScanStripSource,
    SyntheticLineScanSource,
)


def _strip(sequence: int, rows: list[list[float]] | None = None) -> LineScanStrip:
    return LineScanStrip(
        sequence=sequence,
        image=rows
        or [
            [float(sequence), float(sequence) + 0.1],
            [float(sequence), float(sequence) + 0.2],
        ],
        captured_at_s=100.0 + sequence,
        metadata={"strip": sequence},
    )


class TestLineScanSourceSurface:
    def test_source_interface_is_abstract(self) -> None:
        with pytest.raises(TypeError):
            LineScanStripSource()  # type: ignore[abstract]

    def test_synthetic_source_is_camera_source_like(self) -> None:
        source = SyntheticLineScanSource([_strip(0)])

        assert isinstance(source, CameraSourceLike)
        assert source.next_strip() == _strip(0)
        assert source.next_strip() is None


class TestLineScanStitcher:
    def test_drain_source_assembles_synthetic_strips_into_frame(self) -> None:
        source = SyntheticLineScanSource([_strip(0), _strip(1), _strip(2)])
        stitcher = LineScanStitcher(strips_per_frame=3)

        frames = stitcher.drain_source(source)

        assert len(frames) == 1
        frame = frames[0]
        assert frame.frame_id == 1
        assert frame.start_sequence == 0
        assert frame.end_sequence == 2
        assert frame.width == 2
        assert frame.height == 6
        assert frame.metadata == {"strip_count": 3}
        assert frame.image == [
            [0.0, 0.1],
            [0.0, 0.2],
            [1.0, 1.1],
            [1.0, 1.2],
            [2.0, 2.1],
            [2.0, 2.2],
        ]
        assert [strip.sequence for strip in frame.strips] == [0, 1, 2]

    def test_ring_retains_recent_strips_and_emits_stride_frames(self) -> None:
        stitcher = LineScanStitcher(
            strips_per_frame=3,
            ring_capacity=4,
            frame_stride_strips=2,
        )

        frames = [
            frame
            for frame in (stitcher.append(_strip(sequence)) for sequence in range(5))
            if frame is not None
        ]

        assert [(frame.start_sequence, frame.end_sequence) for frame in frames] == [
            (0, 2),
            (2, 4),
        ]
        assert stitcher.buffered_sequences() == [1, 2, 3, 4]
        assert stitcher.buffered_count == 4

    def test_append_rejects_non_contiguous_strips(self) -> None:
        stitcher = LineScanStitcher(strips_per_frame=2)
        assert stitcher.append(_strip(0)) is None

        with pytest.raises(ValueError, match="contiguous"):
            stitcher.append(_strip(2))

    def test_append_rejects_width_changes(self) -> None:
        stitcher = LineScanStitcher(strips_per_frame=2)
        assert stitcher.append(_strip(0, [[0.0, 0.1]])) is None

        with pytest.raises(ValueError, match="width changed"):
            stitcher.append(_strip(1, [[1.0, 1.1, 1.2]]))

    @pytest.mark.parametrize(
        ("kwargs", "message"),
        [
            ({"strips_per_frame": 0}, "strips_per_frame"),
            ({"strips_per_frame": 2, "ring_capacity": 1}, "ring_capacity"),
            (
                {"strips_per_frame": 2, "frame_stride_strips": 0},
                "frame_stride_strips",
            ),
        ],
    )
    def test_stitcher_rejects_invalid_configuration(
        self,
        kwargs: dict[str, int],
        message: str,
    ) -> None:
        with pytest.raises(ValueError, match=message):
            LineScanStitcher(**kwargs)

    def test_strip_rejects_empty_or_ragged_image(self) -> None:
        with pytest.raises(ValueError, match="at least one pixel"):
            LineScanStrip(sequence=0, image=[]).validate()

        with pytest.raises(ValueError, match="consistent width"):
            LineScanStrip(sequence=0, image=[[1.0], [2.0, 3.0]]).validate()
