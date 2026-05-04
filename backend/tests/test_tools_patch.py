"""Phase 67-B S1 — apply_search_replace + apply_unified_diff."""

from __future__ import annotations

import textwrap
from datetime import datetime, timezone

import pytest

from backend.agents import tools_patch as tp

REPO_ROOT = tp.REPO_ROOT


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  SEARCH/REPLACE parse
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _sr_block(search: str, replace: str) -> str:
    return (
        "<<<<<<< SEARCH\n"
        + search
        + "=======\n"
        + replace
        + ">>>>>>> REPLACE"
    )


def test_parse_single_block():
    payload = _sr_block("a\nb\nc\n", "x\ny\nz\n")
    blocks = tp.parse_search_replace(payload)
    assert len(blocks) == 1
    assert blocks[0].search == "a\nb\nc\n"
    assert blocks[0].replace == "x\ny\nz\n"


def test_parse_multiple_blocks():
    p = _sr_block("a\n", "b\n") + "\n" + _sr_block("c\n", "d\n")
    blocks = tp.parse_search_replace(p)
    assert len(blocks) == 2


def test_parse_tolerates_marker_whitespace():
    payload = (
        "<<<<<<< SEARCH   \n"
        "a\nb\nc\n"
        "=======   \n"
        "x\ny\nz\n"
        ">>>>>>> REPLACE   "
    )
    blocks = tp.parse_search_replace(payload)
    assert blocks == [
        tp.SearchReplaceBlock(search="a\nb\nc\n", replace="x\ny\nz\n")
    ]


def test_parse_empty_raises():
    with pytest.raises(tp.PatchMalformed, match="empty"):
        tp.parse_search_replace("")


def test_parse_no_block_raises():
    with pytest.raises(tp.PatchMalformed, match="no SEARCH/REPLACE"):
        tp.parse_search_replace("just some text")


def test_parse_unbalanced_markers_raises():
    malformed = (
        "<<<<<<< SEARCH\na\n=======\nb\n>>>>>>> REPLACE\n"
        "<<<<<<< SEARCH\n"  # orphan
    )
    with pytest.raises(tp.PatchMalformed, match="unbalanced"):
        tp.parse_search_replace(malformed)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  apply_search_replace — single block
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

SOURCE_GOOD = textwrap.dedent("""\
    def init_gpio(pin_number):
        # Initialize the hardware pin
        setup_pin(pin_number, MODE_IN)
        # unrelated comment
        return True
    """)


def test_apply_search_replace_replaces_exactly_once():
    block = tp.SearchReplaceBlock(
        search=(
            "def init_gpio(pin_number):\n"
            "    # Initialize the hardware pin\n"
            "    setup_pin(pin_number, MODE_IN)\n"
        ),
        replace=(
            "def init_gpio(pin_number):\n"
            "    # Initialize the hardware pin with Pull-Up resistor\n"
            "    setup_pin(pin_number, MODE_IN, PULL_UP)\n"
            "    verify_pin_state(pin_number)\n"
        ),
    )
    out = tp.apply_search_replace(SOURCE_GOOD, block)
    assert "PULL_UP" in out
    assert "verify_pin_state" in out
    assert "unrelated comment" in out  # untouched tail preserved


def test_too_little_context_rejected():
    block = tp.SearchReplaceBlock(
        search="setup_pin(pin_number, MODE_IN)\n",  # only 1 non-blank line
        replace="setup_pin(pin_number, MODE_IN, PULL_UP)\n",
    )
    with pytest.raises(tp.PatchMalformed, match="fewer than"):
        tp.apply_search_replace(SOURCE_GOOD, block)


def test_zero_match_raises_not_found():
    block = tp.SearchReplaceBlock(
        search="def nothing():\n    return 1\n    return 2\n",
        replace="def nothing():\n    return 42\n    return 42\n",
    )
    with pytest.raises(tp.PatchNotFound):
        tp.apply_search_replace(SOURCE_GOOD, block)


