/**
 * L8 #2 — Browser-level happy path for the first-install bootstrap wizard.
 *
 * Drives the five wizard gates end-to-end with every backend response
 * mocked at the browser network boundary (``page.route``):
 *
 *   Step 1 — admin password rotation
 *   Step 2 — LLM provider (Anthropic; ``provider.ping()`` mocked)
 *   Step 3 — Cloudflare Tunnel (LAN-only skip — the CF API never fires)
 *   Step 4 — parallel service health check (all green)
 *   Step 5 — smoke subset (both DAGs pass + audit chain OK)
 *   Finalize — ``POST /bootstrap/finalize`` + client-side redirect to ``/``
 *
 * Why mocks instead of hitting the real backend: the wizard is the
 * one-shot first-install path. Driving it for real would mutate app
 * state the other E2E tests rely on (admin password, LLM secret,
 * bootstrap_finalized) and require provisioning a real Cloudflare API
 * token. Mocking the five bootstrap endpoints keeps this spec hermetic
 * and fast — it verifies the UI wiring + gate-flip logic without
 * destabilising other tests.
 *
 * The mock state object ``flow`` doubles as a test oracle — after the
 * run we assert every gate was actually flipped by the UI (not just
 * that the pill turned green). That catches a regression where a step
 * painted green without actually calling its POST.
 */
import { test, expect, type Page } from "@playwright/test"

interface BootstrapFlowState {
  admin_password_default: boolean
  llm_provider_configured: boolean
  cf_tunnel_configured: boolean
  smoke_passed: boolean
  finalized: boolean
  // Counters — we assert each POST fired at least once so a green pill
  // that came from a stale cache would fail the run.
  admin_password_calls: number
  llm_provision_calls: number
  cf_tunnel_skip_calls: number
  parallel_health_calls: number
  smoke_subset_calls: number
  finalize_calls: number
}

function _freshFlow(): BootstrapFlowState {
  return {
    admin_password_default: true,
    llm_provider_configured: false,
    cf_tunnel_configured: false,
    smoke_passed: false,
    finalized: false,
    admin_password_calls: 0,
    llm_provision_calls: 0,
    cf_tunnel_skip_calls: 0,
    parallel_health_calls: 0,
    smoke_subset_calls: 0,
    finalize_calls: 0,
  }
}

function _statusBody(state: BootstrapFlowState) {
  const gates = {
    admin_password_default: state.admin_password_default,
    llm_provider_configured: state.llm_provider_configured,
    cf_tunnel_configured: state.cf_tunnel_configured,
    smoke_passed: state.smoke_passed,
  }
  const missing: string[] = []
  if (gates.admin_password_default) missing.push("admin_password")
  if (!gates.llm_provider_configured) missing.push("llm_provider")
  if (!gates.cf_tunnel_configured) missing.push("cf_tunnel")
  if (!gates.smoke_passed) missing.push("smoke")
  return {
    status: gates,
    all_green: missing.length === 0,
    finalized: state.finalized,
    missing_steps: missing,
  }
}

