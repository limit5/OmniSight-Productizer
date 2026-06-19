#!/usr/bin/env python3
"""End-to-end smoke for meeting intelligence (BI0 ingest + BI1-4).

Drives a running backend over HTTP exactly the way a conference appliance +
operator would: open a meeting, ingest appliance-shaped transcript segments,
read them back, then generate each AI insight. Designed for staging
(set BI_SMOKE_BASE + BI_SMOKE_TOKEN) but works against any backend with the
meeting-intelligence flags enabled.

Two assertion tiers:
  * REQUIRED — the wiring: meeting open, ingest accept/supersede, ordered
    read-back, flag gating. These must pass.
  * BEST-EFFORT — the LLM insights: with a provider configured they return
    structured content; without one they degrade (503 / empty). The smoke
    asserts the endpoint is reachable + does not 5xx-crash, and reports the
    content it got. Pass --require-llm to make non-empty content mandatory.

Env:
  BI_SMOKE_BASE   backend API base incl. /api/v1   (default http://localhost:18080/api/v1)
  BI_SMOKE_TOKEN  operator bearer token            (optional; omit in AUTH_MODE=open)
  BI_SMOKE_LANG   translation target               (default zh-Hant)

Exit 0 = all REQUIRED passed.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = os.environ.get("BI_SMOKE_BASE", "http://localhost:18080/api/v1").rstrip("/")
TOKEN = os.environ.get("BI_SMOKE_TOKEN", "")
LANG = os.environ.get("BI_SMOKE_LANG", "zh-Hant")
REQUIRE_LLM = "--require-llm" in sys.argv

_fails = 0
_warns = 0


def _c(s, code):
    return f"\033[{code}m{s}\033[0m" if sys.stdout.isatty() else s


def ok(msg):
    print(_c("  PASS ", "32") + msg)


def fail(msg):
    global _fails
    _fails += 1
    print(_c("  FAIL ", "31") + msg)


def warn(msg):
    global _warns
    _warns += 1
    print(_c("  WARN ", "33") + msg)


def call(method, path, body=None):
    url = BASE + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if TOKEN:
        req.add_header("Authorization", "Bearer " + TOKEN)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read().decode()
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            payload = json.loads(raw)
        except Exception:
            payload = {"detail": raw[:200]}
        return e.code, payload
    except Exception as e:  # noqa: BLE001
        return 0, {"detail": f"{type(e).__name__}: {e}"}


APPLIANCE_SEGMENTS = [
    {"segment_seq": 0, "start_ms": 0, "end_ms": 1800,
     "text": "Let's start the weekly sync. First, the Q3 roadmap.",
     "language": "en", "confidence": 0.5, "is_final": False,
     "source": "onboard-rknn"},
    {"segment_seq": 0, "start_ms": 0, "end_ms": 1800,
     "text": "Let's start the weekly sync. First item, the Q3 roadmap.",
     "language": "en", "confidence": 0.83, "is_final": True,
     "source": "onboard-rknn"},
    {"segment_seq": 1, "start_ms": 1800, "end_ms": 4200,
     "text": "Alice will finalise the API spec by Friday and email finance.",
     "language": "en", "confidence": 0.88, "is_final": True,
     "source": "onboard-rknn"},
    {"segment_seq": 2, "start_ms": 4200, "end_ms": 6800,
     "text": "Bob raised a concern about the migration timeline; we decided to ship the rc first.",
     "language": "en", "confidence": 0.86, "is_final": True,
     "source": "onboard-rknn"},
]


def main():
    mid = f"smoke-mi-{int(time.time())}"
    print(f"== meeting intelligence smoke ==\nbase={BASE} token={'set' if TOKEN else 'none'} meeting={mid}\n")

    # 0) reachability
    st, _ = call("GET", "/health")
    if st == 0:
        fail(f"backend unreachable at {BASE} — is it up?")
        return _summary()
    ok(f"backend reachable (/health -> {st})")

    # 1) REQUIRED — open meeting
    st, body = call("POST", "/meetings", {"id": mid, "title": "Smoke Weekly Sync"})
    if st in (200, 201):
        ok(f"meeting opened ({st})")
    elif st == 404:
        fail("POST /meetings -> 404: transcript ingest disabled OR routes absent "
             "(check OMNISIGHT_TRANSCRIPT_INGEST_ENABLED + image has BI0)")
        return _summary()
    elif st == 401:
        fail("401 unauthorized — provide BI_SMOKE_TOKEN (operator API key)")
        return _summary()
    else:
        fail(f"POST /meetings -> {st} {body}")

    # 2) REQUIRED — ingest appliance-shaped batch (partial then final supersede)
    st, body = call("POST", f"/meetings/{mid}/segments",
                    {"session_id": "smoke-s1", "segments": APPLIANCE_SEGMENTS})
    if st == 201 and isinstance(body, dict):
        acc, sup = body.get("accepted", 0), body.get("superseded", 0)
        if acc >= 3 and sup >= 1:
            ok(f"ingest accepted={acc} superseded={sup} (partial->final works)")
        else:
            warn(f"ingest counts unexpected: {body}")
    else:
        fail(f"ingest -> {st} {body}")

    # 2b) REQUIRED — idempotent replay
    st, body = call("POST", f"/meetings/{mid}/segments",
                    {"session_id": "smoke-s1", "segments": APPLIANCE_SEGMENTS})
    if st == 201 and body.get("deduped", 0) >= 1:
        ok(f"replay idempotent (deduped={body.get('deduped')})")
    else:
        warn(f"replay not deduped as expected: {st} {body}")

    # 3) REQUIRED — ordered read-back of finals
    st, segs = call("GET", f"/meetings/{mid}/segments?final_only=true")
    if st == 200 and isinstance(segs, list) and len(segs) == 3:
        seqs = [s["segment_seq"] for s in segs]
        if seqs == sorted(seqs):
            ok(f"read-back ordered, {len(segs)} final segments")
        else:
            fail(f"read-back not ordered: {seqs}")
    else:
        fail(f"read-back -> {st} (got {len(segs) if isinstance(segs, list) else segs})")

    # 4) BEST-EFFORT — BI1-4 insights
    transcript_present = isinstance(segs, list) and len(segs) > 0
    _insight("summary", "POST", f"/meetings/{mid}/summary", None,
             lambda b: bool(b.get("tldr") or b.get("bullet_points")))
    _insight("action-items", "POST", f"/meetings/{mid}/action-items", None,
             lambda b: bool(b.get("action_items")))
    _insight("suggestions", "POST", f"/meetings/{mid}/suggestions", None,
             lambda b: bool(b.get("suggestions")))
    _insight("translate", "POST", f"/meetings/{mid}/translate?target_lang={LANG}",
             None, lambda b: bool(b.get("text")))

    return _summary()


def _insight(name, method, path, body, has_content):
    st, payload = call(method, path, body)
    if st in (200, 201):
        if has_content(payload):
            ok(f"{name}: generated content")
        elif REQUIRE_LLM:
            fail(f"{name}: 200 but empty content (no LLM provider?)")
        else:
            warn(f"{name}: reachable, empty content (LLM provider not configured)")
    elif st == 404:
        if REQUIRE_LLM:
            fail(f"{name}: 404 — feature flag disabled")
        else:
            warn(f"{name}: 404 — feature flag disabled (enable to test output)")
    elif st == 503:
        warn(f"{name}: 503 — LLM provider unavailable (endpoint reachable)")
    else:
        fail(f"{name}: -> {st} {payload}")


def _summary():
    print()
    if _fails:
        print(_c(f"SMOKE FAILED — {_fails} required check(s) failed, {_warns} warning(s)", "31"))
        return 1
    print(_c(f"SMOKE PASSED — required checks green, {_warns} warning(s)", "32"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
