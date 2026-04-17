# SKILL-DESKTOP-TAURI — X8 #304

Fourth software-vertical skill pack. Follows the X5/X6/X7 pattern but
swaps the deliverable from "Python HTTP server" / "Go microservice" /
"Rust single-binary CLI" to **a hybrid Tauri 2.x desktop app** —
Rust backend + system webview frontend bundled into platform-native
installers for Windows / macOS / Linux with `tauri-plugin-updater`
wired for auto-update.

## Why this skill exists

Priority X built five scaffolding layers — platform profile schema
(X0), software simulate-track (X1), role skills (X2), build & package
adapters (X3), and license / CVE / SBOM compliance (X4). The first
three skill packs proved the framework on three different shapes of
distributable: a long-running HTTP service (X5 FastAPI), a containerised
microservice (X6 Go), and a single-file native binary (X7 Rust CLI).
X8 is the first **dual-language** consumer: Rust + TypeScript-or-vue,
with **platform-specific installer bundles** (msi / dmg / deb /
AppImage / rpm) instead of a single binary, and an **auto-update
channel** that has to ship its own signing key plumbing.

This validates that:

- the X3 cargo-dist adapter accepts a `src-tauri/`-nested Cargo.toml
  (the Rust crate is no longer at the project root)
- the X4 compliance bundle handles a tree where two ecosystems live
  side-by-side (npm at root, cargo under `src-tauri/`)
- the X1 coverage gate runs once per language track (Rust 75% via
  llvm-cov; frontend 80% via Vitest)

## Outputs

A rendered Tauri 2.x project tree that:

- runs `pnpm tauri dev` for hot-reload local development
- builds with `pnpm tauri build` and produces platform-native
  installers (`.msi` on Windows, `.dmg` + `.app` on macOS, `.deb` +
  `.AppImage` + `.rpm` on Linux)
- passes `cargo test --no-fail-fast` + `cargo llvm-cov --summary-only`
  at ≥ 75% coverage on the Rust track
- passes `pnpm test --coverage` (Vitest) at ≥ 80% coverage on the
  frontend track
- ships a strict CSP (`default-src 'self'`) by default — no
  `unsafe-eval`, no wildcards
- registers every `#[tauri::command]` handler via the Tauri 2
  **Capability System** (`src-tauri/capabilities/default.json`) — each
  command requires an explicit grant; no `**` permissions
- carries a `tauri-plugin-updater` config block in `tauri.conf.json`
  with HTTPS endpoint + minisign public-key envelope
- ships a `.github/workflows/release.yml` driving `tauri-action` over
  a 3-platform matrix (windows-latest / macos-14 arm64 / ubuntu-22.04)
- passes the X3 `CargoDistAdapter` validation when pointed at
  `<out_dir>/src-tauri`
- passes the three X4 compliance gates (license / CVE / SBOM) for both
  the npm and cargo ecosystems

## Choice knobs

| Knob           | Values             | Default              |
|----------------|--------------------|----------------------|
| `app_name`     | display name       | `project_name`       |
| `identifier`   | reverse-DNS bundle id | `com.example.<slug>` |
| `frontend`     | `react` \| `vue`   | `react`              |
| `updater`      | `on` \| `off`      | `on`                 |
| `compliance`   | `on` \| `off`      | `on`                 |
| `platform_profile` | one of 5 X0 desktop profiles | `linux-x86_64-native` |

See `configs/skills/skill-desktop-tauri/tasks.yaml` for the DAG each
knob routes through.

## How to render

```python
from pathlib import Path
from backend.tauri_scaffolder import render_project, ScaffoldOptions

outcome = render_project(
    out_dir=Path("/tmp/my-desktop-app"),
    options=ScaffoldOptions(
        project_name="my-desktop-app",
        app_name="My Desktop App",
        identifier="com.example.mydesktopapp",
        frontend="react",          # or "vue"
        updater=True,
        compliance=True,
    ),
)
print(f"Rendered {len(outcome.files_written)} files, {outcome.bytes_written} bytes")
```

## Tauri 2 Capability System

`src-tauri/capabilities/default.json` is the single source of truth
for what the frontend may ask the Rust backend to do. The scaffold
ships a deny-by-default file that grants only the specific
`#[tauri::command]` names declared in `src-tauri/src/commands.rs` —
plus `core:default` for the platform-independent baseline. There is
**no `permissions: ["**"]`** wildcard; that pattern is the role's
mandatory anti-pattern.

## Auto-update wiring

When `updater=true` (default), the scaffold:

1. Adds `tauri-plugin-updater` to the Rust dependency set and registers
   it in `src-tauri/src/lib.rs`.
2. Emits a `[bundle.updater]` block in `tauri.conf.json` with:
   - `endpoints` — operator-supplied HTTPS update-manifest URL
   - `pubkey` — minisign public key (placeholder; operator generates
     `tauri signer generate -w` and pastes the value)
3. Documents the operator-side workflow in `README.md` (key generation,
   manifest format, S3/CloudFront layout).

`updater=false` strips the plugin, the `[bundle.updater]` block, and
the docs section. Useful when shipping the app to an enterprise app
store that owns the update channel.

## desktop-tauri role conformance

The scaffold satisfies the role's mandatory PR-self-audit checklist
(see `configs/roles/software/desktop-tauri.skill.md`):

- `bundle.identifier` is unique and reverse-DNS shaped
- CSP is strict (`default-src 'self'`; no `unsafe-eval`, no wildcards)
- Capability files grant per-command, no `**` wildcard
- Every `#[tauri::command]` has a matching capability entry
- `cargo clippy -- -D warnings` clean
- `cargo fmt --check` clean
- `tsc --noEmit` clean
- Updater pubkey embedded in binary; HTTPS endpoint
- Two-ecosystem license scan (cargo + npm) wired
- No Tauri 1.x allowlist patterns leaked into the 2.x capability files

## CI matrix

`.github/workflows/release.yml` renders to a `tauri-action`-driven
3-OS matrix:

```yaml
matrix:
  include:
    - platform: 'macos-14'        # macOS arm64 (M-series)
    - platform: 'ubuntu-22.04'    # Linux x86_64
    - platform: 'windows-latest'  # Windows x86_64
```

The job uploads release artifacts to a draft GitHub Release; flipping
`tagName` from `app-v__VERSION__` to `nightly` wires the same matrix
into a nightly channel without touching the build steps.
