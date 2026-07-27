#!/usr/bin/env bash
# Daily Postgres → S3 backup (dockerized wrapper for hosts without pg_dump/awscli).
#
# Origin: 2026-05-18 evening Task C. The shipped scripts/backup_postgres_daily.sh
# requires pg_dump + aws CLI on the host. On this WSL prod host neither is
# installed (operator has no sudo). This wrapper achieves the same end state
# by using docker:
#   - pg_dump  ← `docker exec omnisight-pg-primary pg_dump ...`
#   - aws CLI  ← `docker run --rm amazon/aws-cli ...`
#
# Required env (from EnvironmentFile or shell):
#   OMNISIGHT_BACKUP_S3_URI            s3://bucket/prefix
#   AWS_ACCESS_KEY_ID                  AWS IAM access key
#   AWS_SECRET_ACCESS_KEY              AWS IAM secret
#   AWS_DEFAULT_REGION                 e.g. us-east-1
# Optional env:
#   OMNISIGHT_BACKUP_DIR               local staging (default: $HOME/pg-backups/daily-s3)
#   OMNISIGHT_BACKUP_LABEL             filename label (default: daily)
#   OMNISIGHT_BACKUP_RETENTION_DAYS    local file retention (default: 14)
#   PG_CONTAINER                       container with pg_dump (default: omnisight-pg-primary)
#   PG_USER                            postgres user (default: omnisight)
#   PG_DB                              postgres database (default: omnisight)
#   DRY_RUN                            "1" to log without uploading

set -Eeuo pipefail

LABEL="${OMNISIGHT_BACKUP_LABEL:-daily}"
BACKUP_DIR="${OMNISIGHT_BACKUP_DIR:-$HOME/pg-backups/daily-s3}"
RETENTION_DAYS="${OMNISIGHT_BACKUP_RETENTION_DAYS:-14}"
PG_CONTAINER="${PG_CONTAINER:-omnisight-pg-primary}"
PG_USER="${PG_USER:-omnisight}"
PG_DB="${PG_DB:-omnisight}"
PROD_REPO="${OMNISIGHT_PROD_REPO:-/home/user/omnisight-prod}"
DLP_MIN_TABLES="${DLP_MIN_TABLES:-100}"

