/**
 * H3 row 1522: MetricSparkline contract — the inline 60-pt SVG line
 * chart used by HostDevicePanel for each live host metric.
 */

import { describe, expect, it } from "vitest"
import { render } from "@testing-library/react"

import { MetricSparkline } from "@/components/omnisight/host-device-panel"

describe("MetricSparkline", () => {
  it("renders an empty placeholder when fewer than 2 points are available", () => {
    const { getByTestId } = render(
      <MetricSparkline values={[]} color="red" testId="spark" />,
    )
    const el = getByTestId("spark")
    expect(el.tagName.toLowerCase()).toBe("div")
    expect(el.getAttribute("data-empty")).toBe("true")
    expect(el.textContent).toBe("—")
  })

  it("renders an SVG polyline with one point per value once 2+ points are present", () => {
    const { getByTestId } = render(
      <MetricSparkline
        values={[10, 20, 30]}
        color="hsl(200,80%,50%)"
        domainMax={100}
        width={60}
        height={20}
        testId="spark"
      />,
    )
    const svg = getByTestId("spark")
    expect(svg.tagName.toLowerCase()).toBe("svg")
    expect(svg.getAttribute("data-points")).toBe("3")
    expect(svg.getAttribute("width")).toBe("60")
    expect(svg.getAttribute("height")).toBe("20")
    const poly = svg.querySelector("polyline")
    expect(poly).not.toBeNull()
    const pts = poly!.getAttribute("points") ?? ""
    // Three coordinate pairs separated by spaces.
    expect(pts.split(" ")).toHaveLength(3)
    expect(poly!.getAttribute("stroke")).toBe("hsl(200,80%,50%)")
  })

  it("clamps values to [0, domainMax] so spikes do not blow out the y-axis", () => {
    const { getByTestId } = render(
      <MetricSparkline
        values={[0, 50, 200]} // 200 > domainMax → clamped to 100
        color="red"
        domainMax={100}
        width={40}
        height={10}
        testId="spark"
      />,
    )
    const svg = getByTestId("spark")
    const points = svg.querySelector("polyline")!.getAttribute("points")!
    // Last point's y should clamp to 0 (top of chart) when value ≥ domainMax.
    const lastPair = points.split(" ").at(-1)!
    const [, y] = lastPair.split(",").map(Number)
    expect(y).toBe(0)
  })

  it("auto-fits when domainMax is null (loadavg / container count case)", () => {
    const { getByTestId } = render(
      <MetricSparkline
        values={[1, 2, 4]}
        color="blue"
        domainMax={null}
        width={40}
        height={10}
        testId="spark"
      />,
    )
    const svg = getByTestId("spark")
    const pairs = svg
      .querySelector("polyline")!
      .getAttribute("points")!
      .split(" ")
      .map((p) => p.split(",").map(Number))
    // Min 0, max 4 → 4 sits at top (y=0), 1 sits 25% down from top (y=2.5).
    expect(pairs[2][1]).toBe(0)
    expect(pairs[0][1]).toBeCloseTo(7.5, 5)
  })
})
