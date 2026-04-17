"""G2 #5 — Rolling-deploy soak contract + in-memory integration test.

TODO row 1349:
    整合測試：部署中對 ``/api/v1/*`` 持續打流量，0 個 5xx

This file has **two halves**:

1. **Contract tests** (``TestSoakScript*``) pin the operator-facing
   tool ``scripts/soak_rolling_deploy.py``: CLI flags, exit codes,
   JSON summary shape, and the SoakResult aggregation logic.

2. **Integration test** (``TestRollingDeploySoakIntegration``) spins
   up two in-process "replica" HTTP servers + a tiny Python proxy
   that reproduces Caddy's round-robin + active probe eject + passive
   retry contract, drives a full rolling-restart timeline against it
   (drain A → stop A → restart A → same for B), runs the soak loop
   throughout, and asserts **0 × 5xx observed**.

The integration half is deliberately pure stdlib (``http.server`` +
``urllib``) so it runs in the same sub-second envelope as the other
G2 contract tests — no Docker, no real Caddy, no uvicorn fork. The
sub-second runtime is important: G2 #5 is a CI gate, not a nightly
soak, and a 60 s real-traffic soak would blow the regression budget.

Timing shrink
-------------
Production Caddyfile ejects a replica in ~6 s (2 s interval × 3
fails). The in-test proxy uses ``probe_interval=0.05`` and
``health_fails=3`` for a ~0.15 s ejection budget, so the whole
rolling timeline (drain A → recreate A → drain B → recreate B) fits
in ~3-4 seconds. The invariant being tested is *the shape* of the
eject+retry handshake, not the exact 2 s-vs-0.05 s wall-clock.

Siblings:
    * scripts/soak_rolling_deploy.py            — the subject
    * test_deploy_sh_rolling.py                 — G2 #3 rolling flow
    * test_reverse_proxy_caddyfile.py           — G2 #1 listener/pool
    * test_reverse_proxy_health_eject.py        — G2 #4 eject contract
"""

from __future__ import annotations

import json
import os
import socket
import stat
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Literal
from urllib import error as urlerror
from urllib import request as urlrequest

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SOAK_SCRIPT = PROJECT_ROOT / "scripts" / "soak_rolling_deploy.py"


# ---------------------------------------------------------------------------
# (1) File-level hygiene — the script is a real operator deliverable
# ---------------------------------------------------------------------------


class TestSoakScriptHygiene:
    def test_script_exists(self) -> None:
        assert SOAK_SCRIPT.is_file(), (
            f"G2 #5 deliverable missing: {SOAK_SCRIPT}"
        )

    def test_script_is_executable(self) -> None:
        # Operators chmod+x once at repo clone; the bit must persist
        # across commits so `./scripts/soak_rolling_deploy.py` keeps
        # working without a shebang-less `python3` prefix.
        mode = SOAK_SCRIPT.stat().st_mode
        assert mode & stat.S_IXUSR, "soak script must be executable"

    def test_script_has_python3_shebang(self) -> None:
        first_line = SOAK_SCRIPT.read_text(encoding="utf-8").splitlines()[0]
        assert first_line.startswith("#!") and "python" in first_line, (
            "soak script must declare a python3 shebang so operators can "
            "run it directly from deploy.sh or a deploy-pipeline stage"
        )

    def test_script_is_stdlib_only(self) -> None:
        # The whole point of stdlib-only is: operators run this from the
        # deploy host, which often does NOT have the backend venv
        # installed. Pulling in `httpx`/`requests`/`aiohttp` would
        # break first-boot on a fresh VM. Pin it.
        text = SOAK_SCRIPT.read_text(encoding="utf-8")
        forbidden = (
            "import httpx",
            "import requests",
            "import aiohttp",
            "from httpx ",
            "from requests ",
            "from aiohttp ",
        )
        for token in forbidden:
            assert token not in text, (
                f"soak script must be stdlib-only (found `{token}`); "
                "operators often run this from a bare deploy host without "
                "the backend venv installed"
            )


