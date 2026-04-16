// W2 #276 — Playwright E2E smoke for the static-site fixture.
// Two scenarios per W2 spec:
//   1. homepage renders with the expected <h1>
//   2. primary CTA click triggers the console handler
//
// Executed by the web simulator when Playwright is installed;
// otherwise the simulator degrades the e2e gate to a "mock" pass
// and logs the skip in the report.
import { test, expect } from "@playwright/test";

const BASE_URL = process.env.PLAYWRIGHT_BASE_URL ?? "http://localhost:8080";

test("homepage renders with the W2 fixture heading", async ({ page }) => {
  await page.goto(BASE_URL);
  await expect(page.locator("h1")).toHaveText(/OmniSight Web Simulation Fixture/);
});

test("primary CTA is clickable and logs to console", async ({ page }) => {
  const messages: string[] = [];
  page.on("console", (msg) => messages.push(msg.text()));
  await page.goto(BASE_URL);
  await page.locator("#cta").click();
  expect(messages.some((m) => m.includes("fixture CTA clicked"))).toBeTruthy();
});
