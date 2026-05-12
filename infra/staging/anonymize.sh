#!/usr/bin/env bash
#
# [OP-972] AUDIT-19b — Phase 1 (6c) PII anonymizer for the staging Postgres
# snapshot pipeline.
#
#   Usage:   infra/staging/anonymize.sh <input.sql> <output.sql>
#
# Contract (kept deliberately small so the 6c -> 6g swap is a one-file
# replacement — see the "Phase 2" note at the bottom and AUDIT-19b's
# phased-strategy table):
#
#   * <input.sql>   a *plain-format* (`pg_dump -Fp`) dump of prod.
#   * <output.sql>  the same dump with an anonymization epilogue appended:
#                   a `BEGIN; UPDATE ...; COMMIT;` block that masks every
#                   PII column declared in infra/staging/anonymize-fields.yaml.
#                   Loading <output.sql> into a fresh DB therefore yields a
#                   structurally-identical-but-PII-free copy.
#
# Why an *epilogue* and not in-line rewriting: pg_dump plain output streams
# table data via `COPY ... FROM stdin`; rewriting those COPY blocks safely
# would mean re-parsing every row. Appending UPDATEs that run after the data
# (and after indexes/constraints are created) is simpler and—because the
# masking values are uniqueness-preserving where needed (md5-based emails,
# id-suffixed names)—does not violate UNIQUE constraints. Greenmask (6g)
# rewrites the data stream natively and removes this caveat.
#
# AnonymizeMissedField guard: before generating SQL we scan the dump's
# CREATE TABLE statements; if any column whose *name* matches one of
# anonymize-fields.yaml's `pii_column_patterns` is NOT covered by the spec,
# we FAIL LOUD (exit 3) and emit nothing. Leaking a newly-added real PII
# column to staging is unacceptable — the operator must extend the spec
# (and, if needed, a migration must add the column to the pattern list)
# before the next restore can proceed.
#
# Env overrides:
#   ANONYMIZE_FIELDS_YAML   path to the spec (default: sibling
#                           anonymize-fields.yaml next to this script)
#
# Exit codes:
#   0  ok — <output.sql> written
#   2  usage / missing input or spec file
#   3  AnonymizeMissedField, or a malformed spec (e.g. test_user strategy
#      without an id_column) — nothing written

set -euo pipefail

SELF="anonymize.sh"
log()  { printf '[%s] %s\n' "$SELF" "$*" >&2; }
die()  { printf '[%s] %s\n' "$SELF" "$*" >&2; exit "${2:-2}"; }

IN="${1:-}"
OUT="${2:-}"
[[ -n "$IN" && -n "$OUT" ]] || die "usage: $SELF <input.sql> <output.sql>" 2

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FIELDS="${ANONYMIZE_FIELDS_YAML:-$SCRIPT_DIR/anonymize-fields.yaml}"

[[ -f "$IN" ]]     || die "input dump not found: $IN" 2
[[ -f "$FIELDS" ]] || die "anonymize spec not found: $FIELDS" 2

log "spec=$FIELDS  input=$IN ($(wc -c <"$IN" 2>/dev/null || echo '?') bytes)  output=$OUT"

# ── Generate the anonymization epilogue (+ run the missed-field guard) ─
# The Python helper does the heavy lifting (YAML parsing, CREATE TABLE
# column extraction, pattern matching, SQL emission). It writes the SQL
# block to stdout; on a missed PII column or a malformed spec it writes a
# diagnostic to stderr and exits 3. stdlib + PyYAML only (PyYAML is a hard
# dep of this repo — see pyproject.toml).
epilogue="$(
	python3 - "$FIELDS" "$IN" <<'PY'
import re
import sys

import yaml

spec_path, dump_path = sys.argv[1], sys.argv[2]

with open(spec_path, "r", encoding="utf-8") as fh:
    spec = yaml.safe_load(fh) or {}

tables_spec = spec.get("tables") or []
patterns = [re.compile(p, re.IGNORECASE) for p in (spec.get("pii_column_patterns") or [])]

VALID_STRATEGIES = {"email_hash", "test_user", "null", "redact", "zero"}


