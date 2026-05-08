#!/usr/bin/env bash
# OP-740 C1 — idempotent Gerrit ci-bot provisioning.
#
# Runs as a Gerrit admin.  Creates/updates the ci-bot account, writes the
# standard key-bag credentials, installs the OP-740 project.config ACL/label
# scaffolding on refs/meta/config, and optionally verifies live label voting.

set -euo pipefail

GERRIT_HOST="${GERRIT_HOST:-sora.services}"
GERRIT_PORT="${GERRIT_PORT:-29418}"
GERRIT_PROJECT="${GERRIT_PROJECT:-omnisight/OmniSight-Productizer}"
GERRIT_ADMIN_USER="${GERRIT_ADMIN_USER:-sora}"
GERRIT_ADMIN_KEY="${GERRIT_ADMIN_KEY:-$HOME/.ssh/id_ed25519}"
CI_BOT_USERNAME="${CI_BOT_USERNAME:-ci-bot}"
CI_BOT_EMAIL="${CI_BOT_EMAIL:-rt3628+ci-bot@gmail.com}"
CI_BOT_FULL_NAME="${CI_BOT_FULL_NAME:-OmniSight CI Bot}"
CI_BOT_SSH_KEY="${CI_BOT_SSH_KEY:-$HOME/.config/omnisight/gerrit-ci-bot-ed25519}"
CI_BOT_HTTP_PASSWORD="${CI_BOT_HTTP_PASSWORD:-$HOME/.config/omnisight/gerrit-ci-bot-http-password}"
PROJECT_CONFIG_SOURCE="${PROJECT_CONFIG_SOURCE:-.gerrit/project.config}"
VERIFY_CHANGE="${VERIFY_CHANGE:-}"

repo_root="$(git rev-parse --show-toplevel)"
cd "$repo_root"

admin_ssh=(
  ssh
  -i "$GERRIT_ADMIN_KEY"
  -p "$GERRIT_PORT"
  "${GERRIT_ADMIN_USER}@${GERRIT_HOST}"
)

ci_ssh=(
  ssh
  -i "$CI_BOT_SSH_KEY"
  -p "$GERRIT_PORT"
  "${CI_BOT_USERNAME}@${GERRIT_HOST}"
)

ensure_key_bag() {
  mkdir -p "$(dirname "$CI_BOT_SSH_KEY")" "$(dirname "$CI_BOT_HTTP_PASSWORD")"

  if [[ ! -f "$CI_BOT_SSH_KEY" ]]; then
    ssh-keygen -q -t ed25519 -N "" -C "$CI_BOT_EMAIL" -f "$CI_BOT_SSH_KEY"
  fi
  chmod 0600 "$CI_BOT_SSH_KEY"
  chmod 0644 "${CI_BOT_SSH_KEY}.pub"

  if [[ ! -f "$CI_BOT_HTTP_PASSWORD" ]]; then
    if command -v openssl >/dev/null 2>&1; then
      openssl rand -base64 36 > "$CI_BOT_HTTP_PASSWORD"
    else
      uuidgen > "$CI_BOT_HTTP_PASSWORD"
    fi
  fi
  chmod 0600 "$CI_BOT_HTTP_PASSWORD"
}

gerrit_admin() {
  "${admin_ssh[@]}" gerrit "$@"
}

ensure_group() {
  if ! gerrit_admin ls-groups | grep -Fxq "$CI_BOT_USERNAME"; then
    gerrit_admin create-group "$CI_BOT_USERNAME" \
      --visible-to-all \
      --description "OP-740 CI service account group; Verified label only."
  fi
}

ensure_account() {
  if ! gerrit_admin set-account --active "$CI_BOT_USERNAME" >/dev/null 2>&1; then
    gerrit_admin create-account "$CI_BOT_USERNAME" \
      --full-name "$CI_BOT_FULL_NAME" \
      --email "$CI_BOT_EMAIL" \
      --group "$CI_BOT_USERNAME" \
      --ssh-key - < "${CI_BOT_SSH_KEY}.pub"
  fi

  gerrit_admin set-account "$CI_BOT_USERNAME" \
    --full-name "$CI_BOT_FULL_NAME" \
    --add-ssh-key - < "${CI_BOT_SSH_KEY}.pub" || true
  gerrit_admin set-account "$CI_BOT_USERNAME" \
    --http-password "$(tr -d '\n' < "$CI_BOT_HTTP_PASSWORD")"

  gerrit_admin set-members "$CI_BOT_USERNAME" --add "$CI_BOT_USERNAME"
  # Separation of concerns: ci-bot must not inherit Code-Review voting.
  gerrit_admin set-members ai-reviewer-bots --remove "$CI_BOT_USERNAME" >/dev/null 2>&1 || true
  gerrit_admin set-members non-ai-reviewer --remove "$CI_BOT_USERNAME" >/dev/null 2>&1 || true
  gerrit_admin set-members merger-agent-bot --remove "$CI_BOT_USERNAME" >/dev/null 2>&1 || true
}

install_project_config() {
  if [[ ! -f "$PROJECT_CONFIG_SOURCE" ]]; then
    echo "missing project config source: $PROJECT_CONFIG_SOURCE" >&2
    exit 1
  fi

  tmpdir="$(mktemp -d)"
  trap 'rm -rf "$tmpdir"' EXIT

  GIT_SSH_COMMAND="ssh -i $GERRIT_ADMIN_KEY" git clone \
    "ssh://${GERRIT_ADMIN_USER}@${GERRIT_HOST}:${GERRIT_PORT}/${GERRIT_PROJECT}" \
    "$tmpdir/meta"
  GIT_SSH_COMMAND="ssh -i $GERRIT_ADMIN_KEY" \
    git -C "$tmpdir/meta" fetch origin refs/meta/config:refs/remotes/origin/meta/config
  git -C "$tmpdir/meta" checkout -B meta/config refs/remotes/origin/meta/config

  cp "$PROJECT_CONFIG_SOURCE" "$tmpdir/meta/project.config"

  if git -C "$tmpdir/meta" diff --quiet -- project.config; then
    echo "project.config already matches $PROJECT_CONFIG_SOURCE"
    return
  fi

  git -C "$tmpdir/meta" add project.config
  git -C "$tmpdir/meta" \
    -c user.name="$GERRIT_ADMIN_USER" \
    -c user.email="$CI_BOT_EMAIL" \
    commit -m "OP-740: install Verified label scaffolding"
  GIT_SSH_COMMAND="ssh -i $GERRIT_ADMIN_KEY" \
    git -C "$tmpdir/meta" push origin HEAD:refs/meta/config
}

verify_optional_change() {
  if [[ -z "$VERIFY_CHANGE" ]]; then
    echo "VERIFY_CHANGE not set; skipping live vote smoke test."
    return
  fi

  "${ci_ssh[@]}" gerrit review --label Verified=+1 "$VERIFY_CHANGE"
  "${ci_ssh[@]}" gerrit review --label Verified=-1 "$VERIFY_CHANGE"

  if "${ci_ssh[@]}" gerrit review --label Code-Review=+1 "$VERIFY_CHANGE"; then
    echo "ERROR: ci-bot unexpectedly voted Code-Review." >&2
    exit 1
  fi
}

ensure_key_bag
ensure_group
ensure_account
install_project_config
verify_optional_change

echo "ci-bot provisioning complete."
