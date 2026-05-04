/**
 * KS.2.1 — /tenants/{tid}/settings CMEK wizard wiring.
 */

import React, { Suspense } from "react"
import { describe, expect, it, vi, beforeEach } from "vitest"
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  act,
} from "@testing-library/react"

vi.mock("@/lib/auth-context", () => ({
  useAuth: vi.fn(),
}))

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>()
  return {
    ...actual,
    listTenantMembers: vi.fn(),
    getCmekSettingsStatus: vi.fn(),
    listCmekWizardProviders: vi.fn(),
    generateCmekWizardPolicy: vi.fn(),
    saveCmekWizardKeyId: vi.fn(),
    verifyCmekWizardConnection: vi.fn(),
    completeCmekWizard: vi.fn(),
  }
})

import TenantSettingsPage from "@/app/tenants/[tid]/settings/page"
import { useAuth } from "@/lib/auth-context"
import {
  completeCmekWizard,
  generateCmekWizardPolicy,
  getCmekSettingsStatus,
  listCmekWizardProviders,
  listTenantMembers,
  saveCmekWizardKeyId,
  verifyCmekWizardConnection,
} from "@/lib/api"

const mockedUseAuth = useAuth as unknown as ReturnType<typeof vi.fn>
const mockedListMembers = listTenantMembers as unknown as ReturnType<typeof vi.fn>
const mockedGetStatus = getCmekSettingsStatus as unknown as ReturnType<typeof vi.fn>
const mockedListProviders = listCmekWizardProviders as unknown as ReturnType<typeof vi.fn>
const mockedGeneratePolicy = generateCmekWizardPolicy as unknown as ReturnType<typeof vi.fn>
const mockedSaveKeyId = saveCmekWizardKeyId as unknown as ReturnType<typeof vi.fn>
const mockedVerify = verifyCmekWizardConnection as unknown as ReturnType<typeof vi.fn>
const mockedComplete = completeCmekWizard as unknown as ReturnType<typeof vi.fn>
const clipboardWriteText = vi.fn()

function makeParams(tid = "t-acme") {
  return Promise.resolve({ tid })
}

async function renderPage() {
  await act(async () => {
    render(
      <Suspense fallback={null}>
        <TenantSettingsPage params={makeParams()} />
      </Suspense>,
    )
  })
}

