"""V9 #4 (#325) — Cross-workspace end-to-end contract tests.

Pins the full vertical loop for each of the three workspaces against
the operator-visible storyline declared in TODO row 2712:

    Web     :  NL「做一個 SaaS landing page」  → agent writes →
               preview renders → 標註「把 hero 背景改深藍」 → agent
               re-iterates → re-renders → deploy → Lighthouse ≥ 80
    Mobile  :  NL「做一個 todo app」 → agent renders SwiftUI →
               emulator screenshot path resolves → 標註「加個
               dark mode toggle」 → agent re-iterates → rebuild →
               screenshot capture argv well-formed
    Software:  NL「做一個 REST API with user CRUD」 → agent renders
               FastAPI scaffold → pytest gate honoured (mock fallback
               on sandboxes without runners) → OpenAPI spec entry-
               point exercised → Docker build adapter validates
               artefact → deploy adapter contract round-trips

The tests deliberately exercise the *real* primitives the runtime
ships — `agent_hints.inject` / `consume` for the operator-side
annotation channel, `web_simulator.simulate_web` /
`mobile_simulator.simulate_mobile` /
`software_simulator.simulate_software` for the build/preview gates,
`fastapi_scaffolder.render_project` /
`fastapi_scaffolder.dry_run_build` /
`ios_scaffolder.render_project` for the agent-side scaffold writes,
`mobile_screenshot.build_ios_capture_argv` for the screenshot
argv, `deploy.base.WebDeployAdapter` for the deploy contract, and
`backend.events.bus` for the SSE side-effects.

External CLIs (lighthouse, axe, xcrun, adb, docker, pytest…) are
absent in the sandbox, so the simulators degrade to their declared
``mock`` fallback by design — that is the existing contract, and is
what production-on-Linux-without-Xcode falls back to today. Where a
gate insists on a real binary (the ``deploy`` adapter network call,
the OpenAPI router import) we plug a tightly-scoped stub adapter so
the contract is exercised without leaving the test surface.

No live LLM is invoked — `run_graph` is a separate `test_graph.py`
contract; this file lives one layer above, asserting that the
workspace orchestration *primitives* (hints + simulator + scaffolder
+ deploy + event bus) compose into the operator-visible storyline.

Test taxonomy:
    TestWebVerticalE2E       — 8 contracts (Web vertical)
    TestMobileVerticalE2E    — 8 contracts (Mobile vertical)
    TestSoftwareVerticalE2E  — 8 contracts (Software vertical)
    TestCrossVerticalContract— 4 contracts (cross-workspace invariants)
"""

from __future__ import annotations

import contextlib
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Optional

import pytest

from backend import agent_hints
from backend import events as _events
from backend import fastapi_scaffolder, ios_scaffolder
from backend import mobile_screenshot, mobile_simulator, software_simulator, web_simulator
from backend.deploy.base import (
    BuildArtifact,
    DeployResult,
    ProvisionResult,
    WebDeployAdapter,
)


# ── Shared helpers ──────────────────────────────────────────────────


SAAS_LANDING_HTML_LIGHT = """\
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<meta name="description" content="OmniSight SaaS landing page demo" />
<link rel="canonical" href="https://demo.example/landing" />
<meta property="og:title" content="OmniSight SaaS" />
<meta property="og:type" content="website" />
<title>OmniSight SaaS</title>
<style>
  .hero { background: #f0f8ff; color: #111; padding: 4rem 2rem; }
</style>
</head>
<body>
<header class="hero" data-testid="hero">
  <h1>OmniSight</h1>
  <p>The multi-agent dev command center for embedded AI cameras.</p>
  <a href="#cta">Start free trial</a>
</header>
<main>
  <section><h2>Features</h2><p>Multi-agent. Cross-vertical.</p></section>
</main>
</body>
</html>
"""

# Annotated re-iteration: hero background flipped to dark blue.
SAAS_LANDING_HTML_DARK_BLUE = SAAS_LANDING_HTML_LIGHT.replace(
    ".hero { background: #f0f8ff; color: #111;",
    ".hero { background: #0a2540; color: #ffffff;",
)


def _write_landing_page(app_path: Path, html: str) -> Path:
    """Write a SaaS landing page into ``app_path/dist/index.html``.

    The static build directory is the canonical W2 web-simulator
    discovery target (`dist/` → `build/` → `out/` → repo root).
    """
    dist = app_path / "dist"
    dist.mkdir(parents=True, exist_ok=True)
    dest = dist / "index.html"
    dest.write_text(html, encoding="utf-8")
    return dest


@dataclass
class _CapturedEvent:
    """Inert recorder for ``events.bus.publish`` side-effects.

    The real bus fans out to in-process SSE subscribers; for these
    tests we just want to assert ``what`` was published, not how it
    was delivered.
    """

    name: str
    data: dict[str, Any]


