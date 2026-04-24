#!/usr/bin/env python3
"""M4 — Per-tenant usage report (billing basis).

Reads the running backend's ``/api/v1/host/accounting`` endpoint (admin
only) and prints a cpu_seconds / mem_gb_seconds / disk_gb / sandbox
table per tenant. Also supports a ``--live`` mode that bypasses the
HTTP layer and walks ``host_metrics.snapshot_accounting()`` directly
for local diagnostic use.

Output formats:
    --format text  (default)   → aligned table for humans
    --format json              → single JSON document, one row per tenant
    --format csv               → CSV with header row, suitable for billing

Examples:

    # Live, local (same host as the backend process)
    python scripts/usage_report.py --live

    # Remote, via HTTP (requires admin bearer token)
    python scripts/usage_report.py \\
        --api-url http://localhost:8000 \\
        --token $ADMIN_TOKEN \\
        --format csv > billing.csv

Exit codes:
    0 — report rendered
    1 — HTTP / import / unparsable response error
    2 — bad CLI args
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import sys
from pathlib import Path
from typing import Any

# Allow running from anywhere: prepend repo root so `import backend.*` works.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _rows_live() -> list[dict[str, Any]]:
    """Pull accounting rows straight out of ``host_metrics``."""
    try:
        from backend import host_metrics as hm
    except ImportError as exc:
        print(f"ERROR: cannot import backend.host_metrics: {exc}", file=sys.stderr)
        sys.exit(1)
    out: list[dict[str, Any]] = []
    for a in hm.snapshot_accounting():
        usage = hm.get_tenant_usage(a.tenant_id)
        out.append({
            "tenant_id": a.tenant_id,
            "cpu_seconds_total": round(a.cpu_seconds_total, 3),
            "mem_gb_seconds_total": round(a.mem_gb_seconds_total, 3),
            "cpu_percent_now": round(usage.cpu_percent, 2),
            "mem_used_gb_now": round(usage.mem_used_gb, 3),
            "disk_used_gb": round(usage.disk_used_gb, 3),
            "sandbox_count": usage.sandbox_count,
            "last_updated": a.last_updated,
        })
    return out


def _rows_http(api_url: str, token: str | None) -> list[dict[str, Any]]:
    """Call ``GET /api/v1/host/accounting`` + merge with ``/host/metrics``.

    Uses urllib (stdlib) so the script works on a bare Python install —
    the backend's ``requests`` / ``httpx`` dependency is *not* imported
    on purpose; billing tooling should stay trivially portable.
    """
    import os
    import urllib.error
    import urllib.request

    # User-Agent is mandatory against CF-fronted prod — the default
    # ``Python-urllib/3.x`` signature is flagged by Cloudflare Bot Fight
    # Mode (Error 1010 "browser_signature_banned"). Override via
    # OMNISIGHT_USAGE_REPORT_UA if the operator needs a different
    # identifier for edge-log attribution.
    ua = os.environ.get(
        "OMNISIGHT_USAGE_REPORT_UA",
        "OmniSight-UsageReport/1.0 (+https://github.com/limit5/OmniSight-Productizer)",
    )
    headers = {"Accept": "application/json", "User-Agent": ua}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    def _get(path: str) -> dict:
        req = urllib.request.Request(api_url.rstrip("/") + path, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            print(f"ERROR: {path} returned {exc.code} {exc.reason}", file=sys.stderr)
            sys.exit(1)
        except Exception as exc:
            print(f"ERROR: {path} request failed: {exc}", file=sys.stderr)
            sys.exit(1)

    acct = _get("/api/v1/host/accounting").get("tenants", [])
    live = _get("/api/v1/host/metrics").get("tenants", [])
    live_by_tid = {r["tenant_id"]: r for r in live}

    merged: list[dict[str, Any]] = []
    for row in acct:
        tid = row["tenant_id"]
        live_row = live_by_tid.get(tid, {})
        merged.append({
            "tenant_id": tid,
            "cpu_seconds_total": row.get("cpu_seconds_total", 0),
            "mem_gb_seconds_total": row.get("mem_gb_seconds_total", 0),
            "cpu_percent_now": live_row.get("cpu_percent", 0),
            "mem_used_gb_now": live_row.get("mem_used_gb", 0),
            "disk_used_gb": live_row.get("disk_used_gb", 0),
            "sandbox_count": live_row.get("sandbox_count", 0),
            "last_updated": row.get("last_updated", 0),
        })
    return merged


def _render_text(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "(no tenants have recorded any usage yet)\n"
    columns = [
        ("tenant_id", "Tenant", 24),
        ("cpu_seconds_total", "cpu_s", 12),
        ("mem_gb_seconds_total", "mem_GiB·s", 14),
        ("cpu_percent_now", "cpu%(now)", 10),
        ("mem_used_gb_now", "mem_GiB(now)", 14),
        ("disk_used_gb", "disk_GiB", 10),
        ("sandbox_count", "sandboxes", 10),
    ]
    buf = io.StringIO()
    buf.write("  ".join(f"{title:<{w}}" for _, title, w in columns) + "\n")
    buf.write("  ".join("-" * w for _, _, w in columns) + "\n")
    for row in rows:
        parts = []
        for key, _, w in columns:
            val = row.get(key, "")
            if isinstance(val, float):
                text = f"{val:,.2f}"
            else:
                text = str(val)
            parts.append(f"{text:<{w}}")
        buf.write("  ".join(parts) + "\n")
    return buf.getvalue()


def _render_csv(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return ""
    buf = io.StringIO()
    fieldnames = [
        "tenant_id", "cpu_seconds_total", "mem_gb_seconds_total",
        "cpu_percent_now", "mem_used_gb_now", "disk_used_gb",
        "sandbox_count", "last_updated",
    ]
    writer = csv.DictWriter(buf, fieldnames=fieldnames)
    writer.writeheader()
    for row in rows:
        writer.writerow({k: row.get(k, "") for k in fieldnames})
    return buf.getvalue()


def render(rows: list[dict[str, Any]], fmt: str) -> str:
    if fmt == "json":
        return json.dumps(rows, indent=2)
    if fmt == "csv":
        return _render_csv(rows)
    return _render_text(rows)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Per-tenant usage report (M4 billing basis)",
    )
    p.add_argument("--api-url", default="http://localhost:8000",
                   help="backend base URL (default: http://localhost:8000)")
    p.add_argument("--token", default=None,
                   help="admin bearer token (omit with --live)")
    p.add_argument("--live", action="store_true",
                   help="read from in-process backend.host_metrics instead of HTTP")
    p.add_argument("--format", choices=("text", "json", "csv"), default="text")
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    rows = _rows_live() if args.live else _rows_http(args.api_url, args.token)
    sys.stdout.write(render(rows, args.format))
    return 0


if __name__ == "__main__":
    sys.exit(main())
