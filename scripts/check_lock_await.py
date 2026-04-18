#!/usr/bin/env python3
"""Fix-B B7 — detect `await` while holding a threading.Lock.

Scans `backend/**.py` for any `with _*lock:` block and flags the block
if it contains an `await`. This would deadlock the event loop: the
lock is sync, so the event loop can't progress to wake the awaited
coroutine, and other coroutines can't run either.

Exit code: 0 if clean, 1 if any offender.

Conservative regex-based scanner — false positives are possible on
exotic indentation. Suppress with `# lock-await-ok` on the `await`
line if the case is genuinely safe (e.g. you reacquire the lock).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

LOCK_ENTER = re.compile(r"^(\s*)with\s+[\w.]*_lock\s*:")
AWAIT_LINE = re.compile(r"^\s*.*\bawait\b")
SELF_TEST_MARKER = "# lock-await-self-test"


def scan_file(path: Path) -> list[tuple[int, str]]:
    offenders: list[tuple[int, str]] = []
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        m = LOCK_ENTER.match(lines[i])
        if not m:
            i += 1
            continue
        indent = len(m.group(1))
        j = i + 1
        # Walk until we leave the indented block
        while j < len(lines):
            stripped = lines[j].rstrip()
            if stripped and (len(stripped) - len(stripped.lstrip())) <= indent:
                break
            if AWAIT_LINE.match(lines[j]) and "lock-await-ok" not in lines[j]:
                offenders.append((j + 1, lines[j].strip()))
            j += 1
        i = j
    return offenders


def main() -> int:
    root = Path(__file__).resolve().parent.parent / "backend"
    if not root.is_dir():
        print(f"backend dir not found at {root}", file=sys.stderr)
        return 2
    bad = 0
    for p in sorted(root.rglob("*.py")):
        if "tests" in p.parts or "alembic" in p.parts:
            continue
        for lineno, snippet in scan_file(p):
            print(f"{p.relative_to(root.parent)}:{lineno}: await inside `with _lock:` → {snippet}")
            bad += 1
    if bad:
        print(f"\n{bad} offender(s). Fix by moving `await` outside the lock, "
              f"or annotate with `# lock-await-ok` if verified safe.",
              file=sys.stderr)
        return 1
    print("scripts/check_lock_await.py: clean ✓")
    return 0


def _self_test() -> None:
    """Self-test via inline fixtures."""
    import tempfile
    import textwrap
    bad_code = textwrap.dedent("""\
        async def f():
            with _x_lock:
                await g()
    """)
    good_code = textwrap.dedent("""\
        async def f():
            await before()
            with _x_lock:
                local = compute()
            await after(local)
    """)
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as tmp:
        tmp.write(bad_code)
        bad_path = Path(tmp.name)
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as tmp:
        tmp.write(good_code)
        good_path = Path(tmp.name)
    assert scan_file(bad_path), "expected offender in bad fixture"
    assert not scan_file(good_path), "unexpected offender in good fixture"
    bad_path.unlink()
    good_path.unlink()
    print("self-test ✓")


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        _self_test()
        sys.exit(0)
    sys.exit(main())