# ---------------------------------------------------------------------------
# (2) CLI surface — flags + help string + bad-arg handling
# ---------------------------------------------------------------------------


def _run_soak_script(args: list[str], timeout: float = 20.0) -> subprocess.CompletedProcess:
    """Invoke the soak script as a subprocess."""
    return subprocess.run(
        [sys.executable, str(SOAK_SCRIPT), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


class TestSoakScriptCLI:
    def test_help_flag_exits_zero(self) -> None:
        r = _run_soak_script(["--help"])
        assert r.returncode == 0
        assert "soak" in r.stdout.lower() or "soak" in r.stderr.lower()

    @pytest.mark.parametrize(
        "flag",
        ["--base-url", "--duration", "--concurrency", "--endpoint", "--request-timeout", "--insecure"],
    )
    def test_help_names_each_flag(self, flag: str) -> None:
        r = _run_soak_script(["--help"])
        assert flag in r.stdout, f"--help must document `{flag}`"

    def test_bad_duration_exits_four(self) -> None:
        r = _run_soak_script(["--duration", "0", "--concurrency", "1"])
        assert r.returncode == 4, (
            f"bad --duration must exit 4 (got {r.returncode}); "
            "exit 2 is reserved for 5xx observed"
        )

    def test_bad_concurrency_exits_four(self) -> None:
        r = _run_soak_script(["--duration", "1", "--concurrency", "0"])
        assert r.returncode == 4

    def test_unreachable_target_exits_three(self) -> None:
        # Hit a port we're certain is closed so every worker gets a
        # transport error → zero responses with a status code → the
        # soak correctly reports "no traffic generated" (exit 3).
        port = _get_free_port()
        r = _run_soak_script(
            [
                "--base-url", f"http://127.0.0.1:{port}",
                "--duration", "0.5",
                "--concurrency", "1",
                "--request-timeout", "0.5",
            ],
            timeout=10.0,
        )
        # total_requests > 0 but all transport errors → still exit 0 only
        # if no 5xx were observed AND at least one request was counted.
        # With zero-status-code responses we record them in total still —
        # but the 5xx gate is what we care about here.
        assert r.returncode in (0, 3), (
            "unreachable target with only transport errors must exit 0 (no 5xx) "
            "or 3 (if no requests got generated at all), never 2"
        )
        # Make sure we did not falsely report 5xx.
        assert r.returncode != 2, "transport errors must NOT be counted as 5xx"


# ---------------------------------------------------------------------------
# (3) JSON summary contract — CI pipelines scrape the last stdout line
# ---------------------------------------------------------------------------


class TestSoakScriptJsonSummary:
    def test_summary_is_valid_json(self) -> None:
        port = _get_free_port()
        r = _run_soak_script(
            [
                "--base-url", f"http://127.0.0.1:{port}",
                "--duration", "0.2",
                "--concurrency", "1",
                "--request-timeout", "0.2",
            ],
            timeout=10.0,
        )
        # Last non-empty stdout line must be a JSON summary.
        stdout_lines = [ln for ln in r.stdout.splitlines() if ln.strip()]
        assert stdout_lines, "soak script must emit at least one stdout line"
        summary = json.loads(stdout_lines[-1])
        assert isinstance(summary, dict)

    def test_summary_schema_keys(self) -> None:
        port = _get_free_port()
        r = _run_soak_script(
            [
                "--base-url", f"http://127.0.0.1:{port}",
                "--duration", "0.2",
                "--concurrency", "1",
                "--request-timeout", "0.2",
            ],
            timeout=10.0,
        )
        stdout_lines = [ln for ln in r.stdout.splitlines() if ln.strip()]
        summary = json.loads(stdout_lines[-1])
        required = {
            "total_requests",
            "status_counts",
            "transport_errors",
            "count_2xx",
            "count_4xx",
            "count_5xx",
            "latency_p50_ms",
            "latency_p95_ms",
            "latency_p99_ms",
        }
        missing = required - set(summary.keys())
        assert not missing, f"summary missing keys: {missing}"


# ---------------------------------------------------------------------------
# (4) SoakResult class — thread-safe aggregator used by run_soak()
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def soak_module():
    # Import the script as a module via importlib.util — it's in
    # scripts/, not an importable package.
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "omnisight_soak_rolling_deploy", SOAK_SCRIPT
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestSoakResult:
    def test_total_counts_all_records(self, soak_module) -> None:
        r = soak_module.SoakResult()
        r.record(200, 10.0)
        r.record(500, 20.0)
        r.record(None, 30.0)
        assert r.total == 3

    def test_5xx_detection(self, soak_module) -> None:
        r = soak_module.SoakResult()
        r.record(200, 1.0)
        r.record(404, 2.0)
        r.record(500, 3.0)
        r.record(502, 4.0)
        r.record(503, 5.0)
        assert r.count_5xx() == 3
        assert r.count_4xx() == 1
        assert r.count_2xx() == 1

    def test_transport_errors_not_counted_as_5xx(self, soak_module) -> None:
        # The load-bearing invariant for the 5xx gate: a connection
        # refused during a mid-rolling-restart probe is NOT a 5xx —
        # it's a legitimate transport error that Caddy would retry
        # through. Counting these as 5xx would false-positive the
        # entire soak during a legit rolling deploy.
        r = soak_module.SoakResult()
        r.record(None, 1.0)  # transport error
        r.record(None, 2.0)
        assert r.count_5xx() == 0
        assert r.transport_errors == 2

    def test_percentiles_return_numbers(self, soak_module) -> None:
        r = soak_module.SoakResult()
        for i in range(100):
            r.record(200, float(i))
        assert 45 <= r.percentile(0.5) <= 55
        assert r.percentile(0.99) >= r.percentile(0.5)

    def test_summary_includes_5xx_count(self, soak_module) -> None:
        r = soak_module.SoakResult()
        r.record(500, 1.0)
        summary = r.to_summary()
        assert summary["count_5xx"] == 1
        assert summary["total_requests"] == 1


# ---------------------------------------------------------------------------
# (5) In-memory integration — the "0 × 5xx during rolling restart" invariant
# ---------------------------------------------------------------------------
#
# Fixture hierarchy:
#   FakeReplica (one HTTP server, toggleable ready/draining/stopped)
#   └─ FakeCaddyProxy (round-robin + active probe + retry over replicas)
#      └─ RollingRestartDriver (drives the A-then-B timeline)
#         └─ test: assert run_soak() against the proxy observes 0 × 5xx.


def _get_free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


State = Literal["ready", "draining", "stopped"]


class FakeReplica:
    """One backend replica.

    ``state`` is the observable truth:
      * ready    → /readyz and /api/v1/* both 200
      * draining → /readyz 503, /api/v1/* 503  (matches lifecycle.py
                   middleware behaviour when `shutting_down=True`)
      * stopped  → the HTTP server is literally not running; clients
                   see connection-refused.

    The server is a ``ThreadingHTTPServer`` so the active-probe and
    soak client threads don't serialise through one another.
    """

    def __init__(self, name: str, port: int) -> None:
        self.name = name
        self.port = port
        self.state: State = "stopped"
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._server is not None:
            return
        replica = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a, **kw) -> None:  # silence
                pass

            def do_GET(self) -> None:
                # Replica behaviour per state:
                if replica.state == "draining":
                    # /readyz 503 so active probe ejects; /api/v1/* 503
                    # to simulate lifecycle.py's drain middleware
                    # rejecting new work. The proxy is responsible for
                    # retrying to the other replica → client sees 200.
                    self.send_response(503)
                    self.send_header("Content-Type", "text/plain")
                    self.end_headers()
                    self.wfile.write(b"draining")
                    return
                # ready
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(
                    json.dumps(
                        {"replica": replica.name, "status": "UP"}
                    ).encode("utf-8")
                )

        self._server = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name=f"replica-{self.name}",
            daemon=True,
        )
        self._thread.start()
        self.state = "ready"

    def drain(self) -> None:
        """Mark as draining — next /readyz + /api/v1/* responses go 503."""
        assert self._server is not None, "cannot drain a stopped replica"
        self.state = "draining"

    def stop(self) -> None:
        if self._server is None:
            self.state = "stopped"
            return
        self._server.shutdown()
        self._server.server_close()
        self._server = None
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None
        self.state = "stopped"

    def ready(self) -> None:
        """Come back online after a simulated recreate."""
        if self._server is None:
            self.start()
        self.state = "ready"