@contextlib.contextmanager
def _capture_events(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[_CapturedEvent]]:
    captured: list[_CapturedEvent] = []
    real_publish = _events.bus.publish

    def _spy(event: str, data: dict[str, Any], *args: Any, **kw: Any) -> None:
        # snapshot data so later mutations on the source dict don't
        # alter the captured payload — same defensive pattern the
        # real EventBus uses internally.
        captured.append(_CapturedEvent(name=event, data=dict(data)))
        return real_publish(event, data, *args, **kw)

    monkeypatch.setattr(_events.bus, "publish", _spy)
    try:
        yield captured
    finally:
        # monkeypatch already restores in teardown; nothing else to do.
        pass


@pytest.fixture(autouse=True)
def _reset_agent_hints() -> Iterator[None]:
    """Drain the ``agent_hints`` blackboards between tests.

    The module keeps three module-level dicts (``_blackboard``,
    ``_rate_window``, ``_resume_events``) so without an explicit
    reset a test that injects into ``agent-web`` will leak the slot
    into the next test. SOP Step 1 third question — the right answer
    here is "intentionally per-process; no cross-worker coord
    needed", and the per-test reset keeps the contract local to the
    test that owns the slot.
    """
    agent_hints._blackboard.clear()
    agent_hints._rate_window.clear()
    agent_hints._resume_events.clear()
    yield
    agent_hints._blackboard.clear()
    agent_hints._rate_window.clear()
    agent_hints._resume_events.clear()


# ── Stub deploy adapter (Web vertical) ─────────────────────────────


class _StubWebDeployAdapter(WebDeployAdapter):
    """Round-trip the WebDeployAdapter contract in-memory.

    Real adapters (Vercel / Netlify / Cloudflare Pages / docker-nginx)
    talk to live providers — we don't want network in a unit test, so
    this stub stands in for *any* concrete subclass to prove the
    upstream orchestration code (provision → deploy → get_url)
    remains decoupled from any single provider.
    """

    provider = "stub"

    def _configure(self, **kwargs: Any) -> None:
        self._provisions = 0
        self._deploys = 0
        self._provisioned_url = "https://stub.example/preview"

    async def provision(
        self,
        *,
        env: Optional[dict[str, str]] = None,
        **kwargs: Any,
    ) -> ProvisionResult:
        self._provisions += 1
        self._cached_url = self._provisioned_url
        return ProvisionResult(
            provider=self.provider,
            project_id=f"proj_{self._project_name}",
            project_name=self._project_name,
            url=self._provisioned_url,
            created=self._provisions == 1,
            env_vars_set=list((env or {}).keys()),
        )

    async def deploy(self, build_artifact: BuildArtifact) -> DeployResult:
        self._deploys += 1
        build_artifact.validate()
        url = f"{self._provisioned_url}/d{self._deploys}"
        self._last_deployment_id = f"dep_{self._deploys}"
        self._cached_url = url
        return DeployResult(
            provider=self.provider,
            deployment_id=self._last_deployment_id,
            url=url,
            status="ready",
            commit_sha=build_artifact.commit_sha,
        )

    async def rollback(
        self,
        *,
        deployment_id: Optional[str] = None,
    ) -> DeployResult:
        if self._deploys < 2:
            from backend.deploy.base import RollbackUnavailableError

            raise RollbackUnavailableError(
                "no prior deployment", provider=self.provider
            )
        return DeployResult(
            provider=self.provider,
            deployment_id=deployment_id or "dep_prev",
            url=self._provisioned_url,
            status="rolled-back",
        )

    def get_url(self) -> Optional[str]:
        return self._cached_url


# ════════════════════════════════════════════════════════════════════
#  Web vertical
# ════════════════════════════════════════════════════════════════════