beforeEach(() => {
  cleanup()
  vi.clearAllMocks()
  Object.assign(navigator, { clipboard: { writeText: clipboardWriteText } })
  mockedUseAuth.mockReturnValue({
    user: {
      id: "u-admin0001",
      email: "admin@example.com",
      name: "Admin",
      role: "admin",
      enabled: true,
      tenant_id: "t-acme",
    },
    authMode: "session",
    loading: false,
  })
  mockedListMembers.mockResolvedValue({
    tenant_id: "t-acme",
    status_filter: "active",
    count: 0,
    members: [],
  })
  mockedGetStatus.mockResolvedValue({
    tenant_id: "t-acme",
    security_tier: "tier-2",
    kms_health: "healthy",
    revoke_status: "clear",
    provider: "aws-kms",
    key_id: "arn:aws:kms:us-east-1:111122223333:key/00000000-0000-0000-0000-000000000000",
    reason: "describe_ok",
    raw_state: "Enabled",
    checked_at: 1760000000,
  })
  mockedListProviders.mockResolvedValue({
    tenant_id: "t-acme",
    providers: [
      {
        provider: "aws-kms",
        label: "AWS KMS",
        key_id_label: "KMS key ARN",
        key_id_example: "arn:aws:kms:us-east-1:111122223333:key/00000000-0000-0000-0000-000000000000",
        policy_target_label: "OmniSight IAM role ARN",
        policy_target_example: "arn:aws:iam::444455556666:role/OmniSightCMEKAccess",
      },
      {
        provider: "gcp-kms",
        label: "Google Cloud KMS",
        key_id_label: "CryptoKey resource id",
        key_id_example: "projects/acme-prod/locations/us/keyRings/omnisight/cryptoKeys/tenant-tier2",
        policy_target_label: "OmniSight service account",
        policy_target_example: "serviceAccount:omnisight@example.iam.gserviceaccount.com",
      },
      {
        provider: "vault-transit",
        label: "HashiCorp Vault Transit",
        key_id_label: "Transit key name",
        key_id_example: "transit/omnisight-tenant-tier2",
        policy_target_label: "Vault entity or token display name",
        policy_target_example: "omnisight-cmek",
      },
    ],
  })
  mockedGeneratePolicy.mockResolvedValue({
    tenant_id: "t-acme",
    provider: "aws-kms",
    policy: {
      Version: "2012-10-17",
      Statement: [
        {
          Sid: "AllowOmniSightDescribeTenantKey",
          Action: "kms:DescribeKey",
        },
        {
          Sid: "AllowOmniSightTenantEnvelopeEncryption",
          Action: ["kms:Encrypt", "kms:Decrypt"],
        },
      ],
    },
    policy_json:
      "{\n  \"Statement\": [\n    {\n      \"Action\": \"kms:DescribeKey\"\n    },\n    {\n      \"Action\": [\n        \"kms:Encrypt\",\n        \"kms:Decrypt\"\n      ]\n    }\n  ],\n  \"Version\": \"2012-10-17\"\n}",
  })
  mockedSaveKeyId.mockResolvedValue({
    tenant_id: "t-acme",
    provider: "aws-kms",
    key_id: "arn:aws:kms:us-east-1:111122223333:key/00000000-0000-0000-0000-000000000000",
    accepted: true,
  })
  mockedVerify.mockResolvedValue({
    tenant_id: "t-acme",
    ok: true,
    provider: "aws-kms",
    key_id: "arn:aws:kms:us-east-1:111122223333:key/00000000-0000-0000-0000-000000000000",
    verification_id: "cmekv_abc123",
    operation: "encrypt-decrypt",
    algorithm: "AES-256-GCM",
    wrap_algorithm: "fernet",
    elapsed_ms: 1.2,
    live_provider_checked: false,
  })
  mockedComplete.mockResolvedValue({
    tenant_id: "t-acme",
    security_tier: "tier-2",
    provider: "aws-kms",
    key_id: "arn:aws:kms:us-east-1:111122223333:key/00000000-0000-0000-0000-000000000000",
    verification_id: "cmekv_abc123",
    config_status: "draft",
    persisted: false,
  })
})

