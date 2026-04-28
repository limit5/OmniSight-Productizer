#!/bin/sh
# W14.1: omnisight-web-preview sidecar entrypoint.
#
# This script is intentionally a thin passthrough. The backend's
# `POST /web-sandbox/preview` (W14.2) decides which command to run
# (`pnpm dev`, `bun --bun nuxt dev`, `vite preview`, etc.) and supplies
# it as the container CMD. The entrypoint's only jobs are:
#
#   1. Refuse to run as uid 0 — defence in depth on top of the
#      `USER 10002:10002` Dockerfile directive, in case a future operator
#      forgets `--user` in `docker run` and the image is rebuilt without
#      the USER line.
#   2. `cd /workspace` so the operator's bind-mounted source is the CWD.
#   3. `exec "$@"` so signals (SIGTERM from W14.5 idle-kill /
#      W14.9 cgroup OOM) reach the dev server directly without a shell
#      intermediate eating them.
#
# A bare `docker run omnisight-web-preview` with no CMD falls through to
# the Dockerfile's CMD (`pnpm dev --host 0.0.0.0`) — useful for the
# image-build smoke test, not for production launches.

set -eu

if [ "$(id -u)" = "0" ]; then
    echo "web-preview-entrypoint: refusing to run as root (uid 0)" >&2
    exit 1
fi

cd /workspace

if [ "$#" -eq 0 ]; then
    # No CMD supplied — fall through to the Dockerfile-baked default.
    # Should only happen during the image-build smoke test.
    set -- pnpm dev --host 0.0.0.0
fi

exec "$@"