async function installBootstrapMocks(page: Page, flow: BootstrapFlowState) {
  // GET /bootstrap/status — stateful. Every gate flip mutates ``flow``
  // so the next GET reflects reality.
  await page.route("**/api/v1/bootstrap/status", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(_statusBody(flow)),
    })
  })

  // POST /bootstrap/admin-password — flips ``admin_password_default``.
  await page.route("**/api/v1/bootstrap/admin-password", async (route) => {
    flow.admin_password_calls += 1
    flow.admin_password_default = false
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        status: "ok",
        admin_password_default: false,
        user_id: "admin-e2e-0",
      }),
    })
  })

  // POST /bootstrap/llm-provision — flips ``llm_provider_configured``.
  // Mock the Anthropic provider ping: returns a fake fingerprint +
  // latency so the UI's success banner has something to render.
  await page.route("**/api/v1/bootstrap/llm-provision", async (route) => {
    flow.llm_provision_calls += 1
    flow.llm_provider_configured = true
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        status: "ok",
        provider: "anthropic",
        model: "claude-opus-4-7",
        fingerprint: "sha256:e2e-mock-fingerprint",
        latency_ms: 42,
        models: [],
      }),
    })
  })

  // POST /bootstrap/cf-tunnel-skip — flips ``cf_tunnel_configured`` via
  // the LAN-only escape hatch. The CF API itself never fires because
  // the test clicks "Skip" rather than opening the B12 wizard.
  await page.route("**/api/v1/bootstrap/cf-tunnel-skip", async (route) => {
    flow.cf_tunnel_skip_calls += 1
    flow.cf_tunnel_configured = true
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        status: "ok",
        cf_tunnel_configured: true,
      }),
    })
  })

  // POST /bootstrap/parallel-health-check — four green probes.
  // ``cf_tunnel`` returns status=skipped (not green) to reflect the
  // LAN-only choice from Step 3. The UI treats skipped as green.
  await page.route("**/api/v1/bootstrap/parallel-health-check", async (route) => {
    flow.parallel_health_calls += 1
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        all_green: true,
        elapsed_ms: 17,
        backend: {
          ok: true,
          status: "green",
          detail: "GET /healthz 200",
          latency_ms: 3,
        },
        frontend: {
          ok: true,
          status: "green",
          detail: "GET / 200",
          latency_ms: 5,
        },
        db_migration: {
          ok: true,
          status: "green",
          detail: "migrations up-to-date",
          latency_ms: 4,
        },
        cf_tunnel: {
          ok: true,
          status: "skipped",
          detail: "LAN-only skip recorded at Step 3",
          latency_ms: 1,
        },
      }),
    })
  })

  // POST /bootstrap/smoke-subset — both DAGs pass + audit chain OK.
  await page.route("**/api/v1/bootstrap/smoke-subset", async (route) => {
    flow.smoke_subset_calls += 1
    flow.smoke_passed = true
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        smoke_passed: true,
        subset: "both",
        elapsed_ms: 1234,
        runs: [
          {
            key: "dag1",
            label: "compile-flash host_native",
            dag_id: "dag_1",
            ok: true,
            validation_errors: [],
            run_id: "e2e-run-dag1",
            plan_id: 1001,
            plan_status: "completed",
            task_count: 4,
            t3_runner: "runner-host-native",
            target_platform: "host_native",
          },
          {
            key: "dag2",
            label: "cross-compile aarch64",
            dag_id: "dag_2",
            ok: true,
            validation_errors: [],
            run_id: "e2e-run-dag2",
            plan_id: 1002,
            plan_status: "completed",
            task_count: 5,
            t3_runner: "runner-aarch64",
            target_platform: "aarch64",
          },
        ],
        audit_chain: {
          ok: true,
          first_bad_id: null,
          detail: "2/2 tenants verified",
          tenant_count: 2,
          bad_tenants: [],
        },
      }),
    })
  })

  // POST /bootstrap/finalize — flips ``finalized``.
  await page.route("**/api/v1/bootstrap/finalize", async (route) => {
    flow.finalize_calls += 1
    flow.finalized = true
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        finalized: true,
        status: {
          admin_password_default: flow.admin_password_default,
          llm_provider_configured: flow.llm_provider_configured,
          cf_tunnel_configured: flow.cf_tunnel_configured,
          smoke_passed: flow.smoke_passed,
        },
        actor_user_id: "admin-e2e-0",
      }),
    })
  })

  // Cloudflare endpoints — defensive stub. We pick "Skip (LAN-only)"
  // in Step 3 so the embedded B12 wizard never opens, but if a future
  // change auto-probes ``/cloudflare/status`` on mount we keep the
  // test hermetic by refusing to let it reach the live backend.
  await page.route("**/api/v1/cloudflare/**", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ provisioned: false }),
    })
  })

  // Home-page API calls fired after router.replace("/") — we don't
  // care about them, but we don't want them to hit the real backend
  // and flake the test. Stub out the common reads with empty shapes.
  await page.route("**/api/v1/operation-mode", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ mode: "supervised", parallel_cap: 2, in_flight: 0, over_cap: 0 }),
    })
  })
  await page.route("**/api/v1/budget-strategy", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ strategy: "balanced", tuning: { max_retries: 2, model_tier: "standard" } }),
    })
  })
}

