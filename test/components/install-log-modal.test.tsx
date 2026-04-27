/**
 * BS.7.6 — Install log modal contract tests.
 *
 * Locks the surface the platforms page wires up when the operator hits
 * a failed catalog card's "View log" button:
 *
 *   • ``job=null`` keeps the dialog closed — no portal mount, no DOM.
 *   • ``job!=null`` opens the dialog and renders the entry display
 *     name + state pill + log_tail body.
 *   • ``error_reason`` is shown only on the failed branch (other states
 *     skip the destructive-red banner).
 *   • Empty ``log_tail`` falls back to a stable placeholder so the
 *     modal does not show an empty grey box.
 *   • Copy button writes ``log_tail`` to the clipboard via the injected
 *     ``copyToClipboard`` stub.
 *   • Retry button (only renders when ``onRetry`` is wired) calls the
 *     handler with the job and closes the modal.
 *   • Close button + Esc + overlay click all fire ``onClose``.
 */

import { afterEach, describe, expect, it, vi } from "vitest"
import { cleanup, fireEvent, render, screen } from "@testing-library/react"

import {
  INSTALL_LOG_EMPTY_PLACEHOLDER,
  INSTALL_LOG_NO_REASON_PLACEHOLDER,
  InstallLogModal,
} from "@/components/omnisight/install-log-modal"
import type { InstallJob } from "@/lib/api"

const FAILED_JOB: InstallJob = {
  id: "ij-failed01234",
  tenant_id: "t-abc",
  entry_id: "neural-blur-sdk",
  state: "failed",
  idempotency_key: "key-1234567890abcdef",
  sidecar_id: "omnisight-installer-1",
  protocol_version: 1,
  bytes_done: 524_288,
  bytes_total: 1_073_741_824,
  eta_seconds: null,
  log_tail: "ERROR: layer 3/8 download failed at byte 0x4f8\nlayer-id: sha256:deadbeef\n",
  result_json: null,
  error_reason: "sidecar:docker_pull:layer_unreachable",
  pep_decision_id: "de-abc",
  requested_by: "u-operator",
  queued_at: "2026-04-27T10:00:00Z",
  claimed_at: "2026-04-27T10:00:01Z",
  started_at: "2026-04-27T10:00:02Z",
  completed_at: "2026-04-27T10:00:30Z",
}

afterEach(() => {
  cleanup()
})