class FakeCaddyProxy:
    """Pure-stdlib proxy reproducing the eject + retry contract.

    What we reproduce:
      * round-robin over all *non-ejected* upstreams
      * active probe every ``probe_interval``:
          - /readyz 200 → passes++; passes >= 1 → un-eject
          - anything else → fails++; fails >= ``health_fails`` → eject
      * per-request retry budget: if the chosen upstream returns 5xx
        OR connection-refuses, try the next non-ejected upstream
        (up to ``retry_budget`` attempts total). This maps directly
        to Caddy's ``lb_try_duration`` + ``lb_try_interval`` pair.
      * if ALL upstreams are currently ejected, the proxy still
        attempts each once as a last resort — matches Caddy's
        "never return 5xx if ANY upstream might work" behaviour.
    """

    def __init__(
        self,
        port: int,
        replicas: list[FakeReplica],
        probe_interval: float = 0.05,
        health_fails: int = 3,
        retry_budget: int = 4,
    ) -> None:
        self.port = port
        self.replicas = replicas
        self.probe_interval = probe_interval
        self.health_fails = health_fails
        self.retry_budget = retry_budget
        self._ejected: set[str] = set()
        self._fail_counts: dict[str, int] = {r.name: 0 for r in replicas}
        self._lock = threading.Lock()
        self._rr_counter = 0
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._probe_thread: threading.Thread | None = None
        self._probe_stop = threading.Event()

    # ── upstream selection ──────────────────────────────────────────
    def _select_order(self) -> list[FakeReplica]:
        """Round-robin order with ejected replicas pushed to the end."""
        with self._lock:
            idx = self._rr_counter
            self._rr_counter += 1
        # Rotate so each call picks a different starting replica.
        ordered = self.replicas[idx % len(self.replicas):] + self.replicas[: idx % len(self.replicas)]
        non_ejected = [r for r in ordered if r.name not in self._ejected]
        ejected = [r for r in ordered if r.name in self._ejected]
        # Non-ejected first, then ejected as fall-back — mirrors Caddy's
        # "never 5xx if ANY upstream might work" behaviour.
        return non_ejected + ejected

    def _fetch_upstream(
        self, replica: FakeReplica, path: str
    ) -> tuple[int | None, bytes]:
        """Issue a single GET to a replica. Returns (status, body)."""
        url = f"http://127.0.0.1:{replica.port}{path}"
        try:
            with urlrequest.urlopen(url, timeout=1.0) as resp:
                return resp.getcode(), resp.read()
        except urlerror.HTTPError as e:
            return e.code, b""
        except Exception:
            return None, b""

    def proxy_once(self, path: str) -> tuple[int, bytes]:
        """Proxy one request through the eject + retry machinery.

        Returns the status code the client would observe. This is
        the hot path: every soak request funnels through here, and
        the G2 #5 invariant reduces to "this function never returns
        5xx during a rolling restart".
        """
        order = self._select_order()
        last_status: int | None = None
        attempts = 0
        for replica in order:
            if attempts >= self.retry_budget:
                break
            attempts += 1
            status, body = self._fetch_upstream(replica, path)
            if status is not None and 200 <= status < 500:
                return status, body
            # 5xx or transport error — try the next upstream.
            last_status = status
        # Every upstream failed. Surface a 502 to the client — but in
        # the G2 #5 contract this path must never be exercised
        # (because at least ONE replica is always ready).
        return (last_status if last_status and 500 <= last_status < 600 else 502), b""

    # ── active probe thread ─────────────────────────────────────────
    def _probe_loop(self) -> None:
        while not self._probe_stop.is_set():
            for replica in self.replicas:
                status, _ = self._fetch_upstream(replica, "/readyz")
                ok = status is not None and 200 <= status < 300
                with self._lock:
                    if ok:
                        # One good probe re-admits (matches Caddy
                        # `health_passes 1`).
                        self._fail_counts[replica.name] = 0
                        self._ejected.discard(replica.name)
                    else:
                        self._fail_counts[replica.name] = (
                            self._fail_counts.get(replica.name, 0) + 1
                        )
                        if self._fail_counts[replica.name] >= self.health_fails:
                            self._ejected.add(replica.name)
            self._probe_stop.wait(self.probe_interval)

    # ── HTTP server ─────────────────────────────────────────────────
    def start(self) -> None:
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a, **kw) -> None:
                pass

            def do_GET(self) -> None:
                status, body = proxy.proxy_once(self.path)
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    return

        self._server = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="fake-caddy", daemon=True
        )
        self._thread.start()
        self._probe_thread = threading.Thread(
            target=self._probe_loop, name="fake-caddy-probe", daemon=True
        )
        self._probe_thread.start()

    def stop(self) -> None:
        self._probe_stop.set()
        if self._probe_thread is not None:
            self._probe_thread.join(timeout=2.0)
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=1.0)


