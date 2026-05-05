#!/usr/bin/env bash
# BP.W3.13 Phase U — compare Docker default runc vs gVisor runsc.
#
# Run on the same sandbox host that will launch Tier-1 containers in
# production. The output is CSV so the operator can paste it into the
# release note / HANDOFF evidence without post-processing.

set -euo pipefail

IMAGE="${OMNISIGHT_GVISOR_BENCH_IMAGE:-omnisight-agent:latest}"
REPEATS="${OMNISIGHT_GVISOR_BENCH_REPEATS:-5}"
BENCH_CMD="${OMNISIGHT_GVISOR_BENCH_CMD:-python3 -c 'import hashlib; data=b\"omnisight\"*4096; [hashlib.sha256(data).digest() for _ in range(20000)]'}"

if ! command -v docker >/dev/null 2>&1; then
    echo "docker is required" >&2
    exit 2
fi

RUNTIMES_JSON="$(docker info --format '{{json .Runtimes}}' 2>/dev/null || true)"
for runtime in runc runsc; do
    if ! grep -q "\"${runtime}\"" <<<"$RUNTIMES_JSON"; then
        echo "docker runtime '${runtime}' is not registered on this host" >&2
        exit 3
    fi
done

case "$REPEATS" in
    ''|*[!0-9]*)
        echo "OMNISIGHT_GVISOR_BENCH_REPEATS must be a positive integer" >&2
        exit 4
        ;;
esac
if [[ "$REPEATS" -lt 1 ]]; then
    echo "OMNISIGHT_GVISOR_BENCH_REPEATS must be >= 1" >&2
    exit 4
fi

echo "runtime,iteration,elapsed_ms"
for runtime in runc runsc; do
    for ((i = 1; i <= REPEATS; i++)); do
        start_ns="$(date +%s%N)"
        docker run --rm --runtime="$runtime" --network none "$IMAGE" \
            sh -lc "$BENCH_CMD" >/dev/null
        end_ns="$(date +%s%N)"
        elapsed_ms=$(( (end_ns - start_ns) / 1000000 ))
        echo "${runtime},${i},${elapsed_ms}"
    done
done
