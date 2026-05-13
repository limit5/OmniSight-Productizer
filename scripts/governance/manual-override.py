#!/usr/bin/env python3
"""Manual override CLI for OmniSight governance preflight failures.

L1-authorized override of one of the 10 forbidden-combination rules,
recorded to an append-only JSONL audit log. Minimum viable path per
ADR-0034 §1; the full review-window flow lands in G.C.

Spec : docs/sprint-s12/sprint-s12g-governance-engine-spec.md §5 (G.A-v0 #6)
ADR  : docs/adr/ADR-0034 §1 (override audit trail)

Usage:
    manual-override.py apply --ticket OP-1234 --rule-id 3 \\
        --reason "..." --l1-fingerprint <hex>
    manual-override.py self-test
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_FINGERPRINT_MISSING = 2
EXIT_FINGERPRINT_MISMATCH = 3
EXIT_VALIDATION = 4

DEFAULT_FINGERPRINT_PATH = Path("~/.config/omnisight/governance-l1-fingerprint").expanduser()
DEFAULT_AUDIT_DIR = Path("~/.config/omnisight/governance-overrides").expanduser()
DEFAULT_AUDIT_LOG = DEFAULT_AUDIT_DIR / "overrides.jsonl"

REASON_MAX_LEN = 500
RULE_ID_MIN = 1
RULE_ID_MAX = 10


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _operator_uid() -> str:
    try:
        return os.getlogin()
    except OSError:
        import pwd
        try:
            return pwd.getpwuid(os.getuid()).pw_name
        except KeyError:
            return f"uid:{os.getuid()}"


def _sha256_hex(data: str) -> str:
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _read_expected_fingerprint(path: Path) -> str | None:
    if not path.is_file():
        return None
    return path.read_text(encoding="utf-8").strip()


def _validate_ticket(ticket: str) -> str | None:
    if not ticket or not ticket.startswith("OP-"):
        return f"--ticket must be of form OP-XXXX (got {ticket!r})"
    suffix = ticket[3:]
    if not suffix or not suffix.isdigit():
        return f"--ticket suffix must be digits (got {ticket!r})"
    return None


def _validate_rule_id(rule_id: int) -> str | None:
    if not (RULE_ID_MIN <= rule_id <= RULE_ID_MAX):
        return f"--rule-id must be {RULE_ID_MIN}..{RULE_ID_MAX} (got {rule_id})"
    return None


def _validate_reason(reason: str) -> str | None:
    if not reason or not reason.strip():
        return "--reason must be non-empty"
    if len(reason) > REASON_MAX_LEN:
        return f"--reason exceeds {REASON_MAX_LEN} chars (got {len(reason)})"
    return None


def _ensure_audit_log(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except PermissionError:
        pass
    if not path.exists():
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
    else:
        os.chmod(path, 0o600)


def _append_audit_line(path: Path, record: dict) -> None:
    _ensure_audit_log(path)
    line = json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
    fd = os.open(str(path), os.O_WRONLY | os.O_APPEND)
    try:
        os.write(fd, line.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)


def cmd_apply(args: argparse.Namespace) -> int:
    fingerprint_path = Path(args.fingerprint_path).expanduser() if args.fingerprint_path else DEFAULT_FINGERPRINT_PATH
    audit_log = Path(args.audit_log).expanduser() if args.audit_log else DEFAULT_AUDIT_LOG

    expected = _read_expected_fingerprint(fingerprint_path)
    if expected is None:
        print(f"refuse: expected L1 fingerprint file not found at {fingerprint_path}", file=sys.stderr)
        return EXIT_FINGERPRINT_MISSING

    if args.l1_fingerprint != expected:
        print("refuse: --l1-fingerprint does not match expected L1 fingerprint", file=sys.stderr)
        return EXIT_FINGERPRINT_MISMATCH

    for err in (
        _validate_ticket(args.ticket),
        _validate_rule_id(args.rule_id),
        _validate_reason(args.reason),
    ):
        if err:
            print(f"refuse: {err}", file=sys.stderr)
            return EXIT_VALIDATION

    record = {
        "ts_utc": _utc_now_iso(),
        "ticket": args.ticket,
        "rule_id": args.rule_id,
        "reason": args.reason,
        "l1_fingerprint_hash": _sha256_hex(args.l1_fingerprint),
        "operator_uid": _operator_uid(),
        "hostname": socket.gethostname(),
    }
    _append_audit_line(audit_log, record)
    summary = json.dumps(record, sort_keys=True, separators=(",", ":"))
    print(f"override recorded: {summary}")
    print(f"audit log: {audit_log}")
    return EXIT_OK


def cmd_self_test(_args: argparse.Namespace) -> int:
    fixture_fp = "DEADBEEFCAFE" * 4
    audit_log = Path("/tmp/test-overrides.jsonl")
    fp_handle = tempfile.NamedTemporaryFile(
        "w", prefix="manual-override-selftest-", suffix=".fp", delete=False
    )
    fp_handle.write(fixture_fp + "\n")
    fp_handle.close()
    fp_path = Path(fp_handle.name)
    cleanup_paths = [fp_path, audit_log]

    try:
        missing = Path(tempfile.gettempdir()) / "manual-override-selftest-absent.fp"
        if missing.exists():
            missing.unlink()
        assert _read_expected_fingerprint(missing) is None, "missing FP file should return None"

        if audit_log.exists():
            audit_log.unlink()
        rc = cmd_apply(argparse.Namespace(
            ticket="OP-0000", rule_id=1, reason="self-test mismatch",
            l1_fingerprint="00", fingerprint_path=str(fp_path),
            audit_log=str(audit_log),
        ))
        assert rc == EXIT_FINGERPRINT_MISMATCH, f"mismatch: want {EXIT_FINGERPRINT_MISMATCH}, got {rc}"
        assert not audit_log.exists(), "audit log must not exist after mismatch"

        rc = cmd_apply(argparse.Namespace(
            ticket="OP-9999", rule_id=5, reason="self-test override (fixture)",
            l1_fingerprint=fixture_fp, fingerprint_path=str(fp_path),
            audit_log=str(audit_log),
        ))
        assert rc == EXIT_OK, f"success: want {EXIT_OK}, got {rc}"
        assert audit_log.exists(), "audit log must exist after success"
        rec = json.loads(audit_log.read_text(encoding="utf-8").strip().splitlines()[-1])
        assert rec["ticket"] == "OP-9999"
        assert rec["rule_id"] == 5
        assert rec["l1_fingerprint_hash"] == _sha256_hex(fixture_fp)
        for f in ("ts_utc", "operator_uid", "hostname", "reason"):
            assert f in rec, f"missing field {f}"
        mode = audit_log.stat().st_mode & 0o777
        assert mode == 0o600, f"audit log mode: want 0600, got {oct(mode)}"

        before = audit_log.read_text(encoding="utf-8")
        rc = cmd_apply(argparse.Namespace(
            ticket="OP-9999", rule_id=99, reason="self-test out of range",
            l1_fingerprint=fixture_fp, fingerprint_path=str(fp_path),
            audit_log=str(audit_log),
        ))
        assert rc == EXIT_VALIDATION, f"rule-id OOB: want {EXIT_VALIDATION}, got {rc}"
        assert audit_log.read_text(encoding="utf-8") == before, "audit log mutated on validation failure"

        fp_path.unlink()
        rc = cmd_apply(argparse.Namespace(
            ticket="OP-9999", rule_id=1, reason="self-test missing FP",
            l1_fingerprint=fixture_fp, fingerprint_path=str(fp_path),
            audit_log=str(audit_log),
        ))
        assert rc == EXIT_FINGERPRINT_MISSING, f"missing FP: want {EXIT_FINGERPRINT_MISSING}, got {rc}"

        print("self-test PASS")
        return EXIT_OK
    finally:
        for p in cleanup_paths:
            try:
                p.unlink()
            except FileNotFoundError:
                pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="manual-override.py",
        description="L1-authorized override of governance preflight failures (ADR-0034 §1).",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_apply = sub.add_parser("apply", help="Record an L1-authorized override")
    p_apply.add_argument("--ticket", required=True, help="JIRA ticket key, e.g., OP-1234")
    p_apply.add_argument("--rule-id", required=True, type=int, help="Forbidden-combination rule id (1..10)")
    p_apply.add_argument("--reason", required=True, help="Override justification (1..500 chars)")
    p_apply.add_argument("--l1-fingerprint", required=True, help="Raw L1 GPG fingerprint (hex)")
    p_apply.add_argument("--fingerprint-path", default=None, help=argparse.SUPPRESS)
    p_apply.add_argument("--audit-log", default=None, help=argparse.SUPPRESS)
    p_apply.set_defaults(func=cmd_apply)

    p_test = sub.add_parser("self-test", help="End-to-end self-test against /tmp/test-overrides.jsonl")
    p_test.set_defaults(func=cmd_self_test)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