log() { printf '[%s] %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*"; }
die() { log "ERROR: $*" >&2; exit 1; }

[[ -n "${OMNISIGHT_BACKUP_S3_URI:-}" ]] || die "OMNISIGHT_BACKUP_S3_URI is required"
[[ -n "${AWS_ACCESS_KEY_ID:-}" ]] || die "AWS_ACCESS_KEY_ID is required"
[[ -n "${AWS_SECRET_ACCESS_KEY:-}" ]] || die "AWS_SECRET_ACCESS_KEY is required"
[[ -n "${AWS_DEFAULT_REGION:-}" ]] || die "AWS_DEFAULT_REGION is required"

# Parse s3://bucket/prefix
if [[ ! "$OMNISIGHT_BACKUP_S3_URI" =~ ^s3://([^/]+)(/(.+))?$ ]]; then
  die "OMNISIGHT_BACKUP_S3_URI must be s3://bucket[/prefix]"
fi
S3_BUCKET="${BASH_REMATCH[1]}"
S3_PREFIX="${BASH_REMATCH[3]:-}"

mkdir -p "$BACKUP_DIR"
TS="$(date -u '+%Y%m%dT%H%M%SZ')"
PLAIN_FILE="$BACKUP_DIR/${LABEL}-${TS}.dump.gz"
DUMP_FILE="${PLAIN_FILE}.gpg"
SHA_FILE="${DUMP_FILE}.sha256"
UPLOADED_MARK="$BACKUP_DIR/.uploaded-${TS}"
# Registered BEFORE the plaintext can exist, so an abort cannot orphan an
# unencrypted production dump on disk.
trap 'shred -u "$PLAIN_FILE" 2>/dev/null || rm -f "$PLAIN_FILE" 2>/dev/null || true' EXIT INT TERM

# --- 1. pg_dump via docker exec into pg-primary container ---
log "pg_dump → $DUMP_FILE  (container=$PG_CONTAINER, db=$PG_DB)"
if [[ "${DRY_RUN:-0}" == "1" ]]; then
  # A zero-work exit 0 does not trip OnFailure=, so under a timer DRY_RUN is
  # indistinguishable from a working backup. Allowed by hand, never by systemd.
  [[ -n "${INVOCATION_ID:-}" ]] && die "DRY_RUN is refused under systemd: it exits 0 \
without producing a backup, which reads as success to every monitor we have"
  log "DRY RUN: skipping pg_dump + upload"
  exit 0
fi

docker exec "$PG_CONTAINER" pg_dump \
  -U "$PG_USER" -d "$PG_DB" \
  --format=custom --no-owner --no-privileges \
  | gzip -9 > "$PLAIN_FILE"
chmod 600 "$PLAIN_FILE"

# ── OP-2731 P1: the DLP gate on UPLOAD ELIGIBILITY ───────────────────────────
# The claim this makes is deliberately narrow: "only an artefact carrying a
# successful policy attestation is upload-eligible." NOT "nothing unscanned
# leaves the host" -- the scanner covers public BASE TABLEs and a text-type
# allowlist, and has documented false negatives (OP-2730). Overclaiming here is
# how a gate gets trusted past its evidence.
#
# The gate governs EGRESS, never EXISTENCE. A content verdict must never be able
# to stop a backup being made: the encrypted artefact below is produced either
# way. A block costs off-site freshness, not the backup.
DLP_VERDICT="not-run"; DLP_TABLES=0
SCANNER="$PROD_REPO/scripts/backup_dlp_scan.py"
REVIEWED="${OMNISIGHT_DLP_REVIEWED_BODIES_HOST:-${HOME:-}/.config/omnisight/backup-dlp-reviewed-bodies.txt}"
[[ -r "$SCANNER" ]] || die "DLP scanner missing at $SCANNER"
# Mirrors lane A's FX.7.10 preflight: an unreadable allowlist must be a loud
# deploy bug, not silently demoted into a normal-looking scan block.
[[ -r "$REVIEWED" ]] || die "reviewed-body allowlist unreadable at $REVIEWED"

if docker compose -f "$PROD_REPO/docker-compose.prod.yml" ps --services --filter status=running 2>/dev/null | grep -qx backend-a; then
  TMP_DB="omnisight_c_dlp_${TS}_$$"
  _drop_tmp() { docker exec "$PG_CONTAINER" dropdb -U "$PG_USER" --if-exists "$TMP_DB" >/dev/null 2>&1 || true; }
  if docker exec "$PG_CONTAINER" createdb -U "$PG_USER" "$TMP_DB" >/dev/null 2>&1 &&
     gzip -dc "$PLAIN_FILE" | docker exec -i "$PG_CONTAINER" pg_restore -U "$PG_USER" -d "$TMP_DB" \
       --no-owner --no-privileges --exit-on-error >/dev/null 2>&1; then
    # --exit-on-error above is not decoration: without it a partially restored
    # dump scans clean simply because fewer tables exist, and a subset passing
    # reads identically to the whole thing passing.
    DLP_TABLES="$(docker exec "$PG_CONTAINER" psql -U "$PG_USER" -d "$TMP_DB" -tAc \
      "select count(*) from information_schema.tables where table_schema='public' and table_type='BASE TABLE'" 2>/dev/null || echo 0)"
    if [[ "${DLP_TABLES:-0}" -lt "${DLP_MIN_TABLES:-100}" ]]; then
      DLP_VERDICT="unusable"
      log "[WARN] restored only $DLP_TABLES tables (< ${DLP_MIN_TABLES:-100}) — refusing to treat a partial restore as a clean scan"
    else
      # OMNISIGHT_DATABASE_URL is UNSET for this call on purpose. This lane's
      # EnvironmentFile defines it as ...@127.0.0.1, and compose ${} resolves
      # from the process environment BEFORE the project .env -- so inheriting it
      # would tell the container to reach the database at itself, failing every
      # night under the message "DLP scan failed". Lane A only works because its
      # env file carries nothing but the passphrase.
      if env -u OMNISIGHT_DATABASE_URL docker compose -f "$PROD_REPO/docker-compose.prod.yml" \
            run --rm --no-deps \
            --volume "$SCANNER:/app/scripts/backup_dlp_scan.py:ro" \
            --volume "$REVIEWED:/tmp/omnisight-dlp-reviewed-bodies.txt:ro" \
            --env "OMNISIGHT_DLP_REVIEWED_BODIES=/tmp/omnisight-dlp-reviewed-bodies.txt" \
            --entrypoint python3 backend-a \
            /app/scripts/backup_dlp_scan.py --postgres-tmp-db "$TMP_DB" >/dev/null 2>&1; then
        DLP_VERDICT="pass"
      else
        DLP_VERDICT="blocked"
      fi
    fi
  else
    DLP_VERDICT="unusable"
  fi
  _drop_tmp
else
  # Scanner unavailability is NOT a content verdict and must not be reported as
  # one. Lane A dies here; copying that would stop off-site upload during
  # exactly the incident class where an off-site copy matters most.
  DLP_VERDICT="unusable"
  log "[WARN] backend-a not running — cannot scan; upload will be withheld, artefact kept locally"
fi
log "DLP verdict: $DLP_VERDICT (tables=$DLP_TABLES)"

# OP-2731 F1. This lane is the ONLY one whose artefact leaves the host, and it
# shipped with --sse AES256 alone: server-side encryption, transparent to any
# principal holding s3:GetObject. A DLP content gate gets the rows a regex can
# match; encryption gets all 45 MB. So the artefact is encrypted here, with the
# same non-interactive pattern lane A uses.
[[ -n "${OMNISIGHT_BACKUP_PASSPHRASE:-}" ]] || die "OMNISIGHT_BACKUP_PASSPHRASE is \
required: this lane no longer uploads plaintext. Add backup-dr.env as an EnvironmentFile."
printf '%s' "$OMNISIGHT_BACKUP_PASSPHRASE" | gpg --batch --yes --quiet \
  --pinentry-mode loopback --passphrase-fd 0 \
  --symmetric --cipher-algo AES256 --output "$DUMP_FILE" "$PLAIN_FILE" \
  || die "gpg encryption failed"
chmod 600 "$DUMP_FILE"
shred -u "$PLAIN_FILE" 2>/dev/null || rm -f "$PLAIN_FILE"

# The digest MUST cover the bytes that are actually uploaded. gpg symmetric
# output is non-deterministic (fresh session key + IV), so a digest taken before
# encryption can never be re-derived from the object and silently becomes
# unverifiable. Portable basename too: the previous sidecar embedded an absolute
# host path, so `sha256sum --check` broke on any rename or relocation.
( cd "$(dirname "$DUMP_FILE")" && sha256sum "$(basename "$DUMP_FILE")" ) > "$SHA_FILE"
chmod 600 "$SHA_FILE"

SIZE_MB=$(du -m "$DUMP_FILE" | cut -f1)
log "dump size: ${SIZE_MB} MB"

# --- 2. Upload via amazon/aws-cli image ---
S3_KEY="${S3_PREFIX:+$S3_PREFIX/}${LABEL}/$(basename "$DUMP_FILE")"
log "upload → s3://$S3_BUCKET/$S3_KEY"

# OP-2731 F2, three changes from the previous form:
#  - mount ONLY the approved file, not the whole backup directory. A later
#    passing run used to re-expose every earlier artefact to a container holding
#    AWS credentials.
#  - pin the CLI image by digest. `amazon/aws-cli` is a mutable tag, pulled
#    nightly, and it is handed our credentials.
#  - pass the secret through a 0600 file, not `-e NAME="$VALUE"`. The old form
#    put it in /proc/<pid>/cmdline and `ps`. Note --env-file is NOT sufficient
#    either: docker stores the resolved value in the container's Config.Env,
#    readable via `docker inspect` for its lifetime. So the file is mounted and
#    only its PATH is passed.
AWSCLI_IMAGE="${OMNISIGHT_AWSCLI_IMAGE:-amazon/aws-cli@sha256:276a192679356eba79a14009edfaff1254dd9cf41536156b8c0284ae63d80f8c}"
AWS_CRED_FILE="$(mktemp)"; chmod 600 "$AWS_CRED_FILE"
trap 'shred -u "$PLAIN_FILE" 2>/dev/null || rm -f "$PLAIN_FILE" 2>/dev/null || true; \
      shred -u "$AWS_CRED_FILE" 2>/dev/null || rm -f "$AWS_CRED_FILE" 2>/dev/null || true' EXIT INT TERM
cat > "$AWS_CRED_FILE" <<CREDEOF
[default]
aws_access_key_id = $AWS_ACCESS_KEY_ID
aws_secret_access_key = $AWS_SECRET_ACCESS_KEY
CREDEOF

s3cp() {  # $1 = local file, $2 = s3 key
  docker run --rm \
    -v "$1:/data/$(basename "$1"):ro" \
    -v "$AWS_CRED_FILE:/root/.aws/credentials:ro" \
    -e AWS_DEFAULT_REGION="$AWS_DEFAULT_REGION" \
    "$AWSCLI_IMAGE" s3 cp "/data/$(basename "$1")" "s3://$S3_BUCKET/$2" --sse AES256
}

# THE GATE. Everything above ran regardless of verdict; only egress is
# conditional. A withheld upload exits non-zero so OnFailure= fires, and the
# encrypted artefact stays on disk -- cohort-atomic pruning will not remove it,
# because its .uploaded marker is never written.
if [[ "$DLP_VERDICT" != "pass" ]]; then
  log "[FAIL] upload WITHHELD: DLP verdict '$DLP_VERDICT'."
  log "       The encrypted artefact is retained locally at $DUMP_FILE."
  log "       'blocked' = a content finding; 'unusable' = the scanner could not"
  log "       run, which is NOT a content verdict and must not be read as one."
  exit 1
fi

# The attestation is what "upload-eligible" actually means. An exit code is not
# a durable record, so it binds the things a later reader would need to check:
# the digest of the exact bytes uploaded, the scanner and allowlist that judged
# them, and how much of the database was actually restored to be judged.
ATTEST_FILE="${DUMP_FILE}.attestation.json"
printf '{"artefact":"%s","sha256":"%s","verdict":"%s","restored_tables":%s,"scanner_sha256":"%s","allowlist_sha256":"%s","scanned_at":"%s"}\n' \
  "$(basename "$DUMP_FILE")" \
  "$(cut -d' ' -f1 < "$SHA_FILE")" \
  "$DLP_VERDICT" "${DLP_TABLES:-0}" \
  "$(sha256sum "$SCANNER" | cut -d' ' -f1)" \
  "$(sha256sum "$REVIEWED" | cut -d' ' -f1)" \
  "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" > "$ATTEST_FILE"
chmod 600 "$ATTEST_FILE"

s3cp "$DUMP_FILE" "$S3_KEY" || die "S3 upload failed"

# The sidecar is not optional. It used to be WARN-only, which left objects
# off-site with no way to verify them AND did not trip OnFailure=. A payload
# without its digest is not a restorable cohort.
s3cp "$ATTEST_FILE" "${S3_KEY}.attestation.json" || die "attestation upload failed — \
the payload would be off-site with no record of what judged it"
s3cp "$SHA_FILE" "${S3_KEY}.sha256" || die "SHA sidecar upload failed — the payload \
is off-site without a verifiable digest; treating the whole cohort as failed"

# Cohort marker: written only once EVERY member is off-site. Pruning keys off
# this, never off age alone.
: > "$UPLOADED_MARK"

log "upload OK: s3://$S3_BUCKET/$S3_KEY"

# --- 3. Local retention pruning — COHORT-ATOMIC (OP-2731 F3) ---
# Age alone must never trigger deletion. Two ways that bites:
#  - a cohort whose payload uploaded but whose digest did not is not restorable
#    off-host; deleting the local copy leaves nothing anywhere.
#  - a DLP block or upload outage lasting longer than RETENTION_DAYS would
#    silently destroy the local copies of every day that never reached S3.
# So retention age is a FLOOR on deletion, never a trigger: a cohort is pruned
# only if its .uploaded marker exists AND it is old enough. Anything else is
# retained regardless of age, and grows disk until the existing disk-floor
# check (warn 85% / crit 92%) says so — a loud disk warning rather than the
# silent loss of the last copy.
log "pruning complete, uploaded cohorts older than $RETENTION_DAYS days"
retained=0
while IFS= read -r mark; do
  [[ -n "$mark" ]] || continue
  cts="${mark##*/.uploaded-}"
  if [[ $(find "$mark" -mtime "+$RETENTION_DAYS" -print 2>/dev/null) ]]; then
    rm -f "$BACKUP_DIR/${LABEL}-${cts}.dump.gz.gpg" \
          "$BACKUP_DIR/${LABEL}-${cts}.dump.gz.gpg.sha256" \
          "$BACKUP_DIR/${LABEL}-${cts}.dump.gz.gpg.attestation.json" "$mark" \
      && log "  pruned cohort $cts"
  fi
done < <(find "$BACKUP_DIR" -maxdepth 1 -name '.uploaded-*' 2>/dev/null)
# Legacy reconciliation. Artefacts written BEFORE F1 are plaintext .dump.gz with
# the old naming and no .uploaded marker, so the cohort rule above would retain
# them forever -- 20 plaintext production dumps sitting on disk indefinitely,
# which is strictly worse than the behaviour being replaced. They were written
# under the old age-only contract and were uploaded under it, so they are pruned
# under it. Matching *.dump.gz WITHOUT .gpg is deliberate: the encrypted name
# must never be caught by this branch.
while IFS= read -r old; do
  [[ -n "$old" ]] || continue
  rm -f "$old" "${old}.sha256" && log "  pruned legacy plaintext $(basename "$old")"
done < <(find "$BACKUP_DIR" -maxdepth 1 -type f -name '*.dump.gz' \
           ! -name '*.gpg' -mtime "+$RETENTION_DAYS" 2>/dev/null)
# Deliberately label-AGNOSTIC ('*.dump.gz', not "${LABEL}-*"). The old rule only
# ever pruned the label it was currently running as, so the two
# evening-first-20260518 dumps from the lane's activation were matched by
# nothing and sat as plaintext production dumps for 70 days. Any unencrypted
# dump in this directory past retention should go, whichever label wrote it --
# that is the class, not the instance. Every removal is logged by name.

# Startup reconciliation: report cohorts with no marker. Traps cannot cover
# SIGKILL or power loss, so an interrupted run can leave one behind.
while IFS= read -r f; do
  [[ -n "$f" ]] || continue
  b="$(basename "$f")"; cts="${b#${LABEL}-}"; cts="${cts%%.dump.gz.gpg}"
  [[ -e "$BACKUP_DIR/.uploaded-${cts}" ]] || { retained=$((retained+1)); }
done < <(find "$BACKUP_DIR" -maxdepth 1 -name "${LABEL}-*.dump.gz.gpg" 2>/dev/null)
[[ "$retained" -gt 0 ]] && log "  $retained cohort(s) retained: never confirmed off-site"

log "done"