def _ident(name: str) -> str:
    # bare identifier -> quoted (handles reserved words / mixed case safely);
    # already-quoted stays as-is.
    name = name.strip()
    if name.startswith('"') and name.endswith('"'):
        return name
    return '"' + name.replace('"', '""') + '"'


# --- 1. extract {(schema, table): [col, ...]} from the dump's DDL --------
CREATE_RE = re.compile(
    r'CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?'
    r'(?:(?P<schema>"?[A-Za-z0-9_]+"?)\.)?(?P<table>"?[A-Za-z0-9_]+"?)\s*\('
    # body up to the matching close-paren at the start of a line; tolerate a
    # trailing `PARTITION BY ...` / `WITH (...)` / `INHERITS (...)` before the `;`
    r'(?P<body>.*?)\n\)[^;]*?;',
    re.IGNORECASE | re.DOTALL,
)
COLDEF_RE = re.compile(r'^\s*(?P<col>"[^"]+"|[A-Za-z_][A-Za-z0-9_$]*)\s+\S')

dump_text = open(dump_path, "r", encoding="utf-8", errors="replace").read()

dump_tables: dict[tuple[str, str], list[str]] = {}
for m in CREATE_RE.finditer(dump_text):
    schema = (m.group("schema") or "public").strip('"')
    table = m.group("table").strip('"')
    cols: list[str] = []
    for line in m.group("body").splitlines():
        # stop column scan at table-level constraints
        if re.match(r'\s*(CONSTRAINT|PRIMARY\s+KEY|UNIQUE|FOREIGN\s+KEY|CHECK|EXCLUDE)\b',
                    line, re.IGNORECASE):
            continue
        cm = COLDEF_RE.match(line)
        if cm:
            cols.append(cm.group("col").strip('"'))
    if cols:
        dump_tables[(schema, table)] = cols

# --- 2. AnonymizeMissedField guard --------------------------------------
declared: set[tuple[str, str, str]] = set()
truncated: set[tuple[str, str]] = set()
for t in tables_spec:
    sch = (t.get("schema") or "public")
    tbl = t["name"]
    if t.get("truncate"):
        truncated.add((sch, tbl))
        continue  # a truncated table covers all of its columns
    cols = t.get("columns") or []
    if not cols:
        sys.stderr.write(
            f"AnonymizeMissedField: table {sch}.{tbl} in the spec has neither "
            f"`truncate: true` nor any `columns:` — malformed entry.\n"
        )
        raise SystemExit(3)
    for c in cols:
        declared.add((sch, tbl, c["name"]))

missed: list[str] = []
for (sch, tbl), cols in sorted(dump_tables.items()):
    if (sch, tbl) in truncated:
        continue
    for col in cols:
        if any(p.fullmatch(col) or p.match(col) for p in patterns):
            if (sch, tbl, col) not in declared:
                hit = next(p.pattern for p in patterns if p.fullmatch(col) or p.match(col))
                missed.append(f"  {sch}.{tbl}.{col}  (matches /{hit}/)")

if missed:
    sys.stderr.write(
        "AnonymizeMissedField: the prod dump contains PII-shaped columns not "
        "covered by anonymize-fields.yaml — refusing to anonymize.\n"
        + "\n".join(missed)
        + "\nFix: add each column under the owning table's `columns:` (or set "
          "`truncate: true` on the table), and if its name is a new convention "
          "a `pii_column_patterns:` entry too, then re-run.\n"
    )
    raise SystemExit(3)

# --- 3. emit the anonymization epilogue ---------------------------------
out: list[str] = []
out.append("")
out.append("-- ============================================================")
out.append("-- [OP-972] AUDIT-19b — Phase 1 (6c) anonymization epilogue")
out.append("-- generated by infra/staging/anonymize.sh from anonymize-fields.yaml")
out.append("-- ============================================================")
out.append("BEGIN;")

n_stmts = 0
# 3a. truncated tables first (DELETE FROM all rows).
for t in tables_spec:
    if not t.get("truncate"):
        continue
    sch = (t.get("schema") or "public")
    tbl = t["name"]
    qtable = f"{_ident(sch)}.{_ident(tbl)}"
    if (sch, tbl) not in dump_tables:
        out.append(f"-- skip {sch}.{tbl}: not present in this dump (truncate)")
        continue
    out.append(f"DELETE FROM {qtable};")
    n_stmts += 1

