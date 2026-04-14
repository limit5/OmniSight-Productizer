"""Phase 67-B S2 — tool wrappers: patch_file / create_file / write_file
deprecation interceptor + IIS signal integration."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

# The @tool decorator wraps callables; the real body is at .func
# (LangChain convention).
from backend.agents.tools import create_file, patch_file, write_file


def _run(tool_coro):
    """Invoke a @tool via its underlying async function. Different
    LangChain versions expose either .coroutine, .func, or allow
    direct ainvoke; try in order."""
    return tool_coro


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Fixture
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.fixture()
def active_ws(tmp_path, monkeypatch):
    """Install `tmp_path` as the active workspace root for _safe_path."""
    from backend.agents.tools import set_active_workspace
    set_active_workspace(tmp_path)
    yield tmp_path
    set_active_workspace(None)


async def _invoke(tool_fn, **kwargs):
    """LangChain @tool: the coroutine lives at .coroutine in newer
    versions. Fall back to .func if needed."""
    coro = getattr(tool_fn, "coroutine", None) or getattr(tool_fn, "func", None)
    if coro is None:
        # Some versions expose the tool directly as a callable.
        return await tool_fn.ainvoke(kwargs)
    return await coro(**kwargs)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  create_file — new files only
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.mark.asyncio
async def test_create_file_writes_new_file(active_ws):
    out = await _invoke(create_file, path="new.py", content="# hi\n")
    assert "[OK] Created" in out
    assert (active_ws / "new.py").read_text() == "# hi\n"


@pytest.mark.asyncio
async def test_create_file_refuses_existing(active_ws):
    (active_ws / "exists.py").write_text("# old")
    out = await _invoke(create_file, path="exists.py", content="# new")
    assert "[REJECTED]" in out
    assert "patch_file" in out
    # Old content unchanged.
    assert (active_ws / "exists.py").read_text() == "# old"


@pytest.mark.asyncio
async def test_create_file_uncapped_for_big_templates(active_ws):
    """Generated boilerplate (e.g., long __init__.py) must work."""
    big = "x = 1\n" * 500
    out = await _invoke(create_file, path="big.py", content=big)
    assert "[OK] Created" in out


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  write_file — deprecation interceptor on existing files
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.mark.asyncio
async def test_write_file_new_path_still_works(active_ws):
    """New file path → accepted (write_file remains usable for first-
    time writes; deprecation only applies to EXISTING file overwrites)."""
    out = await _invoke(write_file, path="fresh.py", content="hi\n")
    assert "[OK] Written" in out


@pytest.mark.asyncio
async def test_write_file_small_edit_to_existing_still_works(active_ws):
    (active_ws / "s.py").write_text("one\n")
    out = await _invoke(write_file, path="s.py",
                         content="one\ntwo\nthree\n")  # 3 lines, under cap
    assert "[OK] Written" in out


@pytest.mark.asyncio
async def test_write_file_big_overwrite_rejected_with_guidance(active_ws):
    (active_ws / "big.py").write_text("old\n")
    body = "line\n" * 200  # way over cap
    out = await _invoke(write_file, path="big.py", content=body)
    assert "[REJECTED]" in out
    assert "patch_file" in out
    # Original content untouched.
    assert (active_ws / "big.py").read_text() == "old\n"


@pytest.mark.asyncio
async def test_write_file_cap_respects_env(active_ws, monkeypatch):
    monkeypatch.setenv("OMNISIGHT_PATCH_MAX_INLINE_LINES", "10")
    (active_ws / "x.py").write_text("v1\n")
    # 15 lines — over the tightened cap.
    body = "line\n" * 15
    out = await _invoke(write_file, path="x.py", content=body)
    assert "[REJECTED]" in out


@pytest.mark.asyncio
async def test_write_file_interceptor_bumps_iis(active_ws, monkeypatch):
    """An oversize overwrite feeds the IIS signal layer (as code_pass=False)
    so Phase 63-B can escalate to L1 calibrate."""
    from backend.agents import tools as _t
    monkeypatch.setattr(_t, "get_active_agent_id", lambda: "a-cap")

    (active_ws / "y.py").write_text("old\n")

    from backend import intelligence as iis
    iis.reset_for_tests()
    await _invoke(write_file, path="y.py", content="line\n" * 200)

    w = iis.get_window("a-cap")
    assert w.code_pass_rate() == 0.0  # one false recorded


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  patch_file
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.mark.asyncio
async def test_patch_file_search_replace_happy(active_ws):
    src = textwrap.dedent("""\
        def hello(name):
            # greeting line
            return "hi " + name
    """)
    (active_ws / "g.py").write_text(src)
    payload = (
        "<<<<<<< SEARCH\n"
        "def hello(name):\n"
        "    # greeting line\n"
        "    return \"hi \" + name\n"
        "=======\n"
        "def hello(name):\n"
        "    # greeting line — louder\n"
        "    return \"HI \" + name.upper()\n"
        ">>>>>>> REPLACE"
    )
    out = await _invoke(patch_file, path="g.py",
                         patch_kind="search_replace", payload=payload)
    assert "[OK] Patched" in out
    assert "HI " in (active_ws / "g.py").read_text()


@pytest.mark.asyncio
async def test_patch_file_missing_target(active_ws):
    payload = (
        "<<<<<<< SEARCH\na\nb\nc\n=======\nx\ny\nz\n>>>>>>> REPLACE"
    )
    out = await _invoke(patch_file, path="ghost.py",
                         patch_kind="search_replace", payload=payload)
    assert "[REJECTED]" in out
    assert "create_file" in out


@pytest.mark.asyncio
async def test_patch_file_not_found_feeds_iis(active_ws, monkeypatch):
    """Patch failures (search block doesn't match) are quality incidents;
    feed the IIS window."""
    from backend.agents import tools as _t
    monkeypatch.setattr(_t, "get_active_agent_id", lambda: "a-patch-fail")

    (active_ws / "h.py").write_text("alpha\nbeta\ngamma\n")
    payload = (
        "<<<<<<< SEARCH\n"
        "totally\nnot\npresent\n"
        "=======\n"
        "still\nnot\nhere\n"
        ">>>>>>> REPLACE"
    )
    from backend import intelligence as iis
    iis.reset_for_tests()
    out = await _invoke(patch_file, path="h.py",
                         patch_kind="search_replace", payload=payload)
    assert "[PATCH-FAILED]" in out
    assert "PatchNotFound" in out
    w = iis.get_window("a-patch-fail")
    assert w.code_pass_rate() == 0.0


@pytest.mark.asyncio
async def test_patch_file_unified_diff_roundtrip(active_ws):
    (active_ws / "u.py").write_text("alpha\nbeta\ngamma\n")
    diff = textwrap.dedent("""\
        --- a/u.py
        +++ b/u.py
        @@ -1,3 +1,3 @@
         alpha
        -beta
        +BETA
         gamma
    """)
    out = await _invoke(patch_file, path="u.py",
                         patch_kind="unified_diff", payload=diff)
    assert "[OK] Patched" in out
    assert "BETA" in (active_ws / "u.py").read_text()
