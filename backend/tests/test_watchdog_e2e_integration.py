"""R9 row 2943-2946 (#315) — E2E watchdog integration tests (headless).

Locks the row 2946 sub-bullet verbatim:

  E2E watchdog integration tests (headless,不需真 Discord):
    - Agent 語意空轉 → R2 entropy alert → R9 P2 mapping → L2 ChatOps
      notification(mock) → R1 inject hint → agent resumes
    - PEP 攔截 prod-deploy → R0 HOLD → R9 P2 mapping → L2 ChatOps →
      R1 approve → PEP release → tool executes
    - Agent crash → R4 checkpoint → R8 worktree recreate → R3
      scratchpad reload → agent hot-resumes
    - System OOM → M1 cgroup kill → R9 P1 mapping → L4 PagerDuty(mock)
      + L3 Jira ticket auto-create

These four scenarios are integration-level: each one drives real
modules end-to-end (semantic_entropy → watchdog_events.emit →
notifications._dispatch_external → curl/ChatOps capture; PEP gateway
→ propose/wait round trip; scratchpad save+reload roundtrip; emit
P1 fan-out across all four durable legs) with the side-effecting
edges (curl, chat bridge, decision_engine resolver) replaced by
spies / stubs so the test stays headless and deterministic.

The notification leg is the **same** pattern across all four — that
is by design: row 2935-2942 collapsed the four scenarios onto a
single ``await emit(WatchdogEvent.X, payload)`` choke-point, so an
integration test that exercises that choke-point on each scenario
gives us regression coverage for the whole "event → severity →
tier → channel" chain that R9 ships.

What this row's tests lock that the unit tests in
``test_watchdog_events.py`` / ``test_severity_p{1,2,3}_dispatch.py``
do not:

  * **Cross-module fan-in** — entropy detector → emit → dispatcher
    → ChatOps + Jira fan-out, ChatOps button → agent_hints.inject
    → resume_event fires. A unit test for any one of these does not
    catch a "wrong button id" or "watchdog event taxonomy ignored
    by entropy detector" regression.
  * **PEP HOLD ↔ ChatOps interleave** — pep.evaluate suspends in
    HOLD, the test concurrently emits the P2 watchdog event and
    fires an approve via the wait_for_decision injection point;
    pep returns auto_allow only if the chain didn't deadlock.
  * **Scratchpad save/reload byte-exact roundtrip across a simulated
    crash** — turns scratchpad's "best-effort encryption + atomic
    rename" pair into an assertion: the post-crash reload returns
    the same fields the pre-crash save wrote.
  * **P1 four-leg fan-out** under a realistic OOM payload — verifies
    the four broadcast tiers (PagerDuty / SMS / Jira / Slack) all
    reach their respective transports with the right severity tag /
    label / mention pattern, end-to-end via the watchdog taxonomy
    rather than via a hand-rolled ``notify(level=critical, ...)``.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass

import pytest


# ─────────────────────────────────────────────────────────────────
#  Shared fixtures (mirror test_watchdog_events.py / test_severity_p2_dispatch.py)
# ─────────────────────────────────────────────────────────────────


@dataclass
class _CapturedCurl:
    cmd: tuple[str, ...]
    body: dict | None


class _FakeProc:
    def __init__(self, returncode: int = 0) -> None:
        self.returncode = returncode

    async def communicate(self) -> tuple[bytes, bytes]:
        return b"", b""


@pytest.fixture()
def fake_subprocess(monkeypatch):
    """Replace ``asyncio.create_subprocess_exec`` so all curl-driven
    senders (Slack / Jira / PagerDuty / SMS) run their full code path
    without firing a real HTTP request. ChatOps does NOT go through
    curl — it is captured separately via :func:`captured_chatops`.
    """
    captured: list[_CapturedCurl] = []

    async def _fake_exec(*args, **kwargs):
        body: dict | None = None
        try:
            d_idx = args.index("-d")
            body = json.loads(args[d_idx + 1])
        except (ValueError, IndexError, json.JSONDecodeError):
            body = None
        captured.append(_CapturedCurl(cmd=args, body=body))
        return _FakeProc(returncode=0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
    return captured


@pytest.fixture()
def captured_chatops(monkeypatch):
    """Spy on :func:`backend.chatops_bridge.send_interactive` so the
    severity-driven ChatOps card is asserted on without wiring a real
    Discord/Teams/Line adapter. The spy returns a stub
    ``OutboundMessage``-shaped object so callers that
    ``await out.to_dict()`` don't crash.
    """
    calls: list[dict] = []

    from backend import chatops_bridge as bridge

    class _StubOutboundMessage:
        def __init__(self, channel: str, title: str, body: str,
                     buttons: list, meta: dict) -> None:
            self.id = "cm-stub"
            self.ts = 0.0
            self.channel = channel
            self.title = title
            self.body = body
            self.buttons = buttons
            self.meta = dict(meta or {})

        def to_dict(self) -> dict:
            return {
                "id": self.id, "ts": self.ts, "channel": self.channel,
                "title": self.title, "body": self.body,
                "buttons": [b.__dict__ for b in self.buttons],
                "meta": dict(self.meta),
            }

    async def _fake_send_interactive(channel, message, *, title="OmniSight",
                                     buttons=None, meta=None):
        calls.append({
            "channel": channel,
            "title": title,
            "body": message,
            "buttons": list(buttons or []),
            "meta": dict(meta or {}),
        })
        return _StubOutboundMessage(channel, title, message,
                                    list(buttons or []), meta or {})

    monkeypatch.setattr(bridge, "send_interactive", _fake_send_interactive)
    return calls


@pytest.fixture()
def configured_settings(monkeypatch):
    """Pin every notification env knob so each leg's ``is_configured()``
    gate opens. Mirrors the fixture used by the unit-level dispatcher
    tests (test_severity_p1_dispatch.py et al).
    """
    from backend.config import settings

    monkeypatch.setattr(settings, "notification_slack_webhook",
                        "https://hooks.slack.test/T0/B0/X")
    monkeypatch.setattr(settings, "notification_slack_mention", "U_ONCALL")
    monkeypatch.setattr(settings, "notification_jira_url", "https://jira.test")
    monkeypatch.setattr(settings, "notification_jira_token", "tk")
    monkeypatch.setattr(settings, "notification_jira_project", "OMNI")
    monkeypatch.setattr(settings, "notification_pagerduty_key", "pd-routing-key")
    monkeypatch.setattr(settings, "notification_sms_webhook",
                        "https://sms.gateway.test/send")
    monkeypatch.setattr(settings, "notification_sms_to", "+15551234567")
    monkeypatch.setattr(settings, "notification_max_retries", 1)
    monkeypatch.setattr(settings, "notification_retry_backoff", 0)
    # ChatOps allow-list: empty = dev-mode "everyone allowed" so
    # ``authorize_inject`` doesn't block the inject_hint button click
    # in test 1 (E2E #1).
    monkeypatch.setattr(settings, "chatops_authorized_users", "")
    return settings


@pytest.fixture()
def stub_persistence(monkeypatch):
    """Stub the asyncpg pool + notification persistence helpers so the
    dispatcher path doesn't try to open a real DB connection.
    """
    from backend import db, db_pool

    class _NullConn:
        async def execute(self, *a, **kw):
            return None

        async def fetch(self, *a, **kw):
            return []

        async def fetchrow(self, *a, **kw):
            return None

    class _NullCM:
        async def __aenter__(self):
            return _NullConn()

        async def __aexit__(self, *a):
            return False

    class _NullPool:
        def acquire(self):
            return _NullCM()

    monkeypatch.setattr(db_pool, "get_pool", lambda: _NullPool())

    async def _noop_insert(*a, **kw):
        return None

    async def _noop_update(*a, **kw):
        return None

    monkeypatch.setattr(db, "insert_notification", _noop_insert)
    monkeypatch.setattr(db, "update_notification_dispatch", _noop_update)


@pytest.fixture(autouse=True)
def reset_digest_state(monkeypatch):
    """Reset row 2940's digest buffer + flags between tests so the P3
    L1_LOG_EMAIL leg is observable cleanly. autouse so all four
    scenarios start with an empty buffer regardless of test order.
    """
    from backend import notifications as n
    n._DIGEST_BUFFER.clear()
    monkeypatch.setattr(n, "_DIGEST_OVERFLOW_WARNED", False)
    yield
    n._DIGEST_BUFFER.clear()


@pytest.fixture(autouse=True)
def reset_subsystems():
    """Wipe per-agent state in the integration subsystems we drive
    end-to-end: semantic_entropy monitor, agent_hints blackboard, PEP
    gateway held-registry. Without this an earlier scenario's ghost
    state can leak into a later one (e.g. agent_hints blackboard
    survivor that a P2 ChatOps button click would mistake for the
    current scenario's hint).
    """
    from backend import agent_hints, pep_gateway, semantic_entropy

    semantic_entropy.reset_for_tests()
    agent_hints.reset_for_tests()
    pep_gateway._reset_for_tests()
    yield
    semantic_entropy.reset_for_tests()
    agent_hints.reset_for_tests()
    pep_gateway._reset_for_tests()


# ─────────────────────────────────────────────────────────────────
#  E2E #1 — Agent semantic deadlock → P2 → ChatOps → inject hint
# ─────────────────────────────────────────────────────────────────


class TestSemanticDeadlockE2E:
    """Agent 語意空轉 → R2 entropy alert → R9 P2 mapping → L2 ChatOps
    notification (mock) → R1 inject hint → agent resumes.

    The chain in production:
      1. Agent's ReAct loop pushes outputs into
         ``semantic_entropy.record_output()``.
      2. After enough rephrased-but-identical rounds, the rolling
         pairwise cosine crosses the deadlock threshold → entropy
         monitor returns a measurement with ``verdict == "deadlock"``.
      3. The watchdog's reaction wires the deadlock event into
         ``await emit(WatchdogEvent.P2_COGNITIVE_DEADLOCK, payload)``.
      4. The P2 fan-out fires Jira (with ``blocked`` label) + ChatOps
         (default ack / inject_hint / view_logs button set).
      5. On-call clicks **Inject Hint** in chat → bridge resolves the
         ``inject_hint`` button id → handler calls
         ``agent_hints.inject(agent_id, text, ...)``.
      6. ``agent_hints.inject`` populates the per-agent blackboard
         and **fires the asyncio.Event** the agent's tick loop is
         awaiting on — the agent wakes (hot resume) and consumes the
         hint on its next iteration.

    Test verifies all six steps headless: feeds fixed outputs to the
    entropy monitor (with a reduced threshold so we don't have to
    construct semantically-identical-but-lexically-distinct strings),
    asserts deadlock verdict, drives ``emit``, asserts ChatOps
    captured the right buttons, simulates the inject_hint button by
    binding a custom handler that calls ``agent_hints.inject``,
    asserts the agent's resume event fired and the blackboard slot
    is populated.
    """

    async def test_entropy_deadlock_emits_p2_chatops_inject_hint_resumes(
        self, fake_subprocess, captured_chatops, configured_settings,
        stub_persistence,
    ) -> None:
        from backend import agent_hints, semantic_entropy
        from backend.watchdog_events import WatchdogEvent, emit

        agent_id = "firmware-alpha"
        # Lower the deadlock threshold to 0.30 so the lexical fallback
        # embedder reliably crosses it on a small set of repeated
        # outputs — keeps the test deterministic without depending on
        # a real ML embedder being installed in CI.
        monitor = semantic_entropy.SemanticEntropyMonitor(
            window_size=4, check_every_n=1,
            warn_threshold=0.20, dead_threshold=0.30,
        )

        repeated = "I will fix the failing test now"
        result = None
        for _ in range(4):
            result = monitor.ingest(agent_id, repeated, force_check=True)
        assert result is not None, "monitor should produce a measurement"
        assert result["verdict"] == "deadlock", result

        # Pre-register an asyncio.Event on the running loop so the
        # agent's tick loop has something to await for hot resume.
        # ``agent_hints._get_resume_event`` lazily creates one if
        # none exists; calling it now binds the event to *this*
        # event loop (not a child), which is what the inject path
        # signals later.
        resume_ev = agent_hints.resume_event(agent_id)
        assert not resume_ev.is_set(), \
            "fresh resume event must start cleared"

        # Watchdog reacts to the deadlock verdict by firing the
        # canonical P2 event. In production the entropy detector's
        # broadcast path would call this; the test wires it
        # explicitly so the assertion target stays focused.
        notif = await emit(
            WatchdogEvent.P2_COGNITIVE_DEADLOCK,
            {
                "title": f"agent {agent_id} cognitive deadlock",
                "message": (
                    f"semantic entropy {result['entropy_score']:.2f} "
                    f"≥ {result['threshold_deadlock']:.2f} "
                    f"over last {result['window_size']} outputs"
                ),
                "context": {
                    "agent_id": agent_id,
                    "entropy_score": result["entropy_score"],
                },
            },
        )
        await asyncio.sleep(0)  # let the fire-and-forget chatops task run

        # ── Step 4a: Jira leg fired with row 2939 contract (severity-P2
        # + blocked labels), description prefix tag.
        jira_bodies = [
            c.body for c in fake_subprocess
            if c.body and len(c.cmd) > 4 and "jira.test" in c.cmd[4]
        ]
        assert jira_bodies, fake_subprocess
        labels = jira_bodies[0]["fields"].get("labels", [])
        assert "severity-P2" in labels, labels
        assert "blocked" in labels, labels

        # ── Step 4b: ChatOps leg fired with default 3-button set.
        assert len(captured_chatops) == 1, captured_chatops
        chat = captured_chatops[0]
        assert chat["channel"] == "*", chat
        button_ids = [b.id for b in chat["buttons"]]
        assert button_ids == ["ack", "inject_hint", "view_logs"], button_ids
        assert chat["meta"]["severity"] == "P2", chat
        assert chat["meta"]["notification_id"] == notif.id, chat

        # ── Step 5: simulate the on-call clicking the Inject Hint
        # button. We invoke the button handler directly (the bridge
        # would route ``Inbound(button_id="inject_hint", ...)`` to
        # whatever handler is registered). The handler calls
        # ``agent_hints.inject`` so the contract under test is "click
        # → blackboard populated + resume event fires".
        from backend import chatops_bridge as bridge

        async def _inject_hint_handler(inbound: bridge.Inbound) -> str:
            bridge.authorize_inject(inbound)
            target_aid = inbound.button_value or agent_id
            hint = agent_hints.inject(
                target_aid,
                "Try restarting the failing test runner — the previous "
                "process may still hold a stale lock.",
                author=inbound.author or inbound.user_id or "chatops",
                channel=inbound.channel,
            )
            return f"hint injected ({len(hint.text)} chars)"

        bridge.on_button_click("inject_hint", _inject_hint_handler)
        try:
            inbound = bridge.Inbound(
                kind="button",
                channel="discord",
                author="oncall@example.com",
                user_id="U_ONCALL",
                button_id="inject_hint",
                button_value=agent_id,
            )
            reply = await _inject_hint_handler(inbound)
            assert "hint injected" in reply, reply
        finally:
            # Restore the slot so other tests aren't affected by this
            # one's button-handler registration. ``on_button_click``
            # overwrites; setting the registry entry to a no-op
            # restores neutral behaviour without depending on the
            # bridge's _reset_for_tests internals.
            async def _noop(_inbound: bridge.Inbound) -> str:
                return ""
            bridge.on_button_click("inject_hint", _noop)

        # ── Step 6: agent_hints contract — slot populated, resume
        # event fired (agent's await loop wakes), consume() returns
        # the same hint and clears the slot for the next round.
        pending = agent_hints.peek(agent_id)
        assert pending is not None
        assert "stale lock" in pending.text
        assert pending.author == "oncall@example.com"
        assert resume_ev.is_set(), \
            "agent resume event must fire on inject (hot resume contract)"

        consumed = agent_hints.consume(agent_id)
        assert consumed is not None
        assert consumed.text == pending.text
        # consume() clears the resume event so the next await loop
        # can re-await it on the following round.
        assert not resume_ev.is_set()
        assert agent_hints.peek(agent_id) is None


# ─────────────────────────────────────────────────────────────────
#  E2E #2 — PEP intercept prod-deploy → P2 → ChatOps → approve →
#                                           PEP release → tool runs
# ─────────────────────────────────────────────────────────────────


class TestPepInterceptProdDeployE2E:
    """PEP 攔截 prod-deploy → R0 HOLD → R9 P2 mapping → L2 ChatOps
    → R1 approve → PEP release → tool executes.

    The chain in production:
      1. Agent's tool_executor calls ``pep.evaluate(tool="run_bash",
         arguments={"command": "deploy.sh prod"})``.
      2. PEP classifies the command as production-scope → action =
         hold; raises a Decision Engine proposal via ``propose_fn``;
         awaits ``wait_for_decision`` until an operator resolves.
      3. The watchdog surfaces the HOLD as a P2 cognitive-impact
         event (``WatchdogEvent.P2_COGNITIVE_DEADLOCK`` is the
         closest fit — operator attention-required, not
         system-down). ChatOps fan-out shows the standard P2 card.
      4. On-call approves via the chat surface (or via the
         existing PEP router's POST /pep/decision/{id}). The
         resolution flips ``wait_for_decision`` to "approved".
      5. PEP wakes from the await, returns ``PepDecision`` with
         action=auto_allow + decision_id preserved. The agent's
         tool_executor proceeds with the original command — i.e. the
         tool "executes" (modeled here as the post-eval action
         transitioning to auto_allow).

    The test runs ``pep.evaluate`` as a background asyncio task so
    we can observe the HELD state mid-flight, fire the watchdog P2
    event (concurrently with the held PEP), then release the wait
    via an asyncio.Event the test owns.
    """

    async def test_pep_hold_p2_chatops_approve_release_executes(
        self, fake_subprocess, captured_chatops, configured_settings,
        stub_persistence,
    ) -> None:
        from backend import pep_gateway as pep
        from backend.watchdog_events import WatchdogEvent, emit

        # The fake propose_fn captures the proposal so we can read
        # back the decision_id; the wait_for_decision below uses it.
        captured_props: list[dict] = []
        release_event = asyncio.Event()
        outcomes: dict[str, str] = {}

        def _propose(*, kind, title, detail="", options=None,
                     default_option_id=None, severity=None,
                     timeout_s=None, source=None):
            from dataclasses import dataclass

            @dataclass
            class _Prop:
                id: str
                kind: str
                title: str
                detail: str
                options: list
                default_option_id: str | None
                severity: object
                source: dict

            pid = f"fake-dec-{len(captured_props) + 1}"
            prop = _Prop(
                id=pid, kind=kind, title=title, detail=detail,
                options=options or [],
                default_option_id=default_option_id,
                severity=severity, source=dict(source or {}),
            )
            captured_props.append({
                "id": pid, "kind": kind, "title": title,
                "source": prop.source,
            })
            return prop

        async def _wait_blocking(decision_id: str, timeout_s: float) -> str:
            """Block until the test releases the event — models the
            real waiter that polls decision_engine.get(). Returns the
            outcome the test pre-set in ``outcomes``.
            """
            try:
                await asyncio.wait_for(release_event.wait(), timeout=timeout_s)
            except asyncio.TimeoutError:
                return "timeout"
            return outcomes.get(decision_id, "rejected")

        # ── Step 1: kick off PEP evaluate as a background task; the
        # call will reach HELD and block on ``release_event``.
        eval_task = asyncio.create_task(pep.evaluate(
            tool="run_bash",
            arguments={"command": "deploy.sh prod"},
            agent_id="agent-deployer",
            tier="t3",
            propose_fn=_propose,
            wait_for_decision=_wait_blocking,
            hold_timeout_s=5.0,
        ))

        # Yield until propose runs + the HELD registry is populated.
        # Several scheduler turns may be needed because evaluate
        # awaits several internal coroutines before reaching wait.
        for _ in range(50):
            await asyncio.sleep(0)
            if pep.held_snapshot():
                break
        held = pep.held_snapshot()
        assert held, "PEP should have registered a HELD entry"
        assert len(captured_props) == 1, captured_props
        assert captured_props[0]["kind"] == "pep_tool_intercept"
        de_id = captured_props[0]["id"]

        # ── Step 2: watchdog surfaces the HOLD as a P2 event. In
        # production this is wired by whatever watchdog observer
        # subscribes to the ``pep.held`` SSE channel; the test fires
        # it directly.
        held_entry = held[0]
        await emit(
            WatchdogEvent.P2_COGNITIVE_DEADLOCK,
            {
                "title": f"PEP HOLD · {held_entry['tool']}",
                "message": (
                    f"Agent {held_entry['agent_id']} held on "
                    f"{held_entry['tool']!r} ({held_entry['rule']}) — "
                    "operator approval required"
                ),
                "context": {
                    "pep_id": held_entry["id"],
                    "decision_id": de_id,
                },
            },
        )
        await asyncio.sleep(0)

        # ── Step 3: P2 ChatOps card was emitted with the standard 3-
        # button set; Jira ticket got the row 2939 ``blocked`` label.
        assert len(captured_chatops) == 1, captured_chatops
        chat = captured_chatops[0]
        assert chat["channel"] == "*", chat
        assert chat["meta"]["severity"] == "P2"
        assert [b.id for b in chat["buttons"]] == [
            "ack", "inject_hint", "view_logs",
        ]
        jira_bodies = [
            c.body for c in fake_subprocess
            if c.body and len(c.cmd) > 4 and "jira.test" in c.cmd[4]
        ]
        assert jira_bodies, fake_subprocess
        labels = jira_bodies[0]["fields"].get("labels", [])
        assert "severity-P2" in labels and "blocked" in labels, labels
        # P1 broadcast tiers must NOT have fired — P2 ladder isn't
        # additive with PagerDuty / SMS.
        all_urls = [c.cmd[4] for c in fake_subprocess if len(c.cmd) > 4]
        assert not any("events.pagerduty.com" in u for u in all_urls), all_urls
        assert not any("sms.gateway.test" in u for u in all_urls), all_urls

        # ── Step 4: operator approves. In production the ChatOps
        # ``pep_approve`` button handler (or POST /pep/decision/{id})
        # writes "approved" into decision_engine; here we model that
        # by setting the outcome and releasing the wait.
        outcomes[de_id] = "approved"
        release_event.set()

        # ── Step 5: PEP returns auto_allow; tool may execute.
        result = await asyncio.wait_for(eval_task, timeout=5.0)
        assert result.action is pep.PepAction.auto_allow, result
        assert result.decision_id == de_id
        assert pep.held_snapshot() == [], \
            "HELD registry must be cleaned up on resolution"


# ─────────────────────────────────────────────────────────────────
#  E2E #3 — Crash → checkpoint → worktree recreate → scratchpad
#                                                  reload → resume
# ─────────────────────────────────────────────────────────────────


class TestCrashRecoveryE2E:
    """Agent crash → R4 checkpoint → R8 worktree recreate → R3
    scratchpad reload → agent hot-resumes.

    The chain in production:
      1. Agent saves a checkpoint mid-task to scratchpad (R3 #309).
         Atomic write goes through ``scratchpad.save()`` — markdown
         body + json meta both replace-once, so a torn write can't
         leave the agent unable to resume.
      2. Agent process crashes (SIGKILL / OOM / panic). Worktree
         retry path (R8 #314) calls ``workspace.discard_and_recreate``
         to roll back the working tree to the immutable anchor SHA.
      3. Replacement agent boots, calls ``scratchpad.reload_latest``
         to rehydrate the in-memory state. The agent then continues
         the ReAct loop from where the saved state said it was.
      4. Watchdog observes the recovery and fires
         ``WatchdogEvent.P3_AUTO_RECOVERY`` so the operator's daily
         digest carries the breadcrumb (no PagerDuty noise — P3 is
         informational).

    The test exercises steps 1, 3, and 4 directly; step 2 (worktree
    recreate) is verified by importing
    ``workspace.discard_and_recreate`` and confirming the API
    surface exists. Real ``git worktree`` execution is out of scope
    for an integration test — that's covered in
    ``test_workspace_r8_*.py`` against tmp git repos.
    """

    async def test_save_then_simulated_crash_then_reload_then_p3_emit(
        self, fake_subprocess, captured_chatops, configured_settings,
        stub_persistence, monkeypatch, tmp_path,
    ) -> None:
        from backend import notifications as n
        from backend import scratchpad
        from backend import workspace
        from backend.watchdog_events import WatchdogEvent, emit

        # Point scratchpad at an isolated tmp dir so the test doesn't
        # pollute (or read from) the real data/agents/ directory.
        monkeypatch.setenv("OMNISIGHT_SCRATCHPAD_ROOT", str(tmp_path))
        scratchpad.reset_for_tests()

        agent_id = "firmware-alpha-recovery"

        # ── Step 1: agent saves a checkpoint of its working state.
        pre_crash = scratchpad.ScratchpadState(
            agent_id=agent_id,
            current_task="Implement PWM dimming for IR LED bank",
            progress="3/5 PWM channels migrated; channels 4-5 still on legacy GPIO",
            blockers="Vendor SDK header pending — opened Linear ticket VENDOR-217",
            next_steps="Refactor channel 4 to call new PWM API once header lands",
            context_summary="Working in branch agent/firmware-alpha-recovery/task-pwm",
            turn=7,
            total_turns=12,
            subtask="pwm-dimming",
        )
        save_result = scratchpad.save(
            pre_crash, trigger="checkpoint",
            task_id="task-pwm-dimming", emit=False,
        )
        assert save_result.path.exists()
        assert save_result.sections_count == 5

        # ── Step 2: confirm worktree retry primitive is wired up
        # (the actual ``git worktree add`` is exercised in
        # test_workspace_r8_*.py against real tmp git repos — out of
        # scope for an integration assertion that covers the
        # notification chain).
        assert callable(getattr(workspace, "discard_and_recreate", None))

        # ── Simulated crash: drop in-memory state. The test next
        # exercises the cold-start replacement-agent code path.
        scratchpad.reset_for_tests()

        # ── Step 3: replacement agent reloads scratchpad.
        post_crash = scratchpad.reload_latest(agent_id)
        assert post_crash is not None, "scratchpad must round-trip"
        assert post_crash.agent_id == agent_id
        assert post_crash.current_task == pre_crash.current_task
        assert post_crash.progress == pre_crash.progress
        assert post_crash.blockers == pre_crash.blockers
        assert post_crash.next_steps == pre_crash.next_steps
        assert post_crash.context_summary == pre_crash.context_summary
        assert post_crash.turn == pre_crash.turn
        assert post_crash.subtask == pre_crash.subtask

        # ── Step 4: watchdog surfaces the auto-recovery as a P3
        # event for the daily digest. P3 routes to L1_LOG_EMAIL
        # only — no curl, no ChatOps card.
        before_buffer = len(n._DIGEST_BUFFER)
        notif = await emit(
            WatchdogEvent.P3_AUTO_RECOVERY,
            {
                "title": f"agent {agent_id} hot-resumed from scratchpad",
                "message": (
                    f"checkpoint reload succeeded (turn {post_crash.turn}) — "
                    f"current task: {post_crash.current_task}"
                ),
                "context": {
                    "agent_id": agent_id,
                    "subtask": post_crash.subtask,
                    "turn": post_crash.turn,
                },
            },
        )
        await asyncio.sleep(0)

        # P3 contract: digest buffer received the event, no external
        # transport fired.
        assert len(n._DIGEST_BUFFER) == before_buffer + 1, \
            "P3 must land in the daily digest buffer"
        assert notif.severity is not None and notif.severity.value == "P3"
        assert fake_subprocess == [], \
            "P3 path must not invoke curl (informational only)"
        assert captured_chatops == [], \
            "P3 must not surface a ChatOps card"


# ─────────────────────────────────────────────────────────────────
#  E2E #4 — System OOM → P1 → PagerDuty + Jira + SMS + Slack
# ─────────────────────────────────────────────────────────────────


class TestSystemOomP1E2E:
    """System OOM → M1 cgroup kill → R9 P1 mapping → L4 PagerDuty
    (mock) + L3 Jira ticket auto-create.

    The chain in production:
      1. Linux kernel oom-killer fires inside an agent's cgroup
         (memory.max breached). cgroup_verify reports the OOM kill
         to the watchdog observer.
      2. Watchdog fires
         ``WatchdogEvent.P1_SYSTEM_DOWN`` with the cgroup details +
         RSS spike numbers.
      3. P1 fan-out routes to all four broadcast tiers:
           * **L4 PagerDuty** — page the on-call.
           * **L4 SMS** — backup paging if PagerDuty itself is down.
           * **L3 Jira** — durable ticket with severity-P1 label,
             priority=Highest, issuetype=Bug for post-mortem.
           * **L2 Slack** — broadcast with both `<!channel>` and
             `@everyone` (cross-platform Discord-compat) so the
             whole engineering channel sees the alert.

    The test fires the P1 watchdog event with an OOM-shaped payload
    and asserts the four legs hit their respective transports with
    the correct severity tags + broadcast tokens.
    """

    async def test_p1_oom_emits_to_all_four_durable_legs(
        self, fake_subprocess, captured_chatops, configured_settings,
        stub_persistence,
    ) -> None:
        from backend import cgroup_verify
        from backend.watchdog_events import WatchdogEvent, emit

        # Confirm the cgroup_verify module surface exists — the M1
        # observer that translates a cgroup OOM into a P1 watchdog
        # event lives in the same module. Real cgroup paths only
        # exist on Linux containers; the API contract is what
        # matters here for the integration test.
        assert callable(getattr(cgroup_verify, "read_cpu_weight", None))
        assert callable(getattr(cgroup_verify, "verify_weight_ratio", None))

        # ── Step 2 + 3: emit the P1 event with an OOM-shaped payload.
        notif = await emit(
            WatchdogEvent.P1_SYSTEM_DOWN,
            {
                "title": "kernel oom-killer fired in cgroup backend-a",
                "message": (
                    "RSS 7.8 GiB ≥ memory.max 6.0 GiB — agent process "
                    "killed by oom-reaper; container restart pending"
                ),
                "context": {
                    "cgroup": "/sys/fs/cgroup/system.slice/backend-a.service",
                    "killed_pid": 1187,
                    "rss_bytes": 8369999872,
                    "memory_max_bytes": 6442450944,
                },
            },
        )
        await asyncio.sleep(0)

        # ── L4 PagerDuty — Events API v2 envelope + custom_details
        # carries the row 2936 severity tag.
        pd_calls = [
            c for c in fake_subprocess
            if c.body and len(c.cmd) > 4 and "events.pagerduty.com" in c.cmd[4]
        ]
        assert pd_calls, fake_subprocess
        pd_body = pd_calls[0].body
        # The Events API v2 envelope nests our notification under
        # ``payload`` — both summary prefix and custom_details must
        # carry the P1 tag (row 2936).
        pd_payload = pd_body.get("payload", {})
        assert pd_payload.get("severity") == "critical", pd_body
        assert "[P1]" in pd_payload.get("summary", ""), pd_body
        assert pd_payload.get("custom_details", {}).get(
            "omnisight_severity") == "P1", pd_body

        # ── L4 SMS — gateway POST envelope carries the to/message
        # fields and the severity tag (row 2936).
        sms_calls = [
            c for c in fake_subprocess
            if c.body and len(c.cmd) > 4 and "sms.gateway.test" in c.cmd[4]
        ]
        assert sms_calls, fake_subprocess
        sms_body = sms_calls[0].body
        assert sms_body.get("to") == "+15551234567", sms_body
        assert sms_body.get("severity") == "P1", sms_body
        # Body is auto-truncated to 160 chars but must carry the
        # title prefix; we assert containment, not equality.
        assert "oom-killer" in sms_body.get("message", ""), sms_body

        # ── L3 Jira — ticket with severity-P1 label, priority=Highest,
        # issuetype=Bug, description prefix.
        jira_calls = [
            c for c in fake_subprocess
            if c.body and len(c.cmd) > 4 and "jira.test" in c.cmd[4]
        ]
        assert jira_calls, fake_subprocess
        fields = jira_calls[0].body["fields"]
        assert "severity-P1" in fields.get("labels", []), fields
        # P2's ``blocked`` label must NOT appear on a P1 (label
        # semantics are disjoint per row 2939).
        assert "blocked" not in fields.get("labels", []), fields
        assert fields.get("priority", {}).get("name") == "Highest", fields
        assert fields.get("issuetype", {}).get("name") == "Bug", fields
        assert "[severity:P1]" in fields.get("description", ""), fields

        # ── L2 Slack — broadcast with both Slack `<!channel>` and
        # Discord `@everyone` (row 2936's cross-platform mention).
        slack_calls = [
            c for c in fake_subprocess
            if c.body and len(c.cmd) > 4 and "hooks.slack.test" in c.cmd[4]
        ]
        assert slack_calls, fake_subprocess
        text = slack_calls[0].body["text"]
        assert "<!channel>" in text, text
        assert "@everyone" in text, text
        assert "[severity:P1]" in text, text

        # ── ChatOps must NOT have fired — P1 is broadcast-only, the
        # interactive ChatOps card surface is reserved for P2
        # (operator-attention-required) per row 2939's tier disjoint
        # invariant.
        assert captured_chatops == [], \
            "P1 must not surface an interactive ChatOps card"

        # Notification model carries P1 severity tag end-to-end.
        assert notif.severity is not None and notif.severity.value == "P1"
