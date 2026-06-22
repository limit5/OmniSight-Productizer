# Vendored omnisight-ui (productizer fleet consumption — U4, OP-2284)

Verbatim, re-syncable snapshot of the **launcher-web consumable lib surface**
(components/ + lib/, the U4.1 exports) + **design-system/** from omnisight-ui,
pinned in `PINNED_REF` (ecd43be1430840a18e4fa56ea244c81a9c095c57). Lets the productizer fleet console (U4.5) render
each device's launcher via the REAL shared components (AppGrid/AppTile/
CategoryStrip + manifest/launch), and the productizer brand @theme (U4.2)
consume the same design-tokens.json. NOT the static-SPA app shell.

Re-sync: `./sync.sh <omnisight-ui-checkout>`. Do NOT hand-edit vendored files.
