/**
 * V0 #1 — Contract tests for `app/workspace/[type]/layout.tsx`.
 *
 * The layout is a Next.js Server Component but we test its pure
 * behaviour (the async function, the type guard, metadata shape, and
 * the generated static params). Rendering is exercised by invoking the
 * component as a function and mounting the returned element — no Next
 * runtime required.
 */

import { describe, expect, it, vi } from "vitest"
import { render } from "@testing-library/react"

vi.mock("next/navigation", () => ({
  notFound: vi.fn(() => {
    throw new Error("NEXT_NOT_FOUND")
  }),
}))

import { notFound as notFoundMock } from "next/navigation"
import WorkspaceLayout, {
  WORKSPACE_TYPES,
  isWorkspaceType,
  generateStaticParams,
  generateMetadata,
} from "@/app/workspace/[type]/layout"

const notFoundSpy = notFoundMock as unknown as ReturnType<typeof vi.fn>

describe("WORKSPACE_TYPES", () => {
  it("exposes exactly the three product-line workspaces", () => {
    expect([...WORKSPACE_TYPES].sort()).toEqual(["mobile", "software", "web"])
  })
})

describe("isWorkspaceType", () => {
  it("accepts each known workspace type", () => {
    for (const t of WORKSPACE_TYPES) {
      expect(isWorkspaceType(t)).toBe(true)
    }
  })

  it("rejects unknown / malformed segments", () => {
    for (const v of ["", "WEB", "desktop", "web/", "../web", "null", "undefined"]) {
      expect(isWorkspaceType(v)).toBe(false)
    }
  })
})

describe("generateStaticParams", () => {
  it("returns one entry per workspace type for prerendering", () => {
    const params = generateStaticParams()
    expect(params).toHaveLength(3)
    expect(params.map((p) => p.type).sort()).toEqual(["mobile", "software", "web"])
  })
})

describe("generateMetadata", () => {
  it.each(WORKSPACE_TYPES)("builds per-workspace metadata for %s", async (type) => {
    const meta = await generateMetadata({ params: Promise.resolve({ type }) })
    expect(typeof meta.title).toBe("string")
    expect(meta.title).toMatch(/OmniSight/)
    expect(typeof meta.description).toBe("string")
  })

  it("falls back to a generic title for unknown types", async () => {
    const meta = await generateMetadata({ params: Promise.resolve({ type: "desktop" }) })
    expect(meta.title).toBe("Workspace · OmniSight")
    expect(meta.description).toBeUndefined()
  })
})

describe("WorkspaceLayout (server component)", () => {
  it("renders children wrapped with data-workspace-type for valid types", async () => {
    const element = await WorkspaceLayout({
      children: <div data-testid="workspace-children">hi</div>,
      params: Promise.resolve({ type: "web" }),
    })
    const { getByTestId } = render(element)
    const root = getByTestId("workspace-root")
    expect(root.getAttribute("data-workspace-type")).toBe("web")
    expect(getByTestId("workspace-children").textContent).toBe("hi")
  })

  it.each(["mobile", "software"] as const)(
    "forwards data-workspace-type=%s",
    async (type) => {
      const element = await WorkspaceLayout({
        children: <span>child</span>,
        params: Promise.resolve({ type }),
      })
      const { getByTestId } = render(element)
      expect(getByTestId("workspace-root").getAttribute("data-workspace-type")).toBe(type)
    },
  )

  it("calls notFound() for unknown workspace types", async () => {
    notFoundSpy.mockClear()
    await expect(
      WorkspaceLayout({
        children: <div />,
        params: Promise.resolve({ type: "desktop" }),
      }),
    ).rejects.toThrow("NEXT_NOT_FOUND")
    expect(notFoundSpy).toHaveBeenCalledTimes(1)
  })

  it("calls notFound() for empty / traversal-like segments", async () => {
    for (const bad of ["", "../web", "web/mobile"]) {
      notFoundSpy.mockClear()
      await expect(
        WorkspaceLayout({
          children: <div />,
          params: Promise.resolve({ type: bad }),
        }),
      ).rejects.toThrow("NEXT_NOT_FOUND")
      expect(notFoundSpy).toHaveBeenCalledTimes(1)
    }
  })
})
