# SKILL-DESKTOP-TAURI Integration Guide

X8 #304. Fourth software-vertical skill pack — validates that the
X0-X4 framework holds on a hybrid Tauri 2.x desktop deliverable
(Rust backend + system-webview frontend) after X5 SKILL-FASTAPI
(#301) proved it on Python, X6 SKILL-GO-SERVICE (#302) on Go, and X7
SKILL-RUST-CLI (#303) on a single-binary Rust CLI.

First skill pack with **two ecosystems** in the same tree (npm at
the project root, cargo under `src-tauri/`) and **platform-specific
installer bundles** as the deliverable rather than a single binary
or container image.

## Render a project

```python
from pathlib import Path
from backend.tauri_scaffolder import ScaffoldOptions, render_project

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

## Output tree

```
my-desktop-app/
├── package.json                      (frontend deps + Vitest scripts)
├── pnpm-lock.yaml                    (placeholder; pnpm install fills)
├── vite.config.ts                    (Tauri-aware Vite config)
├── tsconfig.json
├── tsconfig.node.json
├── index.html                        (Vite entry — SPA shell)
├── README.md
├── Makefile
├── .gitignore
├── .env.example
├── spdx.allowlist.json               (X4 — shared across cargo + npm)
├── src/                              (frontend)
│   ├── main.tsx                      (React) OR main.ts (Vue)
│   ├── App.tsx                       (React) OR App.vue (Vue)
│   ├── App.css
│   ├── useTauri.ts                   (invoke wrapper)
│   └── __tests__/
│       └── App.test.tsx              (Vitest + Testing-Library)
├── src-tauri/                        (Rust backend)
│   ├── Cargo.toml                    (Tauri 2.x deps + cargo-dist metadata)
│   ├── rust-toolchain.toml           (stable 1.76 pin)
│   ├── rustfmt.toml
│   ├── clippy.toml
│   ├── deny.toml                     (cargo-deny config)
│   ├── dist-workspace.toml           (cargo-dist anchor)
│   ├── build.rs                      (tauri_build::build())
│   ├── tauri.conf.json               (bundle + 3-OS targets + updater)
│   ├── capabilities/
│   │   └── default.json              (per-command grants — NO `**`)
│   ├── icons/
│   │   └── README.md                 (operator drops PNG/ICO/ICNS here)
│   └── src/
│       ├── main.rs                   (entry — calls lib::run())
│       ├── lib.rs                    (Builder + plugin registration)
│       └── commands.rs               (#[tauri::command] handlers)
├── scripts/
│   └── check_cov.sh                  (cargo llvm-cov + 75% floor)
└── .github/
    └── workflows/
        └── release.yml               (tauri-action 3-OS release matrix)
```

## Quick start (after render)

```bash
cd my-desktop-app
pnpm install                          # install frontend deps
pnpm tauri dev                        # hot-reload native window
pnpm test                             # Vitest --coverage (frontend)
make test                             # cargo test + llvm-cov 75%
make build                            # pnpm tauri build (current OS)
make dist-plan                        # cargo dist plan (offline X3 hook)
```

## Framework gates validated

| X-series | What the scaffold exercises |
|----------|----------------------------|
| X0 | `linux-x86_64-native` default + 4 sibling profiles via cargo-dist matrix |
| X1 | `cargo llvm-cov --summary-only` + 75% (Rust) AND `vitest --coverage` + 80% (frontend) |
| X2 | desktop-tauri role: strict CSP, capability grants per command, no Tauri 1.x allowlist |
| X3 | `CargoDistAdapter` resolves `src-tauri/Cargo.toml` + `dist-workspace.toml` |
| X4 | Two-ecosystem `software_compliance`: cargo via `cargo-license`, npm via `license-checker` |

## Auto-update channel (operator workflow)

The scaffold ships the wiring; operators provide the keys + endpoint.

```bash
# 1. Generate a minisign key pair (once per project)
pnpm tauri signer generate -w ~/.tauri/myapp.key

# 2. Paste the public key into src-tauri/tauri.conf.json
#    -> bundle.updater.pubkey

# 3. Set the manifest endpoint
#    -> bundle.updater.endpoints = ["https://releases.example.com/{{target}}/{{current_version}}"]

# 4. After each `tauri build`, sign + upload the .sig + the binary +
#    a `latest.json` manifest pointing to them. The plugin verifies
#    minisign on download before applying the update.
```

## Two-ecosystem compliance

`detect_ecosystem` returns `"npm"` at the project root (because of
`package.json`) and `"cargo"` under `src-tauri/` (because of
`Cargo.toml`). The `pilot_report()` helper runs the X4 bundle
against both paths and merges the verdicts — a release is blocked
if either ecosystem fails.

## CI matrix

`.github/workflows/release.yml` is rendered as a 3-OS `tauri-action`
matrix:

```yaml
matrix:
  include:
    - { platform: 'macos-14',       arch: 'arm64' }   # macOS Apple silicon
    - { platform: 'ubuntu-22.04',   arch: 'x86_64' }  # Linux x86_64
    - { platform: 'windows-latest', arch: 'x86_64' }  # Windows x86_64
```

Each job uploads `.dmg + .app`, `.deb + .AppImage + .rpm`, and
`.msi + .exe` artifacts to a draft GitHub Release. Operators promote
the draft to public after smoke-testing.

## Anti-pattern guard rails

The scaffold actively avoids the desktop-tauri role's mandatory
anti-patterns:

- No `"permissions": ["**"]` — the rendered `default.json` enumerates
  each `#[tauri::command]` by name.
- No `unsafe-eval` or wildcard hosts in CSP.
- No raw `app_handle.shell().command(user_input)` — the scaffold's
  `commands.rs` carries a `#[tauri::command]` that takes typed args
  and never spawns shells.
- No `tauri-plugin-fs` exposing `$HOME/**` — the default capability
  scopes filesystem to `$APPDATA/{app}/**`.
- No frontend `fetch('file://...')` calls — IPC is the single channel.
- No Tauri 1.x `tauri.allowlist.*` patterns — the rendered config
  uses the 2.x `app.security` + `capabilities/` model exclusively.