# ---------------------------------------------------------------------------
# Rolling restart driver
# ---------------------------------------------------------------------------


def _rolling_restart(replica: FakeReplica, probe_eject_window: float) -> None:
    """Drive one replica through drain → stop → recreate (ready).

    The sequence mirrors scripts/deploy.sh `rolling_restart_replica()`:
      (A) replica.drain()    — /readyz starts returning 503
      (B) wait probe_eject_window — Caddy's active probe ejects it
      (C) replica.stop()     — actual container stop
      (D) replica.ready()    — recreate + back online
    """
    replica.drain()
    time.sleep(probe_eject_window)
    replica.stop()
    # Short "recreate" beat so the proxy has a chance to observe the
    # connection-refused state (exercises the transport-error retry
    # path, distinct from the 5xx-retry path).
    time.sleep(0.05)
    replica.ready()


class TestRollingDeploySoakIntegration:
    """The canonical 0 × 5xx under rolling restart test."""

    @pytest.fixture()
    def topology(self):
        """Start both replicas + proxy, tear down at end of test."""
        a_port = _get_free_port()
        b_port = _get_free_port()
        proxy_port = _get_free_port()
        a = FakeReplica("backend-a", a_port)
        b = FakeReplica("backend-b", b_port)
        a.start()
        b.start()
        proxy = FakeCaddyProxy(proxy_port, [a, b])
        proxy.start()
        # Let the active probe settle — both replicas must be probed
        # healthy at least once before the soak begins.
        time.sleep(0.2)
        try:
            yield a, b, proxy
        finally:
            proxy.stop()
            a.stop()
            b.stop()

    def test_rolling_a_then_b_produces_zero_5xx(
        self, soak_module, topology
    ) -> None:
        """The headline invariant: full A→B rolling restart = 0 × 5xx."""
        a, b, proxy = topology
        base_url = f"http://127.0.0.1:{proxy.port}"

        stop_event = threading.Event()
        result = soak_module.SoakResult()

        def soak_thread():
            # Run for up to 4 s — the rolling driver will finish well
            # before that and signal via stop_event.
            res = soak_module.run_soak(
                base_url=base_url,
                duration_seconds=4.0,
                concurrency=4,
                endpoints=("/api/v1/health",),
                request_timeout=1.0,
                stop_event=stop_event,
            )
            # Copy the aggregate into the outer result.
            with result._lock:
                result.total = res.total
                result.status_counts = dict(res.status_counts)
                result.transport_errors = res.transport_errors
                result.latencies_ms = list(res.latencies_ms)

        t = threading.Thread(target=soak_thread, daemon=True)
        t.start()

        # Give the soak workers a head start so pre-deploy baseline
        # traffic is observed.
        time.sleep(0.3)

        # --- roll backend-a ---
        _rolling_restart(a, probe_eject_window=0.3)
        # Let the proxy re-admit backend-a via its active probe.
        time.sleep(0.3)
        # --- roll backend-b ---
        _rolling_restart(b, probe_eject_window=0.3)
        time.sleep(0.3)

        # Post-roll cool-down — let outstanding requests drain.
        time.sleep(0.2)
        stop_event.set()
        t.join(timeout=5.0)

        summary = result.to_summary()
        # The load-bearing assertion.
        assert summary["count_5xx"] == 0, (
            f"rolling deploy produced 5xx responses: {summary}"
        )
        # Sanity: we actually generated traffic (a silent empty soak
        # would falsely "pass" the 5xx check).
        assert summary["total_requests"] >= 10, (
            f"soak did not generate enough traffic: total={summary['total_requests']}"
        )
        # Sanity: the bulk of responses are 2xx (proxy retry found a
        # live upstream every time).
        assert summary["count_2xx"] >= summary["total_requests"] * 0.9, (
            f"too many non-2xx — retry logic not compensating: {summary}"
        )

    def test_proxy_retry_masks_single_replica_drain(
        self, soak_module, topology
    ) -> None:
        """Drain JUST replica A for the entire soak window.

        With B still healthy, the proxy's retry-on-5xx path must
        keep all client-facing responses at 2xx. This isolates the
        retry invariant from the active-probe invariant.
        """
        a, b, proxy = topology
        base_url = f"http://127.0.0.1:{proxy.port}"

        a.drain()
        try:
            result = soak_module.run_soak(
                base_url=base_url,
                duration_seconds=0.6,
                concurrency=4,
                endpoints=("/api/v1/health",),
                request_timeout=1.0,
            )
        finally:
            a.ready()

        summary = result.to_summary()
        assert summary["count_5xx"] == 0, (
            f"draining A alone leaked 5xx — retry logic is broken: {summary}"
        )
        assert summary["total_requests"] >= 4

    def test_proxy_retry_masks_single_replica_stop(
        self, soak_module, topology
    ) -> None:
        """Stop (connection-refused) replica A for the soak window.

        Same invariant as the drain case, but the failure mode is
        transport-level (ECONNREFUSED) rather than 503. The proxy
        must retry onto B and the client must see 2xx.
        """
        a, b, proxy = topology
        base_url = f"http://127.0.0.1:{proxy.port}"

        a.stop()
        # Give the active probe time to eject A so even the FIRST
        # chosen upstream on round-robin doesn't land on A.
        time.sleep(0.3)
        try:
            result = soak_module.run_soak(
                base_url=base_url,
                duration_seconds=0.5,
                concurrency=4,
                endpoints=("/api/v1/health",),
                request_timeout=1.0,
            )
        finally:
            a.ready()

        summary = result.to_summary()
        assert summary["count_5xx"] == 0, (
            f"stopping A leaked 5xx — transport-error retry is broken: {summary}"
        )

    def test_baseline_both_replicas_healthy(
        self, soak_module, topology
    ) -> None:
        """Sanity check: with NO rolling restart, traffic is 100% 2xx."""
        _a, _b, proxy = topology
        base_url = f"http://127.0.0.1:{proxy.port}"

        result = soak_module.run_soak(
            base_url=base_url,
            duration_seconds=0.4,
            concurrency=4,
            endpoints=("/api/v1/health",),
            request_timeout=1.0,
        )
        summary = result.to_summary()
        assert summary["count_5xx"] == 0
        assert summary["count_4xx"] == 0
        assert summary["count_2xx"] == summary["total_requests"]