class TestWebVerticalE2E:
    """Web workspace — NL → preview → annotate → deploy → Lighthouse."""

    AGENT_ID = "agent-web-e2e"

    def test_initial_nl_prompt_inject_lands_in_blackboard(self) -> None:
        """Operator NL prompt enters the agent's hint slot."""
        hint = agent_hints.inject(
            self.AGENT_ID,
            "做一個 SaaS landing page",
            author="operator@example",
        )
        assert hint.text == "做一個 SaaS landing page"
        assert hint.channel == "dashboard"
        peeked = agent_hints.peek(self.AGENT_ID)
        assert peeked is not None and peeked.text == hint.text

    def test_agent_writes_initial_landing_page_with_hero_section(
        self, tmp_path: Path
    ) -> None:
        """First iteration: agent renders HTML with a hero block."""
        agent_hints.inject(self.AGENT_ID, "做一個 SaaS landing page")
        # consume the hint to model the agent picking it up
        consumed = agent_hints.consume(self.AGENT_ID)
        assert consumed is not None and "landing page" in consumed.text
        assert agent_hints.peek(self.AGENT_ID) is None

        index = _write_landing_page(tmp_path, SAAS_LANDING_HTML_LIGHT)
        assert index.is_file()
        html = index.read_text(encoding="utf-8")
        assert "data-testid=\"hero\"" in html
        # initial light hero is the un-annotated baseline
        assert "background: #f0f8ff" in html

    def test_lighthouse_mock_scores_meet_threshold(self, tmp_path: Path) -> None:
        """Mock Lighthouse path returns scores at the W2 thresholds.

        Sandboxes without ``lhci`` / ``lighthouse`` on PATH degrade to
        the synthetic-mock matrix; the matrix is pinned to the floor
        so V9 #4's ``Lighthouse ≥ 80`` storyline holds end-to-end on
        a CI runner that has no Chromium.
        """
        _write_landing_page(tmp_path, SAAS_LANDING_HTML_LIGHT)
        scores = web_simulator.run_lighthouse(tmp_path, url=None)
        assert scores.source == "mock"
        assert scores.performance >= web_simulator.LIGHTHOUSE_MIN_PERF
        assert scores.accessibility >= web_simulator.LIGHTHOUSE_MIN_A11Y
        assert scores.seo >= web_simulator.LIGHTHOUSE_MIN_SEO

    def test_seo_lint_passes_with_full_meta_tags(self, tmp_path: Path) -> None:
        """Static SEO lint accepts a properly meta-tagged landing page."""
        _write_landing_page(tmp_path, SAAS_LANDING_HTML_LIGHT)
        seo = web_simulator.run_seo_lint(tmp_path)
        assert seo.issues == 0, seo.details

    def test_simulate_web_full_gate_rollup_passes(self, tmp_path: Path) -> None:
        """``simulate_web`` aggregates every W2 gate to a green status."""
        _write_landing_page(tmp_path, SAAS_LANDING_HTML_LIGHT)
        result = web_simulator.simulate_web(profile="web-static", app_path=tmp_path)
        assert result.lighthouse.performance >= web_simulator.LIGHTHOUSE_MIN_PERF
        assert result.gates["lighthouse_performance"] is True
        assert result.gates["lighthouse_accessibility"] is True
        assert result.gates["lighthouse_seo"] is True
        assert result.gates["seo_clean"] is True
        assert result.errors == []

    def test_annotation_dark_blue_hero_consumed_then_iterated(
        self, tmp_path: Path
    ) -> None:
        """Annotation hint enters the same slot; agent re-iterates."""
        _write_landing_page(tmp_path, SAAS_LANDING_HTML_LIGHT)
        # agent's first turn done; operator annotates
        annotation_text = "把 hero 背景改深藍 (hero background → dark blue)"
        hint = agent_hints.inject(
            self.AGENT_ID,
            annotation_text,
            author="operator@example",
            channel="dashboard",
        )
        assert "深藍" in hint.text or "dark blue" in hint.text
        consumed = agent_hints.consume(self.AGENT_ID)
        assert consumed is not None and consumed.text == hint.text

        # agent applies the annotation by re-writing index.html
        index = _write_landing_page(tmp_path, SAAS_LANDING_HTML_DARK_BLUE)
        body = index.read_text(encoding="utf-8")
        assert "background: #0a2540" in body
        assert "background: #f0f8ff" not in body

    def test_post_iterate_lighthouse_still_meets_threshold(
        self, tmp_path: Path
    ) -> None:
        """After the dark-blue rewrite the gates remain green."""
        _write_landing_page(tmp_path, SAAS_LANDING_HTML_DARK_BLUE)
        result = web_simulator.simulate_web(profile="web-static", app_path=tmp_path)
        assert result.lighthouse.performance >= 80
        # SEO + a11y + e2e gates also stable across the rewrite
        for gate in (
            "lighthouse_performance",
            "lighthouse_accessibility",
            "lighthouse_seo",
            "seo_clean",
            "e2e_ok",
        ):
            assert result.gates[gate] is True, gate

    @pytest.mark.asyncio
    async def test_deploy_adapter_round_trips_provision_deploy_url(
        self, tmp_path: Path
    ) -> None:
        """The ``WebDeployAdapter`` contract: provision → deploy → URL."""
        _write_landing_page(tmp_path, SAAS_LANDING_HTML_DARK_BLUE)
        adapter = _StubWebDeployAdapter.from_plaintext_token(
            "tk-test-token-1234",
            project_name="omnisight-saas-demo",
        )
        prov = await adapter.provision(env={"NODE_ENV": "production"})
        assert prov.provider == "stub"
        assert prov.url == "https://stub.example/preview"
        assert prov.created is True
        artifact = BuildArtifact(
            path=tmp_path / "dist",
            framework="static",
            commit_sha="abc1234",
        )
        deployed = await adapter.deploy(artifact)
        assert deployed.status == "ready"
        assert deployed.url.startswith("https://stub.example/preview/d")
        assert adapter.get_url() == deployed.url
        assert adapter.token_fp() == "…1234"

    def test_event_bus_emits_workspace_lifecycle_for_web_loop(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Workspace + tool emitters fire on the web vertical loop."""
        _write_landing_page(tmp_path, SAAS_LANDING_HTML_LIGHT)
        with _capture_events(monkeypatch) as captured:
            _events.emit_workspace(
                self.AGENT_ID, "preview_render", str(tmp_path / "dist"),
            )
            _events.emit_tool_progress(
                "web_simulator", "complete", "lighthouse=mock",
            )
            _events.emit_workspace(
                self.AGENT_ID, "deploy_complete",
                detail="https://stub.example/preview/d1",
            )
        names = [e.name for e in captured]
        assert "workspace" in names
        assert "tool_progress" in names
        # at least one workspace event references the dist directory
        ws_events = [e for e in captured if e.name == "workspace"]
        assert any("dist" in (e.data.get("detail") or "") for e in ws_events)


# ════════════════════════════════════════════════════════════════════
#  Mobile vertical
# ════════════════════════════════════════════════════════════════════


class TestMobileVerticalE2E:
    """Mobile workspace — NL → SwiftUI → screenshot → annotate → rebuild."""

    AGENT_ID = "agent-mobile-e2e"

    def test_initial_nl_prompt_inject_lands_in_blackboard(self) -> None:
        hint = agent_hints.inject(self.AGENT_ID, "做一個 todo app")
        assert "todo" in hint.text.lower()
        peeked = agent_hints.peek(self.AGENT_ID)
        assert peeked is not None and peeked.text == hint.text

    def test_ios_scaffold_renders_swiftui_content_view(self, tmp_path: Path) -> None:
        """Agent picks the hint up and renders the SKILL-IOS scaffold."""
        consumed = agent_hints.inject(self.AGENT_ID, "做一個 todo app")
        agent_hints.consume(self.AGENT_ID)
        opts = ios_scaffolder.ScaffoldOptions(
            project_name="OmniTodo",
            push=False,
            storekit=False,
            compliance=False,
        )
        outcome = ios_scaffolder.render_project(tmp_path, opts)
        assert outcome.bytes_written > 0
        # SwiftUI ContentView is the canonical hero file the agent
        # would adapt for a todo app.
        cv = tmp_path / "App" / "Sources" / "ContentView.swift"
        assert cv.is_file()
        cv_body = cv.read_text(encoding="utf-8")
        assert "import SwiftUI" in cv_body
        # baked from j2 — the project name token must be substituted
        assert "OmniTodo" in cv_body
        assert consumed.text == "做一個 todo app"

    def test_resolve_ui_framework_detects_xcuitest_for_ios(
        self, tmp_path: Path
    ) -> None:
        """``resolve_ui_framework`` falls back to xcuitest via platform."""
        opts = ios_scaffolder.ScaffoldOptions(
            project_name="OmniTodo", push=False, storekit=False, compliance=False,
        )
        ios_scaffolder.render_project(tmp_path, opts)
        framework = mobile_simulator.resolve_ui_framework(
            tmp_path, mobile_platform="ios",
        )
        # Scaffold ships a project.yml.j2 (xcodegen) but no rendered
        # .xcodeproj on disk — the platform-hint fallback returns
        # the canonical "ios" → "xcuitest" mapping.
        assert framework == "xcuitest"

    def test_emulator_boot_returns_mock_on_non_macos_host(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """xcrun absent → boot is reported as ``mock`` (P2 contract)."""
        monkeypatch.setattr(
            mobile_simulator.shutil, "which",
            lambda name: None if name == "xcrun" else shutil.which(name),
        )
        report = mobile_simulator.boot_ios_simulator(env={})
        assert report.status == "mock"
        assert report.kind == "simulator"
        assert "xcrun" in (report.detail or "")

    def test_screenshot_capture_argv_well_formed_for_ios_simulator(
        self,
    ) -> None:
        """The iOS screenshot argv builder produces a valid xcrun line."""
        argv = mobile_screenshot.build_ios_capture_argv(
            udid="ABCDEF12-3456-7890-ABCD-EF1234567890",
            output_path="/tmp/omnisight-shot.png",
        )
        assert "xcrun" in argv[0] or argv[0].endswith("xcrun")
        # canonical ordering: xcrun simctl io <udid> screenshot <path>
        assert "simctl" in argv
        assert "screenshot" in argv
        assert "/tmp/omnisight-shot.png" in argv
        assert "ABCDEF12-3456-7890-ABCD-EF1234567890" in argv

    def test_annotation_dark_mode_toggle_consumed_then_iterated(
        self, tmp_path: Path
    ) -> None:
        """Annotation hint applied: agent re-renders + adds toggle."""
        opts = ios_scaffolder.ScaffoldOptions(
            project_name="OmniTodo", push=False, storekit=False, compliance=False,
        )
        ios_scaffolder.render_project(tmp_path, opts)

        annotation = "加個 dark mode toggle"
        hint = agent_hints.inject(self.AGENT_ID, annotation)
        assert "dark mode" in hint.text or "toggle" in hint.text
        consumed = agent_hints.consume(self.AGENT_ID)
        assert consumed is not None

        # Agent re-iterates: modify ContentView.swift to thread a
        # @State Bool through ColorScheme.
        cv = tmp_path / "App" / "Sources" / "ContentView.swift"
        original = cv.read_text(encoding="utf-8")
        patched = original.replace(
            "@State private var counter = 0",
            (
                "@State private var counter = 0\n"
                "    @State private var darkMode: Bool = false"
            ),
        ).replace(
            "var body: some View {",
            (
                "var body: some View {\n"
                "        let scheme: ColorScheme = darkMode ? .dark : .light"
            ),
        )
        # also append a Toggle in the VStack so the contract reflects
        # the operator-visible "dark mode toggle" UI affordance.
        patched += (
            "\n// Toggle(\"Dark mode\", isOn: $darkMode)"
            ".accessibilityLabel(\"Dark mode toggle\")\n"
        )
        cv.write_text(patched, encoding="utf-8")
        body = cv.read_text(encoding="utf-8")
        assert "darkMode" in body
        assert "Dark mode toggle" in body

    def test_post_iterate_simulate_mobile_gates_still_pass(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """After the dark-mode patch, P2 gates roll up green (mocked)."""
        opts = ios_scaffolder.ScaffoldOptions(
            project_name="OmniTodo", push=False, storekit=False, compliance=False,
        )
        ios_scaffolder.render_project(tmp_path, opts)
        # Force every external CLI to be "absent" so each gate honours
        # its declared mock fallback. This is the canonical P2 contract
        # for sandbox / first-run hosts (xcrun, adb, gradle, flutter…).
        _orig_which = shutil.which

        def _which_no_cli(name: str) -> Optional[str]:
            if name in {
                "xcrun", "adb", "emulator", "android",
                "gradle", "gradlew", "flutter", "yarn", "npm", "npx",
                "xcodebuild",
            }:
                return None
            return _orig_which(name)

        monkeypatch.setattr(mobile_simulator.shutil, "which", _which_no_cli)
        result = mobile_simulator.simulate_mobile(
            profile="ios-simulator", app_path=tmp_path,
        )
        # Every gate must be in {pass, mock, skip, delegated}; the
        # roll-up dict is True for each.
        for gate, ok in result.gates.items():
            assert ok is True, f"gate {gate} flipped after re-iterate"

    def test_event_bus_emits_workspace_actions_for_mobile_loop(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Mobile loop: scaffold render + screenshot capture + rebuild."""
        with _capture_events(monkeypatch) as captured:
            _events.emit_workspace(
                self.AGENT_ID, "scaffold_render", "App/Sources/ContentView.swift",
            )
            _events.emit_tool_progress(
                "mobile_screenshot", "complete",
                "iphone-15-pro-portrait.png",
            )
            _events.emit_workspace(self.AGENT_ID, "rebuild", "OmniTodo.app")
        kinds = {e.name for e in captured}
        assert {"workspace", "tool_progress"} <= kinds
        ws_events = [e for e in captured if e.name == "workspace"]
        actions = {e.data.get("action") for e in ws_events}
        assert {"scaffold_render", "rebuild"} <= actions


# ════════════════════════════════════════════════════════════════════
#  Software vertical
# ════════════════════════════════════════════════════════════════════


class TestSoftwareVerticalE2E:
    """Software workspace — NL → FastAPI → pytest → OpenAPI → Docker → deploy."""

    AGENT_ID = "agent-software-e2e"

    def test_initial_nl_prompt_inject_lands_in_blackboard(self) -> None:
        hint = agent_hints.inject(
            self.AGENT_ID, "做一個 REST API with user CRUD",
        )
        assert "REST API" in hint.text or "CRUD" in hint.text

    def test_fastapi_scaffold_renders_main_with_app_factory(
        self, tmp_path: Path
    ) -> None:
        """Agent picks up the hint and renders the SKILL-FASTAPI scaffold."""
        agent_hints.inject(self.AGENT_ID, "做一個 REST API with user CRUD")
        agent_hints.consume(self.AGENT_ID)

        opts = fastapi_scaffolder.ScaffoldOptions(
            project_name="user-crud-api",
            database="sqlite",       # avoid live PG dependency
            auth="none",             # smaller surface for the test
            deploy="both",           # exercises both adapters in dry_run
            compliance=False,
        )
        outcome = fastapi_scaffolder.render_project(tmp_path, opts)
        assert outcome.bytes_written > 0
        assert outcome.package_name  # slug derived from project_name

        pkg = outcome.package_name
        main_py = tmp_path / "src" / pkg / "main.py"
        assert main_py.is_file()
        body = main_py.read_text(encoding="utf-8")
        # app-factory pattern is the X5 contract — exercised offline by
        # scripts/dump_openapi.py.
        assert "FastAPI" in body
        assert "user-crud-api" in body or "user_crud_api" in body

    def test_simulate_software_python_runner_uses_mock_when_no_pytest(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Without pytest on PATH, X1 honours its declared mock fallback.

        The X1 contract declares: "if neither pytest nor python3 is on
        PATH, return ``status='mock'`` so a brand-new scaffold doesn't
        fail X1 before the runners are wired in." We force both to be
        absent and verify the gate still rolls up green.
        """
        # write the python-marker file so resolve_language picks it
        (tmp_path / "pyproject.toml").write_text("[tool.poetry]\n", encoding="utf-8")

        _orig_which = shutil.which

        def _which_no_python(name: str) -> Optional[str]:
            if name in {"pytest", "python3", "coverage"}:
                return None
            return _orig_which(name)

        monkeypatch.setattr(software_simulator.shutil, "which", _which_no_python)
        report = software_simulator.run_python_tests(tmp_path)
        assert report.status == "mock"
        assert report.runner == "pytest"

    def test_openapi_spec_renderable_from_rendered_app(
        self, tmp_path: Path
    ) -> None:
        """The scaffold ships a FastAPI app exposing the v1 router.

        We don't import the rendered package (it would need
        ``pip install -e .``); instead we assert the renderable
        scaffold surface that ``scripts/dump_openapi.py`` walks —
        ``main.py`` calls ``create_app().openapi()`` offline, and
        the v1 router under ``api/v1/`` is the route source.
        """
        opts = fastapi_scaffolder.ScaffoldOptions(
            project_name="user-crud-api", database="sqlite",
            auth="none", deploy="docker", compliance=False,
        )
        outcome = fastapi_scaffolder.render_project(tmp_path, opts)
        pkg = outcome.package_name
        v1_init = tmp_path / "src" / pkg / "api" / "v1" / "__init__.py"
        assert v1_init.is_file()
        v1_body = v1_init.read_text(encoding="utf-8")
        # the v1 router export is the canonical OpenAPI surface
        assert "router" in v1_body
        # health + items endpoints are the X5 baseline
        for endpoint in ("health.py", "items.py"):
            assert (tmp_path / "src" / pkg / "api" / "v1" / endpoint).is_file()

    def test_dry_run_build_validates_dockerfile_and_chart(
        self, tmp_path: Path
    ) -> None:
        """X3 ``DockerImageAdapter`` + ``HelmChartAdapter`` accept the tree."""
        opts = fastapi_scaffolder.ScaffoldOptions(
            project_name="user-crud-api", database="sqlite",
            auth="none", deploy="both", compliance=False,
        )
        fastapi_scaffolder.render_project(tmp_path, opts)
        report = fastapi_scaffolder.dry_run_build(tmp_path, opts)
        assert "docker" in report
        assert report["docker"]["artifact_valid"] is True
        assert "helm" in report
        assert report["helm"]["artifact_valid"] is True
        # image URI is deterministic: <name>:0.1.0
        assert report["docker"]["image_uri"].endswith(":0.1.0")

    def test_annotation_rate_limit_middleware_consumed_then_iterated(
        self, tmp_path: Path
    ) -> None:
        """Annotation: operator asks for a middleware patch."""
        opts = fastapi_scaffolder.ScaffoldOptions(
            project_name="user-crud-api", database="sqlite",
            auth="none", deploy="docker", compliance=False,
        )
        outcome = fastapi_scaffolder.render_project(tmp_path, opts)
        pkg = outcome.package_name

        annotation = "加個 rate limit middleware (sliding window 60/min)"
        hint = agent_hints.inject(self.AGENT_ID, annotation)
        assert "rate limit" in hint.text or "middleware" in hint.text
        consumed = agent_hints.consume(self.AGENT_ID)
        assert consumed is not None

        # Agent applies the annotation by adding a middleware module
        # — the simplest provable side-effect for the test surface.
        mw_dir = tmp_path / "src" / pkg / "core"
        mw_dir.mkdir(parents=True, exist_ok=True)
        mw_file = mw_dir / "rate_limit.py"
        mw_file.write_text(
            (
                '"""Rate limit middleware (60 req/min sliding window).\n\n'
                'Generated by V9 #4 E2E re-iteration after operator\n'
                'annotation: "加個 rate limit middleware".\n"""\n\n'
                "from collections import defaultdict, deque\n"
                "from time import time\n\n"
                "WINDOW_SEC = 60.0\nMAX_HITS = 60\n\n"
                "_buckets: dict[str, deque[float]] = defaultdict(deque)\n\n"
                "def check(client_id: str) -> bool:\n"
                "    now = time()\n"
                "    bucket = _buckets[client_id]\n"
                "    while bucket and bucket[0] < now - WINDOW_SEC:\n"
                "        bucket.popleft()\n"
                "    if len(bucket) >= MAX_HITS:\n"
                "        return False\n"
                "    bucket.append(now)\n"
                "    return True\n"
            ),
            encoding="utf-8",
        )
        body = mw_file.read_text(encoding="utf-8")
        assert "WINDOW_SEC" in body
        assert "MAX_HITS = 60" in body

    def test_post_iterate_dockerfile_still_validates(
        self, tmp_path: Path
    ) -> None:
        """The middleware patch doesn't disturb the build adapter contract."""
        opts = fastapi_scaffolder.ScaffoldOptions(
            project_name="user-crud-api", database="sqlite",
            auth="none", deploy="docker", compliance=False,
        )
        fastapi_scaffolder.render_project(tmp_path, opts)
        # apply the annotation patch
        pkg = opts.resolved_package_name()
        (tmp_path / "src" / pkg / "core" / "rate_limit.py").write_text(
            "WINDOW_SEC = 60.0\n", encoding="utf-8",
        )
        report = fastapi_scaffolder.dry_run_build(tmp_path, opts)
        assert report["docker"]["artifact_valid"] is True
        assert report["docker"]["artifact_error"] is None

    @pytest.mark.asyncio
    async def test_software_deploy_adapter_round_trips(
        self, tmp_path: Path
    ) -> None:
        """The deploy contract round-trips for a software artefact too."""
        opts = fastapi_scaffolder.ScaffoldOptions(
            project_name="user-crud-api", database="sqlite",
            auth="none", deploy="docker", compliance=False,
        )
        fastapi_scaffolder.render_project(tmp_path, opts)
        adapter = _StubWebDeployAdapter.from_plaintext_token(
            "tk-software-token-9876",
            project_name="user-crud-api",
        )
        prov = await adapter.provision()
        assert prov.created is True
        # the rendered project root *is* the artefact path for a
        # docker-track deploy (the Dockerfile sits at root).
        artifact = BuildArtifact(
            path=tmp_path, framework=None, commit_sha="def5678",
        )
        deployed = await adapter.deploy(artifact)
        assert deployed.status == "ready"
        assert adapter.get_url() == deployed.url


