/**
 * AB.8 — ProviderModeWizard tests.
 */

import { describe, expect, it, vi } from "vitest"
import { render, screen } from "@testing-library/react"
import userEvent from "@testing-library/user-event"

import { ProviderModeWizard } from "@/components/omnisight/ab/provider-mode-wizard"
import type { WizardState } from "@/components/omnisight/ab/types"

function _state(over: Partial<WizardState> = {}): WizardState {
  return {
    mode: "subscription",
    current_step: "not_started",
    target_workspace: "production",
    api_key_configured: false,
    api_key_fingerprint: "",
    spend_daily_usd: null,
    spend_monthly_usd: null,
    fallback_subscription_kept: true,
    smoke_test: null,
    started_at: null,
    completed_at: null,
    rollback_grace_until: null,
    ...over,
  }
}

describe("ProviderModeWizard", () => {
  it("renders subscription mode badge by default", () => {
    render(<ProviderModeWizard state={_state()} />)
    expect(screen.getByTestId("mode-badge-subscription")).toBeInTheDocument()
  })

  it("step 1 form invokes onSubmitApiKey with workspace", async () => {
    const onSubmitApiKey = vi.fn().mockResolvedValue(undefined)
    const user = userEvent.setup()
    render(
      <ProviderModeWizard
        state={_state()}
        onSubmitApiKey={onSubmitApiKey}
      />,
    )
    const input = screen.getByTestId("wizard-api-key")
    await user.type(input, "sk-ant-AAAAAAAAAAAAAAAAAAAAAAAAAA")
    const ws = screen.getByTestId("wizard-workspace")
    await user.selectOptions(ws, "batch")
    await user.click(screen.getByTestId("wizard-submit-api-key"))
    expect(onSubmitApiKey).toHaveBeenCalledWith(
      "sk-ant-AAAAAAAAAAAAAAAAAAAAAAAAAA",
      "batch",
    )
  })

  it("step 1 submit disabled when API key empty", () => {
    render(<ProviderModeWizard state={_state()} onSubmitApiKey={vi.fn()} />)
    const btn = screen.getByTestId("wizard-submit-api-key")
    expect(btn).toBeDisabled()
  })

  it("step 1 surfaces error from onSubmitApiKey", async () => {
    const onSubmitApiKey = vi.fn().mockRejectedValue(new Error("bad key"))
    const user = userEvent.setup()
    render(
      <ProviderModeWizard
        state={_state()}
        onSubmitApiKey={onSubmitApiKey}
      />,
    )
    await user.type(
      screen.getByTestId("wizard-api-key"),
      "sk-ant-zzzzzzzzzzzzzzzzzzzzzzzzzz",
    )
    await user.click(screen.getByTestId("wizard-submit-api-key"))
    const err = await screen.findByTestId("wizard-error")
    expect(err).toHaveTextContent("bad key")
  })

  it("at key_obtained step shows step 2 form", async () => {
    const onConfigureSpendLimits = vi.fn().mockResolvedValue(undefined)
    const user = userEvent.setup()
    render(
      <ProviderModeWizard
        state={_state({
          current_step: "key_obtained",
          api_key_configured: true,
          api_key_fingerprint: "…ABC12345",
        })}
        onConfigureSpendLimits={onConfigureSpendLimits}
      />,
    )
    const dailyInput = screen.getByTestId("wizard-daily-cap")
    await user.clear(dailyInput)
    await user.type(dailyInput, "30")
    const monthlyInput = screen.getByTestId("wizard-monthly-cap")
    await user.clear(monthlyInput)
    await user.type(monthlyInput, "500")
    await user.click(screen.getByTestId("wizard-submit-spend"))
    expect(onConfigureSpendLimits).toHaveBeenCalledWith(30, 500)
  })

  it("at spend_limits_set step shows step 3 switch button", async () => {
    const onSwitchMode = vi.fn().mockResolvedValue(undefined)
    const user = userEvent.setup()
    render(
      <ProviderModeWizard
        state={_state({ current_step: "spend_limits_set" })}
        onSwitchMode={onSwitchMode}
      />,
    )
    await user.click(screen.getByTestId("wizard-switch-mode"))
    expect(onSwitchMode).toHaveBeenCalled()
  })

  it("at mode_switched step shows step 4 + smoke test result", async () => {
    const onRunSmokeTest = vi.fn().mockResolvedValue(undefined)
    const user = userEvent.setup()
    render(
      <ProviderModeWizard
        state={_state({
          current_step: "mode_switched",
          mode: "api",
          smoke_test: {
            call_id: "smoke_1",
            success: true,
            latency_ms: 250,
            cost_usd: 0.0012,
            response_excerpt: "ok",
          },
        })}
        onRunSmokeTest={onRunSmokeTest}
      />,
    )
    expect(screen.getByTestId("wizard-smoke-result")).toHaveTextContent(
      "Smoke test passed",
    )
    expect(screen.getByTestId("wizard-smoke-result")).toHaveTextContent(
      "250ms",
    )
    await user.click(screen.getByTestId("wizard-run-smoke"))
    expect(onRunSmokeTest).toHaveBeenCalled()
  })

  it("at mode_switched step shows failed smoke result", () => {
    render(
      <ProviderModeWizard
        state={_state({
          current_step: "mode_switched",
          mode: "api",
          smoke_test: {
            call_id: "smoke_2",
            success: false,
            latency_ms: 0,
            cost_usd: 0,
            error_message: "401 Unauthorized",
          },
        })}
        onRunSmokeTest={vi.fn()}
      />,
    )
    expect(screen.getByTestId("wizard-smoke-result")).toHaveTextContent(
      "401 Unauthorized",
    )
  })

  it("at smoke_test_passed step shows confirm button", async () => {
    const onConfirm = vi.fn().mockResolvedValue(undefined)
    const user = userEvent.setup()
    render(
      <ProviderModeWizard
        state={_state({
          current_step: "smoke_test_passed",
          mode: "api",
          smoke_test: {
            call_id: "ok",
            success: true,
            latency_ms: 100,
            cost_usd: 0.001,
          },
        })}
        onConfirm={onConfirm}
      />,
    )
    await user.click(screen.getByTestId("wizard-confirm"))
    expect(onConfirm).toHaveBeenCalled()
  })

  it("at confirmed step shows summary + rollback button", async () => {
    const onRollback = vi.fn().mockResolvedValue(undefined)
    const user = userEvent.setup()
    render(
      <ProviderModeWizard
        state={_state({
          current_step: "confirmed",
          mode: "api",
          api_key_fingerprint: "…XYZABCDE",
          spend_daily_usd: 30,
          spend_monthly_usd: 500,
          completed_at: new Date(Date.now() - 60_000).toISOString(),
          rollback_grace_until: new Date(
            Date.now() + 30 * 86_400_000,
          ).toISOString(),
        })}
        onRollback={onRollback}
      />,
    )
    expect(
      screen.getByTestId("wizard-confirmed-summary"),
    ).toHaveTextContent("API mode active")
    // Fingerprint surfaced (no full key)
    expect(
      screen.getByTestId("wizard-confirmed-summary"),
    ).toHaveTextContent("…XYZABCDE")
    await user.click(screen.getByTestId("wizard-rollback"))
    expect(onRollback).toHaveBeenCalled()
  })

  it("at confirmed + finalized state hides rollback, shows lock", () => {
    render(
      <ProviderModeWizard
        state={_state({
          current_step: "confirmed",
          mode: "api",
          fallback_subscription_kept: false,
          completed_at: new Date().toISOString(),
        })}
        onRollback={vi.fn()}
      />,
    )
    expect(screen.queryByTestId("wizard-rollback")).not.toBeInTheDocument()
    expect(
      screen.getByTestId("wizard-rollback-locked"),
    ).toBeInTheDocument()
  })

  it("step indicator marks completed steps", () => {
    render(
      <ProviderModeWizard
        state={_state({ current_step: "spend_limits_set" })}
      />,
    )
    expect(
      screen.getByTestId("wizard-step-key_obtained"),
    ).toHaveAttribute("data-done", "true")
    expect(
      screen.getByTestId("wizard-step-spend_limits_set"),
    ).toHaveAttribute("data-active", "true")
    expect(
      screen.getByTestId("wizard-step-mode_switched"),
    ).toHaveAttribute("data-done", "false")
  })
})