# ---------------------------------------------------------------------------
# (6) Proxy unit tests — isolate the eject + retry logic
# ---------------------------------------------------------------------------


class TestFakeCaddyProxyLogic:
    """Direct tests of the FakeCaddyProxy contract, no soak involved."""

    @pytest.fixture()
    def topology(self):
        a = FakeReplica("backend-a", _get_free_port())
        b = FakeReplica("backend-b", _get_free_port())
        a.start()
        b.start()
        proxy = FakeCaddyProxy(_get_free_port(), [a, b], probe_interval=0.05)
        proxy.start()
        time.sleep(0.2)
        try:
            yield a, b, proxy
        finally:
            proxy.stop()
            a.stop()
            b.stop()

    def test_round_robin_distributes_traffic(self, topology) -> None:
        _a, _b, proxy = topology
        statuses = [proxy.proxy_once("/api/v1/health")[0] for _ in range(20)]
        assert all(s == 200 for s in statuses)

    def test_draining_replica_is_skipped_via_retry(self, topology) -> None:
        a, _b, proxy = topology
        a.drain()
        statuses = [proxy.proxy_once("/api/v1/health")[0] for _ in range(20)]
        # Every response must be 2xx because the proxy retries to B.
        assert all(200 <= s < 300 for s in statuses), statuses

    def test_active_probe_ejects_dead_replica(self, topology) -> None:
        a, _b, proxy = topology
        a.stop()
        # Wait for probe to cross the health_fails threshold.
        # probe_interval=0.05 × 3 fails = 0.15 s minimum.
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if "backend-a" in proxy._ejected:
                break
            time.sleep(0.05)
        assert "backend-a" in proxy._ejected, (
            "active probe must eject a stopped replica within 3 failed probes"
        )

    def test_active_probe_readmits_recovered_replica(self, topology) -> None:
        a, _b, proxy = topology
        a.stop()
        # Wait for eject.
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and "backend-a" not in proxy._ejected:
            time.sleep(0.05)
        assert "backend-a" in proxy._ejected
        # Recover; one good probe should un-eject.
        a.ready()
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and "backend-a" in proxy._ejected:
            time.sleep(0.05)
        assert "backend-a" not in proxy._ejected, (
            "one good probe must re-admit the recovered replica (matches "
            "Caddyfile `health_passes 1`)"
        )


