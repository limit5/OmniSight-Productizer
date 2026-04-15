import { test, expect } from "@playwright/test";

test.describe("K3 — Security response headers", () => {
  test("frontend serves CSP with nonce, no unsafe-eval", async ({ page }) => {
    const response = await page.goto("/");
    expect(response).not.toBeNull();
    const csp = response!.headers()["content-security-policy"] ?? "";

    expect(csp).toContain("script-src");
    expect(csp).not.toContain("unsafe-eval");
    expect(csp).toContain("nonce-");
    expect(csp).toContain("frame-ancestors 'none'");
  });

  test("frontend serves X-Frame-Options DENY", async ({ page }) => {
    const response = await page.goto("/");
    expect(response!.headers()["x-frame-options"]).toBe("DENY");
  });

  test("frontend serves Referrer-Policy strict-origin", async ({ page }) => {
    const response = await page.goto("/");
    expect(response!.headers()["referrer-policy"]).toBe("strict-origin");
  });

  test("frontend serves Permissions-Policy", async ({ page }) => {
    const response = await page.goto("/");
    const pp = response!.headers()["permissions-policy"] ?? "";
    expect(pp).toContain("camera=()");
    expect(pp).toContain("microphone=()");
  });

  test("CSP blocks inline eval", async ({ page }) => {
    await page.goto("/");
    const blocked = await page.evaluate(() => {
      try {
        // eslint-disable-next-line no-eval
        return eval("1+1") === 2 ? "allowed" : "blocked";
      } catch {
        return "blocked";
      }
    });
    expect(blocked).toBe("blocked");
  });

  test("backend API serves security headers", async ({ request }) => {
    const resp = await request.get("/api/v1/health");
    const h = resp.headers();

    expect(h["x-frame-options"]).toBe("DENY");
    expect(h["x-content-type-options"]).toBe("nosniff");
    expect(h["referrer-policy"]).toBe("strict-origin");
    expect(h["content-security-policy"]).toBeDefined();
    expect(h["content-security-policy"]).not.toContain("unsafe-eval");
  });
});