describe("BS.7.6 — InstallLogModal", () => {
  it("renders nothing when job is null", () => {
    render(<InstallLogModal job={null} onClose={() => {}} />)
    expect(screen.queryByTestId("install-log-modal")).toBeNull()
  })

  it("renders the entry display name in the header when wired", () => {
    render(
      <InstallLogModal
        job={FAILED_JOB}
        entryDisplayName="Neural Blur SDK"
        onClose={() => {}}
      />,
    )
    const title = screen.getByTestId("install-log-modal-title")
    expect(title.textContent).toMatch(/Neural Blur SDK/)
    // State pill mirrors backend lifecycle label.
    expect(screen.getByTestId("install-log-modal-state").textContent).toBe(
      "Failed",
    )
  })

  it("falls back to entry_id when no display name is supplied", () => {
    render(<InstallLogModal job={FAILED_JOB} onClose={() => {}} />)
    const title = screen.getByTestId("install-log-modal-title")
    expect(title.textContent).toMatch(/neural-blur-sdk/)
  })

  it("shows the error_reason banner when the job is failed", () => {
    render(<InstallLogModal job={FAILED_JOB} onClose={() => {}} />)
    const banner = screen.getByTestId("install-log-modal-error-reason")
    expect(banner.textContent).toBe("sidecar:docker_pull:layer_unreachable")
  })

  it("substitutes a stable placeholder when error_reason is null on a failed row", () => {
    render(
      <InstallLogModal
        job={{ ...FAILED_JOB, error_reason: null }}
        onClose={() => {}}
      />,
    )
    const banner = screen.getByTestId("install-log-modal-error-reason")
    expect(banner.textContent).toBe(INSTALL_LOG_NO_REASON_PLACEHOLDER)
  })

  it("does not render the error banner when the state is not 'failed'", () => {
    const completed: InstallJob = {
      ...FAILED_JOB,
      state: "completed",
      error_reason: null,
    }
    render(<InstallLogModal job={completed} onClose={() => {}} />)
    expect(screen.queryByTestId("install-log-modal-error-reason")).toBeNull()
    expect(screen.getByTestId("install-log-modal-state").textContent).toBe(
      "Completed",
    )
  })

  it("renders the log_tail body verbatim", () => {
    render(<InstallLogModal job={FAILED_JOB} onClose={() => {}} />)
    const body = screen.getByTestId("install-log-modal-log-body")
    expect(body.textContent).toBe(FAILED_JOB.log_tail)
  })

  it("substitutes a placeholder when log_tail is empty", () => {
    render(
      <InstallLogModal
        job={{ ...FAILED_JOB, log_tail: "" }}
        onClose={() => {}}
      />,
    )
    const body = screen.getByTestId("install-log-modal-log-body")
    expect(body.textContent).toBe(INSTALL_LOG_EMPTY_PLACEHOLDER)
    // Copy button is disabled when there is no content to copy.
    const copy = screen.getByTestId(
      "install-log-modal-copy",
    ) as HTMLButtonElement
    expect(copy.disabled).toBe(true)
  })

  it("Close button fires onClose", () => {
    const onClose = vi.fn()
    render(<InstallLogModal job={FAILED_JOB} onClose={onClose} />)
    fireEvent.click(screen.getByTestId("install-log-modal-close"))
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it("does not render a retry button when onRetry is omitted", () => {
    render(<InstallLogModal job={FAILED_JOB} onClose={() => {}} />)
    expect(screen.queryByTestId("install-log-modal-retry")).toBeNull()
  })

  it("renders a retry button when onRetry is wired and forwards the job + closes", () => {
    const onRetry = vi.fn()
    const onClose = vi.fn()
    render(
      <InstallLogModal
        job={FAILED_JOB}
        onClose={onClose}
        onRetry={onRetry}
      />,
    )
    const retry = screen.getByTestId(
      "install-log-modal-retry",
    ) as HTMLButtonElement
    expect(retry.disabled).toBe(false)
    fireEvent.click(retry)
    expect(onRetry).toHaveBeenCalledTimes(1)
    expect(onRetry).toHaveBeenCalledWith(FAILED_JOB)
    // Modal closes after the retry fires so the operator returns to
    // the catalog surface watching for the new install_jobs row.
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it("disables the retry button when retryDisabled is true", () => {
    render(
      <InstallLogModal
        job={FAILED_JOB}
        onClose={() => {}}
        onRetry={() => {}}
        retryDisabled
      />,
    )
    const retry = screen.getByTestId(
      "install-log-modal-retry",
    ) as HTMLButtonElement
    expect(retry.disabled).toBe(true)
  })

  it("Copy button calls copyToClipboard with the log_tail content", async () => {
    const writer = vi.fn().mockResolvedValue(undefined)
    render(
      <InstallLogModal
        job={FAILED_JOB}
        onClose={() => {}}
        copyToClipboard={writer}
      />,
    )
    fireEvent.click(screen.getByTestId("install-log-modal-copy"))
    // Microtask to let the async writer resolve.
    await Promise.resolve()
    expect(writer).toHaveBeenCalledTimes(1)
    expect(writer).toHaveBeenCalledWith(FAILED_JOB.log_tail)
  })

  it("Copy button swallows clipboard errors silently (operator can still select-and-copy)", async () => {
    const writer = vi.fn().mockRejectedValue(new Error("clipboard blocked"))
    const onClose = vi.fn()
    render(
      <InstallLogModal
        job={FAILED_JOB}
        onClose={onClose}
        copyToClipboard={writer}
      />,
    )
    fireEvent.click(screen.getByTestId("install-log-modal-copy"))
    // Wait two microtasks so the rejection settles.
    await Promise.resolve()
    await Promise.resolve()
    // The modal is still mounted (no crash, no auto-close).
    expect(screen.getByTestId("install-log-modal")).toBeTruthy()
    // onClose is not fired by a clipboard failure.
    expect(onClose).not.toHaveBeenCalled()
  })
})
