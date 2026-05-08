"""ADR-0005 governance package — Tier S/M/L/X authority enforcement.

Houses the back-end half of the four-layer protection model:

* Layer 1 (path force-upgrade) — implemented by the Gerrit
  ``patchset-created`` hook (OP-805 / G3) which calls a future
  ``tier_classifier.classify_paths`` here in-process or over HTTPS.
* Layer 2 (Tier S whitelist) — same classifier; deny-by-default.
* Layer 3 (reviewer monotonicity) — Gerrit submit-rule (G4).
* Layer 4 (per-agent misclassification cooldown) — :mod:`.tier_cooldown`
  in this package (OP-807 / G5).

Only :mod:`.tier_cooldown` is implemented right now; the classifier
module (``tier_classifier``) lands separately in G2's ticket.
"""
