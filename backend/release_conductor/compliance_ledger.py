"""OP-952 H7 — append-only compliance audit ledger."""
from __future__ import annotations

import csv
import hashlib
import hmac
import json
import logging
import os
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import sqlalchemy as sa


logger = logging.getLogger(__name__)

ZERO_HASH = "0" * 64
SIGNATURE_VERSION = 1


class LedgerTriggerBypassed(RuntimeError):
    """DB append-only trigger rejected an UPDATE or DELETE."""


class ChainBroken(RuntimeError):
    """A ledger row's hash chain does not match its persisted values."""


class ExportFailed(RuntimeError):
    """Daily export failed and should be retried by the next run."""


@dataclass(frozen=True)
class LedgerSigningKey:
    key_id: str
    secret: bytes


_test_engine: sa.Engine | None = None
_prod_engine: sa.Engine | None = None


def set_engine_for_tests(engine: sa.Engine | None) -> None:
    global _test_engine
    _test_engine = engine


def _engine() -> sa.Engine:
    if _test_engine is not None:
        return _test_engine
    global _prod_engine
    if _prod_engine is None:
        url = os.environ.get("OMNISIGHT_DATABASE_URL", "sqlite:///:memory:")
        _prod_engine = sa.create_engine(url, future=True)
    return _prod_engine


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def evidence_hash(payload: Any) -> str:
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _canonical_payload(
    *,
    ts: str,
    actor: str,
    action: str,
    release_id: str,
    before_state: str | None,
    after_state: str | None,
    reason: str,
    evidence_hash_value: str,
    prev_row_hash: str,
) -> str:
    return json.dumps(
        {
            "ts": ts,
            "actor": actor,
            "action": action,
            "release_id": release_id,
            "before_state": before_state,
            "after_state": after_state,
            "reason": reason,
            "evidence_hash": evidence_hash_value,
            "prev_row_hash": prev_row_hash,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def compute_row_hash(
    *,
    ts: str,
    actor: str,
    action: str,
    release_id: str,
    before_state: str | None,
    after_state: str | None,
    reason: str,
    evidence_hash_value: str,
    prev_row_hash: str,
) -> str:
    canonical = _canonical_payload(
        ts=ts,
        actor=actor,
        action=action,
        release_id=release_id,
        before_state=before_state,
        after_state=after_state,
        reason=reason,
        evidence_hash_value=evidence_hash_value,
        prev_row_hash=prev_row_hash,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _clean(value: str | None, *, default: str = "") -> str:
    text = (value or "").strip()
    return text or default


def _missing_table(exc: sa.exc.DBAPIError) -> bool:
    message = str(exc).lower()
    return "release_compliance_ledger" in message and (
        "no such table" in message or "undefinedtable" in message
    )


def record(
    *,
    actor: str,
    action: str,
    release_id: str,
    before_state: str | None,
    after_state: str | None,
    reason: str,
    evidence: Any,
    conn: sa.Connection | None = None,
) -> dict[str, Any] | None:
    """Append one compliance ledger row.

    Returns the inserted row dict. If older test schemas have not
    created the OP-952 table yet, the write is skipped with a warning
    so pre-existing H3/H4/H5 tests remain isolated to their own
    migrations; production deployments apply 0234 before runtime.
    """
    actor_clean = _clean(actor, default="system")
    action_clean = _clean(action, default="unknown")
    release_clean = _clean(release_id, default="unknown")
    reason_clean = _clean(reason)
    ev_hash = evidence_hash(evidence)
    ts = _now_iso()

    def _insert(active_conn: sa.Connection) -> dict[str, Any] | None:
        if not sa.inspect(active_conn).has_table("release_compliance_ledger"):
            if active_conn.dialect.name == "postgresql":
                raise RuntimeError(
                    "release_compliance_ledger table missing; run alembic 0234"
                )
            logger.warning(
                "release_compliance_ledger table missing; skipped action=%s release_id=%s",
                action_clean,
                release_clean,
            )
            return None
        if active_conn.dialect.name == "postgresql":
            active_conn.execute(
                sa.text("SELECT pg_advisory_xact_lock(hashtext(:name))"),
                {"name": "release_compliance_ledger"},
            )
        previous = active_conn.execute(
            sa.text(
                "SELECT row_hash FROM release_compliance_ledger "
                "ORDER BY id DESC LIMIT 1"
            )
        ).first()
        prev_hash = str(previous[0]) if previous else ZERO_HASH
        row_hash = compute_row_hash(
            ts=ts,
            actor=actor_clean,
            action=action_clean,
            release_id=release_clean,
            before_state=before_state,
            after_state=after_state,
            reason=reason_clean,
            evidence_hash_value=ev_hash,
            prev_row_hash=prev_hash,
        )
        result = active_conn.execute(
            sa.text(
                "INSERT INTO release_compliance_ledger "
                "(ts, actor, action, release_id, before_state, after_state, "
                " reason, evidence_hash, prev_row_hash, row_hash) "
                "VALUES (:ts, :actor, :action, :release_id, :before_state, "
                " :after_state, :reason, :evidence_hash, :prev_row_hash, "
                " :row_hash)"
            ),
            {
                "ts": ts,
                "actor": actor_clean,
                "action": action_clean,
                "release_id": release_clean,
                "before_state": before_state,
                "after_state": after_state,
                "reason": reason_clean,
                "evidence_hash": ev_hash,
                "prev_row_hash": prev_hash,
                "row_hash": row_hash,
            },
        )
        row_id = int(result.lastrowid or 0)
        if row_id == 0 and active_conn.dialect.name == "postgresql":
            row = active_conn.execute(
                sa.text(
                    "SELECT id FROM release_compliance_ledger "
                    "WHERE row_hash = :row_hash"
                ),
                {"row_hash": row_hash},
            ).first()
            row_id = int(row[0]) if row else 0
        return {
            "id": row_id,
            "ts": ts,
            "actor": actor_clean,
            "action": action_clean,
            "release_id": release_clean,
            "before_state": before_state,
            "after_state": after_state,
            "reason": reason_clean,
            "evidence_hash": ev_hash,
            "prev_row_hash": prev_hash,
            "row_hash": row_hash,
        }

    try:
        if conn is not None:
            return _insert(conn)
        with _engine().begin() as owned_conn:
            return _insert(owned_conn)
    except sa.exc.IntegrityError as exc:
        if "LedgerTriggerBypassed" in str(exc):
            raise LedgerTriggerBypassed(str(exc)) from exc
        raise
    except sa.exc.DBAPIError as exc:
        if _missing_table(exc):
            logger.warning(
                "release_compliance_ledger table missing; skipped action=%s release_id=%s",
                action_clean,
                release_clean,
            )
            return None
        if "LedgerTriggerBypassed" in str(exc):
            raise LedgerTriggerBypassed(str(exc)) from exc
        raise


def list_rows(*, since: date | str | None = None) -> list[dict[str, Any]]:
    where = ""
    params: dict[str, Any] = {}
    if since is not None:
        since_text = since.isoformat() if isinstance(since, date) else str(since)
        where = "WHERE ts >= :since"
        params["since"] = since_text
    with _engine().connect() as conn:
        rows = conn.execute(
            sa.text(
                "SELECT id, ts, actor, action, release_id, before_state, "
                "       after_state, reason, evidence_hash, prev_row_hash, row_hash "
                f"FROM release_compliance_ledger {where} ORDER BY id ASC"
            ),
            params,
        ).fetchall()
    return [_row_to_dict(row) for row in rows]


def verify_chain(rows: Iterable[dict[str, Any]] | None = None) -> bool:
    materialized = list_rows() if rows is None else list(rows)
    previous = ZERO_HASH
    for row in materialized:
        if row["prev_row_hash"] != previous:
            raise ChainBroken(
                f"row {row['id']} prev_row_hash mismatch: "
                f"{row['prev_row_hash']} != {previous}"
            )
        expected = compute_row_hash(
            ts=str(row["ts"]),
            actor=row["actor"],
            action=row["action"],
            release_id=row["release_id"],
            before_state=row.get("before_state"),
            after_state=row.get("after_state"),
            reason=row["reason"],
            evidence_hash_value=row["evidence_hash"],
            prev_row_hash=row["prev_row_hash"],
        )
        if row["row_hash"] != expected:
            raise ChainBroken(
                f"row {row['id']} row_hash mismatch: {row['row_hash']} != {expected}"
            )
        previous = row["row_hash"]
    return True


def signing_key_from_env() -> LedgerSigningKey:
    key_id = os.environ.get("OMNISIGHT_COMPLIANCE_LEDGER_SIGNING_KEY_ID", "local-dev")
    secret = os.environ.get(
        "OMNISIGHT_COMPLIANCE_LEDGER_SIGNING_KEY",
        "local-dev-compliance-ledger-key",
    ).encode("utf-8")
    return LedgerSigningKey(key_id=key_id, secret=secret)


def export_csv(
    rows: list[dict[str, Any]],
    path: Path,
    *,
    signing_key: LedgerSigningKey | None = None,
) -> dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "id",
        "ts",
        "actor",
        "action",
        "release_id",
        "before_state",
        "after_state",
        "reason",
        "evidence_hash",
        "prev_row_hash",
        "row_hash",
    ]
    try:
        with path.open("w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=fields)
            writer.writeheader()
            for row in rows:
                writer.writerow({field: row.get(field, "") for field in fields})
        key = signing_key or signing_key_from_env()
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        signature = hmac.new(key.secret, digest.encode("ascii"), hashlib.sha256).hexdigest()
        payload = {
            "version": SIGNATURE_VERSION,
            "algorithm": "hmac-sha256",
            "key_id": key.key_id,
            "artifact_sha256": digest,
            "signature": signature,
        }
        sig_path = path.with_suffix(path.suffix + ".sig")
        sig_path.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        return {**payload, "signature_path": str(sig_path)}
    except OSError as exc:
        raise ExportFailed(f"CSV export failed: {exc}") from exc


def verify_csv_signature(path: Path, signature: dict[str, Any], key: LedgerSigningKey) -> bool:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    expected = hmac.new(key.secret, digest.encode("ascii"), hashlib.sha256).hexdigest()
    return (
        signature.get("version") == SIGNATURE_VERSION
        and signature.get("artifact_sha256") == digest
        and hmac.compare_digest(str(signature.get("signature", "")), expected)
    )


def export_pdf(rows: list[dict[str, Any]], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["Release Compliance Ledger", ""]
    for row in rows:
        lines.append(
            f"{row['id']} {row['ts']} {row['release_id']} "
            f"{row['action']} {row.get('before_state') or '-'}->{row.get('after_state') or '-'}"
        )
        lines.append(f"actor={row['actor']} reason={row['reason']}")
        lines.append(f"hash={row['row_hash']}")
        lines.append("")
    try:
        path.write_bytes(_minimal_pdf(lines))
    except OSError as exc:
        raise ExportFailed(f"PDF export failed: {exc}") from exc
    return path


def export_since(
    *,
    since: date | str,
    output_dir: Path,
    signing_key: LedgerSigningKey | None = None,
) -> dict[str, Any]:
    rows = list_rows(since=since)
    verify_chain()
    since_text = since.isoformat() if isinstance(since, date) else str(since)
    stem = f"release-compliance-ledger-{since_text}"
    csv_path = output_dir / f"{stem}.csv"
    pdf_path = output_dir / f"{stem}.pdf"
    signature = export_csv(rows, csv_path, signing_key=signing_key)
    export_pdf(rows, pdf_path)
    return {
        "row_count": len(rows),
        "csv_path": str(csv_path),
        "csv_signature": signature,
        "pdf_path": str(pdf_path),
    }


def _minimal_pdf(lines: list[str]) -> bytes:
    escaped_lines = [_pdf_escape(line[:110]) for line in lines[:240]]
    text_ops = ["BT", "/F1 10 Tf", "50 780 Td"]
    first = True
    for line in escaped_lines:
        if first:
            first = False
        else:
            text_ops.append("0 -14 Td")
        text_ops.append(f"({line}) Tj")
    text_ops.append("ET")
    stream = "\n".join(text_ops).encode("latin-1", errors="replace")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>"
        ),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        (
            b"<< /Length "
            + str(len(stream)).encode("ascii")
            + b" >>\nstream\n"
            + stream
            + b"\nendstream"
        ),
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for idx, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out.extend(f"{idx} 0 obj\n".encode("ascii"))
        out.extend(obj)
        out.extend(b"\nendobj\n")
    xref_at = len(out)
    out.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    out.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        out.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    trailer = (
        f"trailer << /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_at}\n%%EOF\n"
    )
    out.extend(trailer.encode("ascii"))
    return bytes(out)


def _pdf_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _row_to_dict(row: sa.Row) -> dict[str, Any]:
    return {
        "id": int(row[0]),
        "ts": str(row[1]),
        "actor": row[2],
        "action": row[3],
        "release_id": row[4],
        "before_state": row[5],
        "after_state": row[6],
        "reason": row[7],
        "evidence_hash": row[8],
        "prev_row_hash": row[9],
        "row_hash": row[10],
    }


__all__ = [
    "ChainBroken",
    "ExportFailed",
    "LedgerSigningKey",
    "LedgerTriggerBypassed",
    "ZERO_HASH",
    "evidence_hash",
    "export_csv",
    "export_pdf",
    "export_since",
    "list_rows",
    "record",
    "set_engine_for_tests",
    "signing_key_from_env",
    "verify_chain",
    "verify_csv_signature",
]