def test_ambiguous_match_raises():
    dup = "hello\nhello\nhello\n" * 3  # "hello\nhello\nhello\n" appears 3×
    block = tp.SearchReplaceBlock(search="hello\nhello\nhello\n",
                                  replace="world\nworld\nworld\n")
    with pytest.raises(tp.PatchAmbiguous, match="matched 3"):
        tp.apply_search_replace(dup, block)


def test_preserves_tail_exactly():
    out = tp.apply_search_replace(SOURCE_GOOD, tp.SearchReplaceBlock(
        search=(
            "def init_gpio(pin_number):\n"
            "    # Initialize the hardware pin\n"
            "    setup_pin(pin_number, MODE_IN)\n"
        ),
        replace="def init_gpio(pin_number):\n    pass\n    pass\n",
    ))
    # Everything after the patched section stays verbatim.
    assert out.endswith("    # unrelated comment\n    return True\n")


def test_cascade_layer_1_exact_match():
    match = tp.find_search_replace_match(
        SOURCE_GOOD,
        (
            "def init_gpio(pin_number):\n"
            "    # Initialize the hardware pin\n"
            "    setup_pin(pin_number, MODE_IN)\n"
        ),
    )
    assert match.layer == 1
    assert match.score == 1.0


def test_cascade_layer_2_indent_agnostic_match():
    source = textwrap.dedent("""\
        if ready:
          start()
          finish()
    """)
    block = tp.SearchReplaceBlock(
        search="if ready:\n    start()\n    finish()\n",
        replace="if ready:\n    start()\n    verify()\n",
    )
    match = tp.find_search_replace_match(source, block.search)
    out = tp.apply_search_replace(source, block)

    assert match.layer == 2
    assert match.score == 0.98
    assert "verify()" in out


def test_cascade_layer_3_prefix_tail_rescue_match():
    source = textwrap.dedent("""\
        def render():
            before()
            live_middle()
            after()
    """)
    block = tp.SearchReplaceBlock(
        search="def render():\n    before()\n    stale_middle()\n    after()\n",
        replace="def render():\n    before()\n    new_middle()\n    after()\n",
    )
    match = tp.find_search_replace_match(source, block.search)
    out = tp.apply_search_replace(source, block)

    assert match.layer == 3
    assert match.score == 0.94
    assert "new_middle()" in out


def test_cascade_layer_4_jaro_winkler_match():
    source = textwrap.dedent("""\
        def provision():
            prepare_config()
            apply_config()
            verify_output()
    """)
    block = tp.SearchReplaceBlock(
        search=(
            "def provision_config():\n"
            "    prepare_config()\n"
            "    apply_configs()\n"
            "    verify_output()\n"
        ),
        replace=(
            "def provision():\n"
            "    prepare_config()\n"
            "    apply_config()\n"
            "    record_output()\n"
        ),
    )
    match = tp.find_search_replace_match(source, block.search)
    out = tp.apply_search_replace(source, block)

    assert match.layer == 4
    assert match.score >= 0.9
    assert match.score < 1.0
    assert "record_output()" in out


def test_hd_bringup_strict_path_uses_095_fuzzy_threshold(tmp_path):
    f = tmp_path / "board.dts"
    f.write_text(textwrap.dedent("""\
        &i2c1 {
            status = "okay";
            clock-frequency = <400000>;
            sensor@10 {
                compatible = "vendor,old-sensor";
                reg = <0x10>;
            };
        };
    """), encoding="utf-8")
    payload = _sr_block(
        textwrap.dedent("""\
            &i2c2 {
                status = "okay";
                clock-frequency = <100000>;
                sensor@10 {
                    compatible = "vendor,new-sensor";
                    reg = <0x10>;
                };
            };
        """),
        textwrap.dedent("""\
            &i2c1 {
                status = "okay";
                clock-frequency = <400000>;
                sensor@10 {
                    compatible = "vendor,new-sensor";
                    reg = <0x10>;
                };
            };
        """),
    )

    with pytest.raises(tp.PatchNotFound):
        tp.apply_to_file(f, "search_replace", payload)


