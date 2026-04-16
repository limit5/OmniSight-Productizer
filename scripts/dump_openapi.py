#!/usr/bin/env python3
"""N3 — dump the FastAPI OpenAPI schema to disk.

Used by:
  * CI `openapi-contract` job — to regenerate and compare against the
    committed snapshot (`openapi.json`); any drift fails the build.
  * Local dev — to refresh `openapi.json` + `lib/generated/api-types.ts`
    after editing a Pydantic model or route signature.

We call `app.openapi()` directly instead of booting uvicorn + curl so
the script runs offline and stays fast (~1s vs ~10s). This does NOT
execute the lifespan (no DB / no network) — only route & schema
introspection, which is exactly what OpenAPI generation needs.

Usage:
  python scripts/dump_openapi.py                 # writes ./openapi.json
  python scripts/dump_openapi.py --out /tmp/x.json
  python scripts/dump_openapi.py --check         # exit 1 if on-disk drift
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

# Ensure the repo root is on sys.path regardless of the caller's cwd. CI
# invokes from the repo root and devs from anywhere, so be defensive.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _load_schema() -> dict:
    # Import lazily so `--help` works even if backend deps are missing.
    # Startup-config validation in backend.config trips on missing envs
    # (decision bearer, provider keys) when debug=False. For schema
    # introspection we don't need any of that — force debug=True so
    # validate_startup_config() stays lenient even on CI runners.
    os.environ.setdefault("OMNISIGHT_DEBUG", "true")
    from backend.main import app

    schema = app.openapi()
    # Drop FastAPI's build-time version bump so the snapshot only moves
    # when a real schema change lands. The app version is tracked in
    # pyproject anyway.
    schema.pop("info", None)
    schema["info"] = {"title": "OmniSight Engine API", "version": "contract"}
    return schema


def _canonical(schema: dict) -> str:
    # Sort keys so diffs are stable across Python runs. Trailing newline
    # so editors / `git diff` behave.
    return json.dumps(schema, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--out",
        default="openapi.json",
        help="Output path (default: ./openapi.json at repo root)",
    )
    ap.add_argument(
        "--check",
        action="store_true",
        help="Do not write — compare current schema against the file at --out; "
        "exit 1 on drift. Intended for CI.",
    )
    args = ap.parse_args()

    schema_text = _canonical(_load_schema())
    out = Path(args.out)

    if args.check:
        if not out.exists():
            print(f"[dump_openapi] --check: {out} does not exist", file=sys.stderr)
            return 1
        current = out.read_text(encoding="utf-8")
        if current == schema_text:
            print(f"[dump_openapi] {out}: up to date")
            return 0
        print(
            f"[dump_openapi] DRIFT — committed {out} differs from generated schema.\n"
            f"Run: python scripts/dump_openapi.py && pnpm run openapi:types",
            file=sys.stderr,
        )
        # Print a short diff for the PR reviewer.
        try:
            diff = subprocess.run(
                ["diff", "-u", str(out), "-"],
                input=schema_text,
                capture_output=True,
                text=True,
            )
            sys.stderr.write(diff.stdout[:4000])
        except Exception:
            pass
        return 1

    out.write_text(schema_text, encoding="utf-8")
    print(f"[dump_openapi] wrote {out} ({len(schema_text):,} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