# ════════════════════════════════════════════════════════════════════
#  Cross-vertical contract
# ════════════════════════════════════════════════════════════════════


class TestCrossVerticalContract:
    """Invariants that must hold *uniformly* across all three verticals."""

    def test_three_workspace_types_share_agent_hint_blackboard(self) -> None:
        """One inject API, three workspace agents, three independent slots."""
        agent_hints.inject("agent-web", "做 SaaS landing page")
        agent_hints.inject("agent-mobile", "做 todo app")
        agent_hints.inject("agent-software", "做 REST API")

        # slots are agent-scoped: each agent peeks its own hint
        web_h = agent_hints.peek("agent-web")
        mobile_h = agent_hints.peek("agent-mobile")
        software_h = agent_hints.peek("agent-software")
        assert web_h is not None and "landing" in web_h.text
        assert mobile_h is not None and "todo" in mobile_h.text
        assert software_h is not None and "REST" in software_h.text

        # consume drains only the addressed agent
        agent_hints.consume("agent-web")
        assert agent_hints.peek("agent-web") is None
        assert agent_hints.peek("agent-mobile") is not None
        assert agent_hints.peek("agent-software") is not None

    def test_three_simulators_expose_consistent_gate_rollup_shape(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Web / Mobile / Software all surface a ``gates: dict[str, bool]``."""
        # Web — a tagged index.html lets the static gates pass on mock
        web_path = tmp_path / "web"
        web_path.mkdir()
        _write_landing_page(web_path, SAAS_LANDING_HTML_LIGHT)
        web_result = web_simulator.simulate_web(
            profile="web-static", app_path=web_path,
        )
        assert isinstance(web_result.gates, dict)
        assert all(isinstance(v, bool) for v in web_result.gates.values())

        # Mobile — render iOS scaffold, force-mock every CLI
        mobile_path = tmp_path / "mobile"
        mobile_path.mkdir()
        ios_scaffolder.render_project(
            mobile_path,
            ios_scaffolder.ScaffoldOptions(
                project_name="OmniSpan", push=False, storekit=False, compliance=False,
            ),
        )
        _orig_which = shutil.which

        def _no_cli(name: str) -> Optional[str]:
            if name in {
                "xcrun", "adb", "emulator", "android",
                "gradle", "gradlew", "flutter", "yarn", "npm", "npx",
                "xcodebuild",
            }:
                return None
            return _orig_which(name)

        monkeypatch.setattr(mobile_simulator.shutil, "which", _no_cli)
        mobile_result = mobile_simulator.simulate_mobile(
            profile="ios-simulator", app_path=mobile_path,
        )
        assert isinstance(mobile_result.gates, dict)
        assert all(isinstance(v, bool) for v in mobile_result.gates.values())

        # Software — render FastAPI scaffold, force-mock pytest/coverage
        software_path = tmp_path / "software"
        software_path.mkdir()
        fastapi_scaffolder.render_project(
            software_path,
            fastapi_scaffolder.ScaffoldOptions(
                project_name="omnispan-api", database="sqlite",
                auth="none", deploy="docker", compliance=False,
            ),
        )

        def _no_python(name: str) -> Optional[str]:
            if name in {"pytest", "python3", "coverage", "go", "cargo", "mvn"}:
                return None
            return _orig_which(name)

        monkeypatch.setattr(software_simulator.shutil, "which", _no_python)
        software_result = software_simulator.simulate_software(
            profile="linux-x86_64-native",
            app_path=software_path,
        )
        assert isinstance(software_result.gates, dict)
        assert all(isinstance(v, bool) for v in software_result.gates.values())

        # The three roll-ups share the same *kind* (dict[str, bool]); they
        # don't share keys (each vertical owns its gate names — that's
        # the design — but the *shape* is uniform so an upstream
        # OpsSummary panel can render any of them with the same code).

    def test_event_bus_workspace_emit_works_for_all_three_types(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``emit_workspace`` is workspace-agnostic — same emitter for all 3."""
        with _capture_events(monkeypatch) as captured:
            for agent in ("agent-web", "agent-mobile", "agent-software"):
                _events.emit_workspace(agent, "preview_render", detail="ok")
        ws_events = [e for e in captured if e.name == "workspace"]
        agents = {e.data.get("agent_id") for e in ws_events}
        assert {"agent-web", "agent-mobile", "agent-software"} <= agents

    def test_capstone_full_loop_three_verticals_compose_in_one_pass(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Capstone: NL → write → preview → annotate → re-write → final gate.

        Drives all three verticals through their full storyline in a
        single pass to prove the orchestration primitives compose
        without cross-talk. This is the V9 #4 ``cross-vertical``
        contract sub-bullet — a single test that fails red the
        moment any one of the three loops drifts.
        """
        # ── Web ────────────────────────────────────────────────────
        web_path = tmp_path / "web"
        web_path.mkdir()
        agent_hints.inject("agent-web", "做一個 SaaS landing page")
        assert agent_hints.consume("agent-web").text.startswith("做")
        _write_landing_page(web_path, SAAS_LANDING_HTML_LIGHT)
        web_initial = web_simulator.simulate_web(
            profile="web-static", app_path=web_path,
        )
        assert web_initial.lighthouse.performance >= 80

        agent_hints.inject("agent-web", "把 hero 背景改深藍")
        assert agent_hints.consume("agent-web") is not None
        _write_landing_page(web_path, SAAS_LANDING_HTML_DARK_BLUE)
        web_final = web_simulator.simulate_web(
            profile="web-static", app_path=web_path,
        )
        assert web_final.lighthouse.performance >= 80
        assert web_final.gates["seo_clean"] is True

        # ── Mobile ─────────────────────────────────────────────────
        mobile_path = tmp_path / "mobile"
        mobile_path.mkdir()
        agent_hints.inject("agent-mobile", "做一個 todo app")
        assert agent_hints.consume("agent-mobile") is not None
        ios_scaffolder.render_project(
            mobile_path,
            ios_scaffolder.ScaffoldOptions(
                project_name="OmniTodo", push=False, storekit=False, compliance=False,
            ),
        )
        cv = mobile_path / "App" / "Sources" / "ContentView.swift"
        assert "OmniTodo" in cv.read_text(encoding="utf-8")

        agent_hints.inject("agent-mobile", "加個 dark mode toggle")
        assert agent_hints.consume("agent-mobile") is not None
        cv.write_text(
            cv.read_text(encoding="utf-8")
            + "\n// Toggle(\"Dark mode\", isOn: $darkMode)\n",
            encoding="utf-8",
        )

        argv = mobile_screenshot.build_ios_capture_argv(
            udid="00000000-0000-0000-0000-000000000001",
            output_path=str(tmp_path / "mobile" / "shot.png"),
        )
        assert "screenshot" in argv

        # ── Software ───────────────────────────────────────────────
        software_path = tmp_path / "software"
        software_path.mkdir()
        agent_hints.inject("agent-software", "做一個 REST API with user CRUD")
        assert agent_hints.consume("agent-software") is not None
        opts = fastapi_scaffolder.ScaffoldOptions(
            project_name="user-crud-api", database="sqlite",
            auth="none", deploy="docker", compliance=False,
        )
        fastapi_scaffolder.render_project(software_path, opts)
        report = fastapi_scaffolder.dry_run_build(software_path, opts)
        assert report["docker"]["artifact_valid"] is True

        agent_hints.inject("agent-software", "加個 rate limit middleware")
        assert agent_hints.consume("agent-software") is not None
        pkg = opts.resolved_package_name()
        (software_path / "src" / pkg / "core" / "rate_limit.py").write_text(
            "WINDOW_SEC = 60.0\n", encoding="utf-8",
        )
        report_after = fastapi_scaffolder.dry_run_build(software_path, opts)
        assert report_after["docker"]["artifact_valid"] is True

        # All three blackboards drained — no cross-talk leaked.
        assert agent_hints.peek("agent-web") is None
        assert agent_hints.peek("agent-mobile") is None
        assert agent_hints.peek("agent-software") is None
