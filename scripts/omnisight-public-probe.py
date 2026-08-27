#!/usr/bin/env python3
"""omnisight-public-probe — alert when the PUBLIC entry stops serving.

Born 2026-08-27: the prod cloudflared tunnel died on 2026-08-06 and
ai.sora-dev.app returned Cloudflare 530 for THREE WEEKS — local checks all
green, nothing probed the public path. This closes that gap via the proven
channel (exit 1 -> OnFailure -> JIRA, OP-2728).

Probes are env-overridable:
  OMNISIGHT_PUBLIC_PROBES  default 'https://ai.sora-dev.app/=200'
                           format: url=expected_status[,url=expected...]
Note /readyz is NOT probed publicly — infra endpoints are deliberately not
exposed through the tunnel (public 404 there is by design); the homepage is
the honest end-to-end signal (CF edge -> tunnel -> caddy -> frontend).
"""

import os
import sys
import time
import urllib.error
import urllib.request

PROBES = os.environ.get("OMNISIGHT_PUBLIC_PROBES", "https://ai.sora-dev.app/=200")


def status_of(url):
    req = urllib.request.Request(url, method="GET", headers={"User-Agent": "omnisight-public-probe"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code          # 4xx/5xx still reached an HTTP responder
    except Exception as e:     # noqa: BLE001 — DNS/TLS/timeout = unreachable
        return f"unreachable({type(e).__name__})"


def main():
    bad = 0
    for spec in [s.strip() for s in PROBES.split(",") if s.strip()]:
        url, _, want = spec.rpartition("=")
        # tolerate transient edge/tunnel blips: 3 attempts over ~40s
        got = None
        for attempt in range(3):
            got = status_of(url)
            if str(got) == want:
                break
            if attempt < 2:
                time.sleep(20)
        if str(got) == want:
            print(f"ok   {url} -> {got}")
        else:
            bad += 1
            print(f"FAIL {url} -> {got} (expected {want}) — public path broken "
                  f"(tunnel down shows as 530, origin-route issues as 404/502)")
    if bad:
        print(f"{bad} public probe(s) failing — exiting 1 so OnFailure files the JIRA alert")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
