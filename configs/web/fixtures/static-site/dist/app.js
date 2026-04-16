// W2 #276 — trivial runtime JS for the fixture's primary CTA.
// Intentionally minimal (~200 B) so the bundle-budget gate has real
// asset bytes to sum without dominating the 500 KiB static budget.
document.getElementById("cta")?.addEventListener("click", () => {
  console.log("OmniSight fixture CTA clicked");
});