test.describe("L8 #2 — Bootstrap wizard happy path (mocked)", () => {
  test("walks all five gates and finalizes", async ({ page }) => {
    const flow = _freshFlow()
    await installBootstrapMocks(page, flow)

    await page.goto("/bootstrap")

    // ── Step 1 — Admin password rotation ──────────────────────────
    // The wizard auto-advances to the first red step on mount; the
    // shipping default makes ``admin_password`` the first red step.
    const adminPasswordForm = page.getByTestId("bootstrap-admin-password-form")
    await expect(adminPasswordForm).toBeVisible({ timeout: 15_000 })
    await page.getByTestId("bootstrap-admin-password-current").fill("omnisight-admin")
    // 20 chars, 4 classes, no common substrings, no 3-char repeats,
    // no length-4 keyboard sequence — passes ``estimatePasswordStrength``
    // with score 4 (>= PASSWORD_MIN_SCORE) so the submit button enables.
    const strongPassword = "MockP@ssphrase-2026!"
    await page.getByTestId("bootstrap-admin-password-new").fill(strongPassword)
    await page.getByTestId("bootstrap-admin-password-confirm").fill(strongPassword)
    await page.getByTestId("bootstrap-admin-password-submit").click()

    // Sidebar pill flips to green once status reloads.
    await expect(page.getByTestId("bootstrap-step-admin_password"))
      .toHaveAttribute("data-state", "green", { timeout: 15_000 })

    // ── Step 2 — LLM provider (Anthropic ping mocked) ─────────────
    // Auto-advance lands us here; pick Anthropic, paste a dummy key,
    // submit. The mocked provision endpoint returns a success body.
    await expect(page.getByTestId("bootstrap-llm-provider-step"))
      .toBeVisible({ timeout: 10_000 })
    await page.getByTestId("bootstrap-llm-provider-option-anthropic").click()
    await page.getByTestId("bootstrap-llm-provider-api-key")
      .fill("sk-ant-mock-e2e-key-not-real")
    await page.getByTestId("bootstrap-llm-provider-submit").click()

    await expect(page.getByTestId("bootstrap-step-llm_provider"))
      .toHaveAttribute("data-state", "green", { timeout: 15_000 })

    // ── Step 3 — Cloudflare Tunnel (LAN-only skip) ────────────────
    // Use the documented LAN-only escape hatch so the mocked CF API
    // never fires. The skip records an audit warning on the backend
    // and flips ``cf_tunnel_configured``.
    await expect(page.getByTestId("bootstrap-cf-tunnel-step"))
      .toBeVisible({ timeout: 10_000 })
    await page.getByTestId("bootstrap-cf-tunnel-skip-reveal").click()
    await page.getByTestId("bootstrap-cf-tunnel-skip-reason")
      .fill("E2E mocked run — LAN-only install")
    await page.getByTestId("bootstrap-cf-tunnel-skip-confirm").click()

    await expect(page.getByTestId("bootstrap-step-cf_tunnel"))
      .toHaveAttribute("data-state", "green", { timeout: 15_000 })

    // ── Step 4 — Service health (all-green on first probe) ────────
    // The step auto-probes on mount; the mock returns all_green=true
    // immediately, so the sidebar pill flips green without a manual
    // interaction. We don't assert the step panel itself because the
    // auto-advance effect may have already moved past it — the pill
    // is the durable contract.
    await expect(page.getByTestId("bootstrap-step-services_ready"))
      .toHaveAttribute("data-state", "green", { timeout: 15_000 })

    // ── Step 5 — Smoke subset (both DAGs green + audit chain OK) ──
    await expect(page.getByTestId("bootstrap-smoke-subset-step"))
      .toBeVisible({ timeout: 10_000 })
    await page.getByTestId("bootstrap-smoke-run-button").click()

    await expect(page.getByTestId("bootstrap-step-smoke"))
      .toHaveAttribute("data-state", "green", { timeout: 15_000 })

    // ── Finalize ─────────────────────────────────────────────────
    // Auto-advance lands on the Finalize pane once all four backend
    // gates are green. The main finalize button is part of that
    // pane; click it and wait for the client-side redirect away
    // from /bootstrap that ``reloadStatus`` triggers on ``finalized=true``.
    const finalizeButton = page.getByTestId("bootstrap-finalize-button")
    await expect(finalizeButton).toBeVisible({ timeout: 10_000 })
    await expect(finalizeButton).toBeEnabled({ timeout: 10_000 })
    await finalizeButton.click()

    await page.waitForURL(
      (url) => !url.pathname.startsWith("/bootstrap"),
      { timeout: 15_000 },
    )

    // Test oracle: every gate-flip POST fired at least once, and the
    // final state matches the wizard's visible green pills.
    expect(flow.admin_password_calls).toBeGreaterThanOrEqual(1)
    expect(flow.llm_provision_calls).toBeGreaterThanOrEqual(1)
    expect(flow.cf_tunnel_skip_calls).toBeGreaterThanOrEqual(1)
    expect(flow.parallel_health_calls).toBeGreaterThanOrEqual(1)
    expect(flow.smoke_subset_calls).toBeGreaterThanOrEqual(1)
    expect(flow.finalize_calls).toBeGreaterThanOrEqual(1)
    expect(flow.admin_password_default).toBe(false)
    expect(flow.llm_provider_configured).toBe(true)
    expect(flow.cf_tunnel_configured).toBe(true)
    expect(flow.smoke_passed).toBe(true)
    expect(flow.finalized).toBe(true)
  })
})