def test_non_hd_path_keeps_09_fuzzy_threshold():
    source = textwrap.dedent("""\
        def provision():
            prepare_config()
            apply_config()
            verify_output()
    """)
    block = tp.SearchReplaceBlock(
        search=(
            "def provision_config():\n"
            "    prepare_config()\n"
            "    apply_configs()\n"
            "    verify_output()\n"
        ),
        replace=(
            "def provision():\n"
            "    prepare_config()\n"
            "    apply_config()\n"
            "    record_output()\n"
        ),
    )

    out = tp.apply_search_replace(source, block)

    assert "record_output()" in out


def test_cascade_match_score_flows_into_search_replace_payload_chain():
    src = "one\ntwo\nthree\nfour\nfive\n"
    payload = (
        _sr_block("one\ntwo\nthree\n", "one\nTWO\nthree\n")
        + "\n"
        + _sr_block("one\nTWO\nthree\n", "one\nTWO\nTHREE\n")
    )
    out, matches = tp._apply_search_replace_payload_with_matches(src, payload)

    assert "THREE" in out
    assert [m.layer for m in matches] == [1, 1]
    assert [m.score for m in matches] == [1.0, 1.0]


def test_cascade_ambiguous_fallback_raises():
    source = textwrap.dedent("""\
        patch target:
            keep()
            live_a()
            done()

        patch target:
            keep()
            live_b()
            done()
    """)
    block = tp.SearchReplaceBlock(
        search="patch target:\n    keep()\n    stale()\n    done()\n",
        replace="patch target:\n    keep()\n    patched()\n    done()\n",
    )

    with pytest.raises(tp.PatchAmbiguous, match="cascade layer 3"):
        tp.apply_search_replace(source, block)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  apply_search_replace_payload — multi-block chain
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_multi_block_applied_in_order():
    src = "line-one\nline-two\nline-three\nline-four\nline-five\n"
    payload = (
        _sr_block("line-one\nline-two\nline-three\n",
                  "line-one\nline-NEW\nline-three\n")
        + "\n"
        + _sr_block("line-NEW\nline-three\nline-four\n",
                    "line-NEW\nline-AGAIN\nline-four\n")
    )
    out = tp.apply_search_replace_payload(src, payload)
    assert "line-AGAIN" in out
    assert "line-two" not in out


def test_multi_block_failure_annotates_block_index():
    src = "aaa\nbbb\nccc\nddd\neee\n"
    payload = (
        _sr_block("aaa\nbbb\nccc\n", "aaa\nBBB\nccc\n")
        + _sr_block("nothing\nnothing\nnothing\n",
                    "zzz\nzzz\nzzz\n")  # will not match
    )
    with pytest.raises(tp.PatchNotFound, match="block 2/2"):
        tp.apply_search_replace_payload(src, payload)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Unified diff
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_unified_diff_single_hunk():
    source = "alpha\nbeta\ngamma\n"
    diff = textwrap.dedent("""\
        --- a/file
        +++ b/file
        @@ -1,3 +1,3 @@
         alpha
        -beta
        +BETA
         gamma
        """)
    out = tp.apply_unified_diff(source, diff)
    assert out == "alpha\nBETA\ngamma\n"


def test_unified_diff_multiple_hunks():
    source = "a\nb\nc\nd\ne\nf\ng\nh\n"
    diff = textwrap.dedent("""\
        --- a/x
        +++ b/x
        @@ -1,3 +1,3 @@
         a
        -b
        +B
         c
        @@ -6,3 +6,3 @@
         f
        -g
        +G
         h
        """)
    out = tp.apply_unified_diff(source, diff)
    # Both lines replaced, others untouched.
    assert "B\n" in out
    assert "G\n" in out
    assert "b\n" not in out.replace("B\n", "")
    assert "g\n" not in out.replace("G\n", "")


def test_unified_diff_delete_line():
    source = "alpha\nbeta\ngamma\n"
    diff = textwrap.dedent("""\
        --- a/x
        +++ b/x
        @@ -1,3 +1,2 @@
         alpha
        -beta
         gamma
        """)
    assert tp.apply_unified_diff(source, diff) == "alpha\ngamma\n"


