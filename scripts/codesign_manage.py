#!/usr/bin/env python3
"""P3 #288 — Codesign store management CLI.

Operator-facing helper for :mod:`backend.codesign_store`.  Does NOT
materialise plaintext key material — ``show``/``list`` always print
redacted views, ``decrypt`` is intentionally omitted (callers that need
plaintext must use the module API and take responsibility).

Usage:
    python3 scripts/codesign_manage.py list
    python3 scripts/codesign_manage.py show <cert_id>
    python3 scripts/codesign_manage.py expiries [--now <unix_ts>]
    python3 scripts/codesign_manage.py audit [--cert-id <cert_id>]
    python3 scripts/codesign_manage.py audit-verify

Exit codes:
    0 — success
    2 — usage / argument error
    3 — cert not found
    4 — audit chain tampering detected
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from backend import codesign_store as cs  # noqa: E402


def cmd_list(_: argparse.Namespace) -> int:
    records = cs.get_store().list_redacted()
    if not records:
        print("(no certs registered)")
        return 0
    for r in records:
        print(
            f"{r['cert_id']:<28} {r['kind']:<34} "
            f"HSM={r['hsm_vendor']:<9} expires={_fmt_ts(r['not_after'])}",
        )
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    try:
        rec = cs.get_store().get(args.cert_id)
    except cs.UnknownCertError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 3
    view = cs.redacted_view(rec)
    print(json.dumps(view, indent=2, sort_keys=True))
    return 0


def cmd_expiries(args: argparse.Namespace) -> int:
    findings = cs.check_cert_expiries(now=args.now)
    if not findings:
        print("(no certs within 30 days of expiry)")
        return 0
    for f in findings:
        print(
            f"[{f.severity:<8}] {f.cert_id:<28} "
            f"days_left={f.days_left:>7.2f} threshold={f.threshold_days}d "
            f"kind={f.cert_kind}",
        )
    return 0


def cmd_audit(args: argparse.Namespace) -> int:
    chain = cs.get_global_audit_chain()
    entries = (
        chain.for_cert(args.cert_id) if args.cert_id else chain.entries
    )
    if not entries:
        print("(no audit entries)")
        return 0
    for e in entries:
        print(
            f"ts={_fmt_ts(e['ts'])} cert={e['cert_id']} "
            f"actor={e['actor']:<20} reason={e['reason_code']:<10} "
            f"artifact={e['artifact_sha256'][:16]}… "
            f"head={e['curr_hash'][:12]}…",
        )
    return 0


def cmd_audit_verify(_: argparse.Namespace) -> int:
    chain = cs.get_global_audit_chain()
    ok, bad = chain.verify()
    if ok:
        print(f"chain OK ({len(chain.entries)} entries, head={chain.head()[:12]}…)")
        return 0
    print(f"CHAIN TAMPERED at entry index {bad}", file=sys.stderr)
    return 4


def _fmt_ts(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="codesign_manage")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="list registered certs (redacted)")

    p_show = sub.add_parser("show", help="show one cert (redacted)")
    p_show.add_argument("cert_id")

    p_exp = sub.add_parser("expiries", help="certs within 30 days of expiry")
    p_exp.add_argument("--now", type=float, default=None)

    p_aud = sub.add_parser("audit", help="print audit chain entries")
    p_aud.add_argument("--cert-id", default=None)

    sub.add_parser("audit-verify", help="verify audit chain integrity")
    return p


_HANDLERS = {
    "list": cmd_list,
    "show": cmd_show,
    "expiries": cmd_expiries,
    "audit": cmd_audit,
    "audit-verify": cmd_audit_verify,
}


def main(argv: list[str]) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv[1:])
    return _HANDLERS[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