describe("/tenants/{tid}/settings — CMEK wizard", () => {
  it("renders the Security Tier selector, revoke status, and KMS health badge", async () => {
    await renderPage()

    fireEvent.click(screen.getByTestId("settings-tab-security"))

    expect(await screen.findByTestId("cmek-security-tier-selector")).toHaveValue("tier-2")
    expect(screen.getByTestId("cmek-revoke-status")).toHaveAttribute("data-status", "clear")
    expect(screen.getByTestId("cmek-kms-health-badge")).toHaveAttribute("data-status", "healthy")

    fireEvent.change(screen.getByTestId("cmek-security-tier-selector"), {
      target: { value: "tier-1" },
    })

    expect(screen.getByTestId("cmek-security-tier")).toHaveTextContent(
      "Tier 1 · OmniSight-managed KEK",
    )
    expect(screen.getByTestId("cmek-tier1-selected")).toHaveTextContent(
      "Tier 1 selected",
    )
  })

  it("shows revoked KMS status when the health endpoint reports revoked access", async () => {
    mockedGetStatus.mockResolvedValueOnce({
      tenant_id: "t-acme",
      security_tier: "tier-2",
      kms_health: "revoked",
      revoke_status: "revoked",
      provider: "gcp-kms",
      key_id: "projects/acme-prod/locations/us/keyRings/r/cryptoKeys/k",
      reason: "key_disabled",
      raw_state: "DISABLED",
      checked_at: 1760000001,
    })

    await renderPage()

    fireEvent.click(screen.getByTestId("settings-tab-security"))
    expect(await screen.findByTestId("cmek-revoke-status")).toHaveAttribute(
      "data-status",
      "revoked",
    )
    expect(screen.getByTestId("cmek-kms-health-badge")).toHaveAttribute(
      "data-status",
      "revoked",
    )
    expect(screen.getByTestId("cmek-revoke-status")).toHaveTextContent(
      "Revoked",
    )
  })

  it("runs policy generation, verify, and done steps", async () => {
    await renderPage()

    fireEvent.click(screen.getByTestId("settings-tab-security"))
    expect(await screen.findByTestId("cmek-security-tab")).toBeInTheDocument()
    expect(await screen.findByTestId("cmek-provider-aws-kms")).toBeInTheDocument()
    fireEvent.click(screen.getByTestId("cmek-provider-continue"))

    fireEvent.click(screen.getByTestId("cmek-generate-policy"))
    await waitFor(() => {
      expect(mockedGeneratePolicy).toHaveBeenCalledWith("t-acme", {
        provider: "aws-kms",
        principal: "arn:aws:iam::444455556666:role/OmniSightCMEKAccess",
        key_id: "arn:aws:kms:us-east-1:111122223333:key/00000000-0000-0000-0000-000000000000",
      })
    })
    expect(await screen.findByText(/2012-10-17/)).toBeInTheDocument()
    fireEvent.click(screen.getByTestId("cmek-copy-policy-json"))
    await waitFor(() => {
      expect(clipboardWriteText).toHaveBeenCalledWith(
        expect.stringContaining('"kms:DescribeKey"'),
      )
    })
    expect(screen.getByTestId("cmek-copy-policy-json")).toHaveTextContent("Copied")

    fireEvent.click(screen.getByTestId("cmek-save-key-id"))
    await waitFor(() => {
      expect(mockedSaveKeyId).toHaveBeenCalledWith("t-acme", {
        provider: "aws-kms",
        key_id: "arn:aws:kms:us-east-1:111122223333:key/00000000-0000-0000-0000-000000000000",
      })
    })
    expect(await screen.findByTestId("cmek-key-id-accepted")).toHaveTextContent(
      "Accepted",
    )

    fireEvent.click(screen.getByTestId("cmek-verify"))
    expect(await screen.findByTestId("cmek-verify-result")).toHaveTextContent("cmekv_abc123")

    fireEvent.click(screen.getByTestId("cmek-complete"))
    await waitFor(() => {
      expect(mockedComplete).toHaveBeenCalledWith("t-acme", {
        provider: "aws-kms",
        key_id: "arn:aws:kms:us-east-1:111122223333:key/00000000-0000-0000-0000-000000000000",
        verification_id: "cmekv_abc123",
      })
    })
    expect(await screen.findByTestId("cmek-security-tier")).toHaveTextContent(
      "Tier 2 · Customer-managed KEK",
    )
    expect(screen.getByTestId("cmek-step-5")).toHaveTextContent("Done")
    expect(screen.getByTestId("cmek-complete-result")).toHaveTextContent(
      "Step 5 done · UI switched to Tier 2",
    )
  })

  it("keeps Step 1 scoped to AWS, GCP, and Vault provider selection", async () => {
    await renderPage()

    fireEvent.click(screen.getByTestId("settings-tab-security"))
    expect(await screen.findByTestId("cmek-security-tab")).toBeInTheDocument()

    const aws = await screen.findByTestId("cmek-provider-aws-kms")
    const gcp = screen.getByTestId("cmek-provider-gcp-kms")
    const vault = screen.getByTestId("cmek-provider-vault-transit")

    expect(aws).toHaveAttribute("role", "radio")
    expect(gcp).toHaveAttribute("role", "radio")
    expect(vault).toHaveAttribute("role", "radio")
    expect(aws).toHaveAttribute("aria-checked", "true")
    expect(screen.getByTestId("cmek-provider-continue")).toHaveTextContent("Use AWS KMS")
    expect(screen.queryByTestId("cmek-generate-policy")).not.toBeInTheDocument()

    fireEvent.click(gcp)
    expect(gcp).toHaveAttribute("aria-checked", "true")
    expect(aws).toHaveAttribute("aria-checked", "false")
    expect(screen.getByTestId("cmek-provider-continue")).toHaveTextContent("Use Google Cloud KMS")

    fireEvent.click(vault)
    expect(vault).toHaveAttribute("aria-checked", "true")
    expect(gcp).toHaveAttribute("aria-checked", "false")
    expect(screen.getByTestId("cmek-provider-continue")).toHaveTextContent("Use HashiCorp Vault Transit")

    fireEvent.click(screen.getByTestId("cmek-provider-continue"))
    expect(screen.getByTestId("cmek-principal-input")).toHaveValue("omnisight-cmek")
    expect(screen.getByTestId("cmek-key-id-input")).toHaveValue("transit/omnisight-tenant-tier2")
    expect(screen.getByTestId("cmek-step-1")).toHaveTextContent("Provider")
  })
})