def test_unified_diff_insert_at_eof():
    source = "alpha\nbeta\ngamma\n"
    diff = textwrap.dedent("""\
        --- a/x
        +++ b/x
        @@ -4,0 +4,1 @@
        +delta
        """)
    assert tp.apply_unified_diff(source, diff) == "alpha\nbeta\ngamma\ndelta\n"


def test_unified_diff_context_mismatch_raises():
    source = "a\nb\nc\n"
    diff = textwrap.dedent("""\
        --- a/x
        +++ b/x
        @@ -1,3 +1,3 @@
         a
        -DIFFERENT
        +X
         c
        """)
    with pytest.raises(tp.PatchNotFound, match="removal line"):
        tp.apply_unified_diff(source, diff)


def test_unified_diff_out_of_range_raises():
    diff = textwrap.dedent("""\
        --- a/x
        +++ b/x
        @@ -5,1 +5,1 @@
        -missing
        +MISSING
        """)
    with pytest.raises(tp.PatchNotFound, match="out of range"):
        tp.apply_unified_diff("a\nb\nc\n", diff)


def test_unified_diff_no_hunks_raises():
    with pytest.raises(tp.PatchMalformed, match="no valid hunk"):
        tp.apply_unified_diff("a\nb\nc\n", "--- a/x\n+++ b/x\n")


def test_unified_diff_preserves_crlf():
    source = "alpha\r\nbeta\r\ngamma\r\n"
    diff = textwrap.dedent("""\
        --- a/x
        +++ b/x
        @@ -1,3 +1,3 @@
         alpha
        -beta
        +BETA
         gamma
        """)
    out = tp.apply_unified_diff(source, diff)
    assert "\r\n" in out
    assert "BETA" in out


def test_unified_diff_trailing_newline_preserved():
    source = "a\nb\nc\n"  # with trailing \n
    diff = textwrap.dedent("""\
        --- a/x
        +++ b/x
        @@ -1,3 +1,3 @@
         a
        -b
        +B
         c
        """)
    assert tp.apply_unified_diff(source, diff).endswith("\n")


def test_unified_diff_does_not_add_trailing_newline():
    source = "a\nb\nc"  # no trailing \n
    diff = textwrap.dedent("""\
        --- a/x
        +++ b/x
        @@ -1,3 +1,3 @@
         a
        -b
        +B
         c
        """)
    assert tp.apply_unified_diff(source, diff) == "a\nB\nc"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  apply_to_file
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_apply_to_file_sr_round_trip(tmp_path):
    f = tmp_path / "x.py"
    f.write_text(SOURCE_GOOD, encoding="utf-8")
    payload = _sr_block(
        "def init_gpio(pin_number):\n"
        "    # Initialize the hardware pin\n"
        "    setup_pin(pin_number, MODE_IN)\n",
        "def init_gpio(pin_number):\n"
        "    # Initialize the hardware pin with Pull-Up resistor\n"
        "    setup_pin(pin_number, MODE_IN, PULL_UP)\n",
    )
    tp.apply_to_file(f, "search_replace", payload)
    assert "PULL_UP" in f.read_text(encoding="utf-8")


def test_apply_to_file_appends_n10_confidence_ledger_row(tmp_path):
    f = tmp_path / "x.py"
    f.write_text(SOURCE_GOOD, encoding="utf-8")
    ledger = tmp_path / "upgrade_rollback_ledger.md"
    ledger.write_text(textwrap.dedent("""\
        # Major Upgrade + Rollback Ledger (N10)

        ## Diff Validation Confidence

        | Applied (UTC) | Path | Patch kind | Layer | Confidence | Disposition | Notes |
        |---|---|---|---:|---:|---|---|
        | _(runtime rows appended by WP.3 patcher; no raw patch payloads stored)_ | | | | | | |

        ## Trigger vocabulary (Rollbacks)
    """), encoding="utf-8")
    payload = _sr_block(
        "def init_gpio(pin_number):\n"
        "    # Initialize the hardware pin\n"
        "    setup_pin(pin_number, MODE_IN)\n",
        "def init_gpio(pin_number):\n"
        "    # Initialize the hardware pin with Pull-Up resistor\n"
        "    setup_pin(pin_number, MODE_IN, PULL_UP)\n",
    )

    tp.apply_to_file(f, "search_replace", payload, ledger_path=ledger)

    ledger_text = ledger.read_text(encoding="utf-8")
    assert "| search_replace | 1 | 1.000 | applied |" in ledger_text
    assert "WP.3 cascade match confidence" in ledger_text
    assert "setup_pin" not in ledger_text


