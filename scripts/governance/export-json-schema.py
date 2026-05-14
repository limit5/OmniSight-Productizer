#!/usr/bin/env python3
"""Export the v1 ticket contract as canonical JSON Schema.

Re-run `python3 scripts/governance/export-json-schema.py` after editing
v1.py. Pre-commit hook (future ticket) will enforce.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUT = Path("governance_engine/schema/v1.schema.json")
SCHEMA_URI = "https://json-schema.org/draft/2020-12/schema"
SCHEMA_ID = "https://omnisight.example/schemas/ticket-contract/v1"
MAX_INLINE_CHARS = 180


def _render_json(value: object, level: int = 0) -> str:
    inline = json.dumps(value, sort_keys=True, separators=(", ", ": "))
    if len(inline) <= MAX_INLINE_CHARS:
        return inline

    prefix = "  " * level
    child_prefix = "  " * (level + 1)
    if isinstance(value, dict):
        lines = [
            f"{child_prefix}{json.dumps(key)}: {_render_json(value[key], level + 1)}"
            for key in sorted(value)
        ]
        return "{\n" + ",\n".join(lines) + "\n" + prefix + "}"
    if isinstance(value, list):
        lines = [f"{child_prefix}{_render_json(item, level + 1)}" for item in value]
        return "[\n" + ",\n".join(lines) + "\n" + prefix + "]"
    return json.dumps(value)


def _schema_bytes() -> bytes:
    sys.path.insert(0, str(REPO_ROOT))
    from governance_engine.schema.v1 import TicketContractV1

    schema = TicketContractV1.model_json_schema(mode="serialization")
    schema["$schema"] = SCHEMA_URI
    schema["$id"] = SCHEMA_ID
    return (_render_json(schema) + "\n").encode("utf-8")


def _resolve_out(path: str) -> Path:
    out = Path(path)
    if not out.is_absolute():
        out = REPO_ROOT / out
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Export governance_engine.schema.v1.TicketContractV1 as JSON Schema.",
    )
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="Output JSON Schema path.")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Compare generated schema against --out and fail if it differs.",
    )
    args = parser.parse_args(argv)
    out = _resolve_out(args.out)
    rendered = _schema_bytes()
    if args.check:
        try:
            current = out.read_bytes()
        except FileNotFoundError:
            print(f"schema drift: {out} does not exist", file=sys.stderr)
            return 1
        if current != rendered:
            print(f"schema drift: {out} is not up to date", file=sys.stderr)
            return 1
        return 0
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