# ---------------------------------------------------------------------------
# (7) Cross-file consistency — soak script references the right contracts
# ---------------------------------------------------------------------------


class TestCrossFileConsistency:
    def test_soak_script_references_caddy(self) -> None:
        # The runbook for running this script is embedded in the
        # module docstring. It must point back at the reverse-proxy
        # deliverable so operators don't reinvent the timing budget.
        text = SOAK_SCRIPT.read_text(encoding="utf-8")
        assert "Caddy" in text or "caddy" in text
        assert "deploy.sh" in text

    def test_soak_script_references_api_v1(self) -> None:
        text = SOAK_SCRIPT.read_text(encoding="utf-8")
        assert "/api/v1/" in text, (
            "soak script must hammer /api/v1/* endpoints — that's the "
            "load-bearing contract from TODO row 1349"
        )

    def test_soak_script_exit_code_contract_documented(self) -> None:
        text = SOAK_SCRIPT.read_text(encoding="utf-8")
        # Each exit code is a CI contract; the docstring must call them
        # out or a later edit can silently drift (e.g. exit 2 for bad
        # args would make the deploy pipeline miss real 5xx).
        for code in ("0", "2", "3", "4"):
            assert f" {code} " in text or f"{code}  " in text, (
                f"soak script docstring must document exit code {code}"
            )
