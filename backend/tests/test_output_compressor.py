"""Tests for backend/output_compressor.py — output compression engine."""

import asyncio

import pytest

from backend.output_compressor import compress_output, get_compression_stats, reset_compression_stats, _looks_binary


class TestInputValidation:

    @pytest.mark.asyncio
    async def test_none_input(self):
        text, saved = await compress_output(None, "test")
        assert text == ""
        assert saved == 0

    @pytest.mark.asyncio
    async def test_empty_string(self):
        text, saved = await compress_output("", "test")
        assert text == ""
        assert saved == 0

    @pytest.mark.asyncio
    async def test_non_string_input(self):
        text, saved = await compress_output(123, "test")
        assert text == "123"
        assert saved == 0

    @pytest.mark.asyncio
    async def test_small_output_no_compression(self):
        text, saved = await compress_output("hello world", "test")
        assert text == "hello world"
        assert saved == 0


class TestDeduplication:

    @pytest.mark.asyncio
    async def test_duplicate_lines_collapsed(self):
        lines = ["warning: unused variable x in function do_something_important"] * 50
        text = "\n".join(lines)  # > 1000 bytes threshold
        compressed, saved = await compress_output(text, "run_bash")
        assert saved > 0
        assert "identical line(s) removed" in compressed

    @pytest.mark.asyncio
    async def test_no_dedup_for_unique_lines(self):
        lines = [f"line {i}" for i in range(20)]
        text = "\n".join(lines)
        compressed, saved = await compress_output(text, "test")
        # Unique lines — minimal compression (only blank line cleanup)
        assert "identical line(s) removed" not in compressed


class TestPatternCollapse:

    @pytest.mark.asyncio
    async def test_repeated_warnings_collapsed(self):
        lines = [f"/src/file{i}.c:10:5: warning: unused variable 'x' in function 'process_data'" for i in range(30)]
        text = "\n".join(lines)  # > 1000 bytes
        compressed, saved = await compress_output(text, "run_bash")
        assert saved > 0
        assert "pattern repeated" in compressed


class TestBinaryDetection:

    def test_text_content(self):
        assert not _looks_binary("hello world\nfoo bar\n")

    def test_binary_content(self):
        binary = "\x00\x01\x02\x03\x04" * 50
        assert _looks_binary(binary)

    def test_mixed_content_mostly_text(self):
        text = "normal text " * 20 + "\x00"
        assert not _looks_binary(text)  # <10% non-printable


class TestCompressionStats:

    def test_stats_tracking(self):
        reset_compression_stats()
        lines = ["same line repeated for compression threshold test padding"] * 50
        asyncio.run(compress_output("\n".join(lines), "test"))  # > 1000 bytes
        stats = get_compression_stats()
        assert stats["compression_count"] >= 1
        assert stats["total_lines_removed"] > 0
        assert stats["avg_ratio"] > 0

    def test_stats_reset(self):
        reset_compression_stats()
        stats = get_compression_stats()
        assert stats["compression_count"] == 0
        assert stats["total_original_bytes"] == 0