# 3b. column-level masking UPDATEs.
for t in tables_spec:
    if t.get("truncate"):
        continue
    sch = (t.get("schema") or "public")
    tbl = t["name"]
    key = (sch, tbl)
    qtable = f"{_ident(sch)}.{_ident(tbl)}"
    if key not in dump_tables:
        out.append(f"-- skip {sch}.{tbl}: not present in this dump")
        continue
    present_cols = set(dump_tables[key])
    id_col = t.get("id_column")
    for c in (t.get("columns") or []):
        col, strat = c["name"], c["strategy"]
        if strat not in VALID_STRATEGIES:
            sys.stderr.write(f"AnonymizeMissedField: unknown strategy '{strat}' for {sch}.{tbl}.{col}\n")
            raise SystemExit(3)
        if col not in present_cols:
            out.append(f"-- skip {sch}.{tbl}.{col}: column not present in this dump")
            continue
        qcol = _ident(col)
        if strat == "email_hash":
            out.append(
                f"UPDATE {qtable} SET {qcol} = "
                f"'redacted-' || left(md5({qcol}::text), 12) || '@staging.test' "
                f"WHERE {qcol} IS NOT NULL;"
            )
        elif strat == "test_user":
            if not id_col:
                sys.stderr.write(
                    f"AnonymizeMissedField: {sch}.{tbl}.{col} uses strategy 'test_user' "
                    f"but the table has no `id_column` in the spec.\n"
                )
                raise SystemExit(3)
            if id_col not in present_cols:
                out.append(f"-- skip {sch}.{tbl}.{col}: id_column {id_col} not present in this dump")
                continue
            out.append(f"UPDATE {qtable} SET {qcol} = 'Test User ' || {_ident(id_col)}::text;")
        elif strat == "null":
            out.append(f"UPDATE {qtable} SET {qcol} = NULL;")
        elif strat == "redact":
            out.append(f"UPDATE {qtable} SET {qcol} = '[redacted]';")
        elif strat == "zero":
            out.append(f"UPDATE {qtable} SET {qcol} = 0;")
        n_stmts += 1

out.append("COMMIT;")
out.append(f"-- anonymization epilogue: {n_stmts} statement(s) (DELETE FROM + UPDATE)")
out.append("")
sys.stdout.write("\n".join(out))
PY
)" || exit $?

# ── Write <output.sql> = original dump + epilogue ─────────────────────
tmp_out="${OUT}.tmp.$$"
trap 'rm -f "$tmp_out"' EXIT
cat "$IN" >"$tmp_out"
printf '%s\n' "$epilogue" >>"$tmp_out"
mv "$tmp_out" "$OUT"
trap - EXIT

n_stmts="$(printf '%s\n' "$epilogue" | grep -cE '^(UPDATE |DELETE FROM )' || true)"
log "ok: wrote $OUT ($(wc -c <"$OUT") bytes, ${n_stmts} anonymizing statement(s) appended)"
exit 0

# ── Phase 2 (6g) migration note ──────────────────────────────────────
# Swapping to Greenmask (https://greenmask.io/) is a one-file replacement:
# this script becomes a thin wrapper that (1) renders a Greenmask config
# from infra/staging/anonymize-fields.yaml's `greenmask:` block + `tables:`
# (the `strategy_to_transformer` map already specifies every translation),
# (2) runs `greenmask dump --config <generated>` to produce an anonymized
# archive, and (3) `pg_restore`s it. snapshot-restore.sh's interface to
# this script (`anonymize.sh <in> <out>`) does not change — only the
# meaning of <out> shifts from "plain SQL + epilogue" to "anonymized
# archive", which snapshot-restore.sh already tolerates (it sniffs the
# format). Because Greenmask is schema-aware it preserves referential
# integrity during the dump itself, so the post-load-UPDATE caveat above
# disappears. No change to anonymize-fields.yaml is required for the swap.
