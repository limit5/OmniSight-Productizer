#!/usr/bin/env python3
"""OP-1843 — build a configured external product source."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from backend.agents.product_build import build_product_source_sync
from backend.agents.product_source import resolve_product_source


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build an external product source at its pinned ref"
    )
    parser.add_argument("project_key", help="JIRA project key, e.g. DEMO")
    parser.add_argument(
        "--workspace",
        default=".artifacts/product-build",
        help="workspace directory",
    )
    parser.add_argument("--tenant-id", help="tenant id for git_accounts lookup")
    args = parser.parse_args(argv)

    source = resolve_product_source(args.project_key)
    result = build_product_source_sync(
        args.project_key,
        workspace_root=Path(args.workspace),
        tenant_id=args.tenant_id,
    )
    payload = {
        "project_key": args.project_key,
        "source": {
            "repo_url": source.repo_url if source else "",
            "tier": source.tier if source else "",
            "branch": source.branch if source else None,
            "pinned_ref": source.pinned_ref if source else "",
            "git_account_ref": source.git_account_ref if source else None,
        },
        "build": {
            "apk_path": str(result.apk_path),
            "tier": result.tier,
            "pinned_ref": result.pinned_ref,
            "repo_url": result.repo_url,
            "module": result.module,
        },
    }
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