def test_append_diff_validation_confidence_ledger_escapes_cells(tmp_path):
    ledger = tmp_path / "upgrade_rollback_ledger.md"
    ledger.write_text(textwrap.dedent("""\
        # Major Upgrade + Rollback Ledger (N10)

        ## Diff Validation Confidence

        | Applied (UTC) | Path | Patch kind | Layer | Confidence | Disposition | Notes |
        |---|---|---|---:|---:|---|---|
        | _(runtime rows appended by WP.3 patcher; no raw patch payloads stored)_ | | | | | | |

        ## Trigger vocabulary (Rollbacks)
    """), encoding="utf-8")

    tp.append_diff_validation_confidence_ledger(
        tp.DiffValidationLedgerEvent(
            path="backend/agents/tools_patch.py",
            patch_kind="search_replace",
            layer=4,
            score=0.91234,
            notes="fuzzy | no raw payload\nstored",
        ),
        ledger_path=ledger,
        now=datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc),
    )

    ledger_text = ledger.read_text(encoding="utf-8")
    assert (
        "| 2026-05-04T12:00:00Z | backend/agents/tools_patch.py | "
        "search_replace | 4 | 0.912 | applied | fuzzy \\| no raw payload stored |"
    ) in ledger_text


def test_n10_ledger_has_diff_validation_confidence_table():
    ledger = (
        REPO_ROOT / "docs" / "ops" / "upgrade_rollback_ledger.md"
    ).read_text(encoding="utf-8")

    assert "## Diff Validation Confidence" in ledger
    assert "| Applied (UTC) | Path | Patch kind | Layer | Confidence |" in ledger
    assert "Do not store raw" in ledger
    assert "SEARCH / REPLACE payloads" in ledger


def test_apply_to_file_refuses_missing_file(tmp_path):
    with pytest.raises(tp.PatchNotFound, match="does not exist"):
        tp.apply_to_file(
            tmp_path / "no-such.py", "search_replace", _sr_block("a\nb\nc\n", "x\ny\nz\n"),
        )


def test_apply_to_file_unknown_kind_raises(tmp_path):
    f = tmp_path / "x.py"
    f.write_text("hi\n")
    with pytest.raises(tp.PatchMalformed, match="unknown patch_kind"):
        tp.apply_to_file(f, "not-a-kind", "whatever")


def test_apply_to_file_failed_patch_preserves_original(tmp_path):
    f = tmp_path / "x.py"
    original = "alpha\nbeta\ngamma\n"
    f.write_text(original, encoding="utf-8")
    payload = _sr_block("missing\nmissing\nmissing\n", "x\ny\nz\n")

    with pytest.raises(tp.PatchNotFound):
        tp.apply_to_file(f, "search_replace", payload)

    assert f.read_text(encoding="utf-8") == original
    assert list(tmp_path.glob("*.omnisight-patch-tmp")) == []


def test_apply_to_file_is_atomic_leaves_no_tmp(tmp_path):
    f = tmp_path / "x.py"
    f.write_text(SOURCE_GOOD)
    payload = _sr_block(
        "def init_gpio(pin_number):\n"
        "    # Initialize the hardware pin\n"
        "    setup_pin(pin_number, MODE_IN)\n",
        "def init_gpio(pin_number):\n    pass\n    pass\n",
    )
    tp.apply_to_file(f, "search_replace", payload)
    leftover = list(tmp_path.glob("*.omnisight-patch-tmp"))
    assert leftover == []
