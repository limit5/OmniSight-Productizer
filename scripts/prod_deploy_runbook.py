#!/usr/bin/env python3
"""OP-881 D9 -- CLI counterpart to ``POST /api/v1/prod/deploy``.

Drives :class:`backend.orchestrator.prod_deploy.ProdDeployOrchestrator`
directly from the operator runbook so an ad-hoc deploy can be triggered
without going through the FastAPI surface (useful when the API host is
itself the target of the deploy, or during DR drills).

Usage:

    python scripts/prod_deploy_runbook.py \\
        --release-id 2026.05.11-canary \\
        --image-tag v2026.05.11 \\
        --reason 'manual sandbox-mirror DoD run' \\
        --approval-token <slack-dm-token> \\
        --actor op@example.test

The approval gate still applies. Pass
``--insecure-skip-webhook-signature`` ONLY in a sandboxed mirror where
the webhook secret is unavailable (the flag is logged loudly and
recorded in the audit row's context).
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import logging
import os
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _sign(body: bytes) -> str:
    secret = os.environ.get("OMNISIGHT_PROD_DEPLOY_WEBHOOK_SECRET", "").encode()
    if not secret:
        return ""
    return "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--release-id", required=True)
    parser.add_argument("--image-tag", required=True)
    parser.add_argument("--reason", required=True)
    parser.add_argument("--approval-token", required=True)
    parser.add_argument("--actor", required=True)
    parser.add_argument(
        "--insecure-skip-webhook-signature",
        action="store_true",
        help="sandbox-mirror DoD only: skip signature verification",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    from backend.orchestrator import prod_deploy

    body = json.dumps(
        {
            "release_id": args.release_id,
            "image_tag": args.image_tag,
            "reason": args.reason,
            "approval_token": args.approval_token,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()

    if args.insecure_skip_webhook_signature:
        logging.warning(
            "INSECURE: --insecure-skip-webhook-signature is set; "
            "this run will write a context flag into prod_deploy_audit."
        )
        signature = ""
        os.environ["OMNISIGHT_PROD_DEPLOY_WEBHOOK_SECRET"] = ""
    else:
        signature = _sign(body)

    orch = prod_deploy.ProdDeployOrchestrator(
        steps=prod_deploy.build_default_steps(),
    )
    try:
        result = orch.execute(
            release_id=args.release_id,
            image_tag=args.image_tag,
            actor=args.actor,
            reason=args.reason,
            approval_token=args.approval_token,
            body_bytes=body,
            signature_header=signature,
        )
    except prod_deploy.ProdDeployError as exc:
        logging.error(
            "prod-deploy aborted: %s — %s", exc.__class__.__name__, exc
        )
        print(json.dumps({
            "status": exc.abort_status,
            "error_class": exc.__class__.__name__,
            "error_message": str(exc),
        }))
        return 2

    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
