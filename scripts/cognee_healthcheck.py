#!/usr/bin/env python3
"""OP-899 read-only Cognee runtime healthcheck."""

from __future__ import annotations

import json
import sys

from backend.agents.cognee_integration import healthcheck


def main() -> int:
    result = healthcheck()
    print(
        json.dumps(
            {
                "ok": result.ok,
                "status": result.status,
                "detail": result.detail,
            },
            sort_keys=True,
        )
    )
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
