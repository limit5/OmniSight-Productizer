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
    listCmekWizardProviders: vi.fn(),
    generateCmekWizardPolicy: vi.fn(),
    verifyCmekWizardConnection: vi.fn(),
    completeCmekWizard: vi.fn(),
  }
})

import TenantSettingsPage from "@/app/tenants/[tid]/settings/page"
import { useAuth } from "@/lib/auth-context"
import {
  completeCmekWizard,
  generateCmekWizardPolicy,
  listCmekWizardProviders,
  listTenantMembers,
  verifyCmekWizardConnection,
} from "@/lib/api"

const mockedUseAuth = useAuth as unknown as ReturnType<typeof vi.fn>
const mockedListMembers = listTenantMembers as unknown as ReturnType<typeof vi.fn>
const mockedListProviders = listCmekWizardProviders as unknown as ReturnType<typeof vi.fn>
const mockedGeneratePolicy = generateCmekWizardPolicy as unknown as ReturnType<typeof vi.fn>
const mockedVerify = verifyCmekWizardConnection as unknown as ReturnType<typeof vi.fn>
const mockedComplete = completeCmekWizard as unknown as ReturnType<typeof vi.fn>

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
    policy: { Version: "2012-10-17" },
    policy_json: "{\n  \"Version\": \"2012-10-17\"\n}",
  })
  mockedVerify.mockResolvedValue({
    tenant_id: "t-acme",
    ok: true,
    provider: "aws-kms",
    key_id: "arn:aws:kms:us-east-1:111122223333:key/00000000-0000-0000-0000-000000000000",
    verification_id: "cmekv_abc123",
    algorithm: "fernet",
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
    expect(await screen.findByTestId("cmek-security-tier")).toHaveTextContent("Tier 2")
    expect(screen.getByTestId("cmek-complete-result")).toHaveTextContent("Tier 2 draft")
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
