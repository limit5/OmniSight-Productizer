#!/usr/bin/env bash
# scripts/advance_prod_checkout.sh — advance the release-SHA-pinned prod
# compose checkout to a new release SHA (OP-1741, completes OP-1717).
#
# Context (OP-1717 checkout-topology, cutover 2026-05-26): the prod compose
# stack boots from a DEDICATED, release-SHA-pinned checkout — by default
# /home/user/omnisight-prod (omnisight-compose-prod.service WorkingDirectory,
# pinned @ v0.6.2/ecde4787 at cutover). Staging boots from /home/user/sora-
# bridge (develop-tip). The remaining gap OP-1717 left open: a prod deploy
# must ADVANCE the prod pin to the new release SHA so the compose context
# (docker-compose.prod.yml + scripts/) matches the deployed release — without
# an operator hand-running `git checkout`, and WITHOUT ever deploying from a
# throwaway /tmp worktree.
#
# This helper:
#   1. Asserts it is running from the canonical pinned checkout — errs on a
#      throwaway `git worktree add` linked worktree (often parked under /tmp);
#      warns when the path is simply not the canonical one (set
#      OMNISIGHT_PROD_CHECKOUT if this host pins elsewhere).
#   2. Fails CLOSED if the working tree is dirty (OP-1741 MUST NOT: never
#      deploy from a dirty checkout — unreviewed compose/script edits would
#      ship, and a checkout-advance cannot safely move past local changes).
#   3. `git fetch` + `git checkout --detach <release-sha>` (the pin), then
#      verifies HEAD actually resolved to the requested SHA.
#
# It does NOT touch the running stack, and it does NOT change the digest-deploy
# contract: deploy-prod.sh still deploys by --backend-digest/--frontend-digest;
# the SHA only advances the compose-file pin (OP-1741 MUST NOT).
#
# Usage:
#   scripts/advance_prod_checkout.sh --release-sha=<git-sha> [--dry-run]
#   scripts/advance_prod_checkout.sh --assert-only   # just the canonical+clean assertion
#
# The canonical checkout path defaults to /home/user/omnisight-prod and is
# overridable via OMNISIGHT_PROD_CHECKOUT (for tests / non-standard hosts).

set -euo pipefail

CANONICAL_CHECKOUT="${OMNISIGHT_PROD_CHECKOUT:-/home/user/omnisight-prod}"
RELEASE_SHA=""
DRY_RUN=false
ASSERT_ONLY=false

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'
log()  { echo -e "${GREEN}✅${NC} $*"; }
warn() { echo -e "${YELLOW}⚠️${NC}  $*"; }
err()  { echo -e "${RED}❌${NC} $*"; exit 1; }
step() { echo -e "\n${CYAN}${BOLD}━━━ $* ━━━${NC}\n"; }

# ── CLI Args ──
for arg in "$@"; do
    case "$arg" in
        --release-sha=*) RELEASE_SHA="${arg#*=}" ;;
        --dry-run) DRY_RUN=true ;;
        --assert-only) ASSERT_ONLY=true ;;
        --help|-h)
            echo "Usage: $0 --release-sha=<git-sha> [--dry-run]"
            echo "       $0 --assert-only"
            echo "  Advances the release-SHA-pinned prod compose checkout (default"
            echo "  $CANONICAL_CHECKOUT, override OMNISIGHT_PROD_CHECKOUT) to <git-sha>"
            echo "  via git fetch + checkout. Fails closed on a dirty tree or a"
            echo "  throwaway linked worktree. --assert-only runs ONLY the assertion."
            exit 0 ;;
        *) err "Unknown argument: $arg" ;;
    esac
done

if [ "$ASSERT_ONLY" = true ] && [ -n "$RELEASE_SHA" ]; then
    err "--assert-only and --release-sha are mutually exclusive"
fi
if [ "$ASSERT_ONLY" = false ] && [ -z "$RELEASE_SHA" ]; then
    err "a release SHA is required: --release-sha=<git-sha> (or --assert-only to run just the assertion)"
fi

# ── Canonical-checkout assertion (OP-1741) ──
# The prod deploy MUST run from the canonical pinned prod checkout — never a
# throwaway /tmp worktree, never a dirty tree.
assert_canonical_checkout() {
    if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
        # Not a git work tree (e.g. a hermetic sandbox). There is no pin to
        # advance / assert here; a real checkout-advance would fail loudly at
        # the git step below anyway.
        warn "not inside a git work tree; skipping canonical-checkout assertion"
        return 0
    fi
    local toplevel gitdir
    toplevel="$(git rev-parse --show-toplevel)"
    gitdir="$(git rev-parse --git-dir)"

    # A linked worktree (`git worktree add`) — the throwaway worktree the
    # ticket warns about, often parked under /tmp — has its git-dir under
    # <main>/.git/worktrees/<name>. Deploying from one means the compose
    # context is NOT the canonical pinned checkout. Fail closed.
    case "$gitdir" in
        */worktrees/*)
            err "running from a throwaway linked worktree (git-dir=$gitdir) — deploy from the canonical pinned prod checkout $CANONICAL_CHECKOUT (OP-1717/OP-1741), not a worktree" ;;
    esac

    if [ "$toplevel" != "$CANONICAL_CHECKOUT" ]; then
        warn "running from $toplevel, not the canonical pinned prod checkout $CANONICAL_CHECKOUT — proceeding (set OMNISIGHT_PROD_CHECKOUT if this host pins elsewhere)"
    fi

    # Fail CLOSED on a dirty tree: unreviewed compose/script edits MUST NOT
    # ship, and a checkout-advance cannot safely move past local changes.
    if [ -n "$(git status --porcelain)" ]; then
        err "working tree at $toplevel is dirty — refusing to deploy / advance from a dirty checkout (fail-closed, OP-1741). Commit, stash, or clean it, then re-run."
    fi
    log "Canonical-checkout assertion passed: $toplevel (clean working tree)"
}

assert_canonical_checkout

if [ "$ASSERT_ONLY" = true ]; then
    exit 0
fi

# ── Advance the pin: git fetch + checkout <release-sha> ──
step "Advance pinned prod checkout → $RELEASE_SHA"
if [ "$DRY_RUN" = true ]; then
    echo "  [dry-run] git fetch --tags --prune"
    echo "  [dry-run] git checkout --detach $RELEASE_SHA"
    echo "  [dry-run] verify HEAD == $RELEASE_SHA"
    exit 0
fi

git fetch --tags --prune || \
    err "git fetch failed — cannot advance the pinned prod checkout to $RELEASE_SHA"

git checkout --detach "$RELEASE_SHA" || \
    err "git checkout $RELEASE_SHA failed — the pinned prod checkout was NOT advanced (working tree unchanged)"

# Verify the pin actually moved to the requested SHA.
resolved="$(git rev-parse HEAD)"
requested="$(git rev-parse --verify "${RELEASE_SHA}^{commit}" 2>/dev/null || true)"
if [ -z "$requested" ] || [ "$resolved" != "$requested" ]; then
    err "post-checkout HEAD ($resolved) does not match requested release SHA ($RELEASE_SHA) — aborting"
fi
log "Pinned prod checkout advanced to $RELEASE_SHA (HEAD=$resolved, clean detached pin)"
