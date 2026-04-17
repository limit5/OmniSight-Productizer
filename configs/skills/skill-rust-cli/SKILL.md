# SKILL-RUST-CLI — X7 #303

Third software-vertical skill pack. Follows the X5/X6 pattern but
swaps the deliverable from "HTTP server in a container" to
**single-file native CLI binary** — re-exercising the X0-X4
framework on a Rust toolchain (cargo + rustc + cargo-dist) with a
different packaging route than Docker/Helm/goreleaser.

## Why this skill exists

Priority X built five scaffolding layers — platform profile schema
(X0), software simulate-track (X1), role skills (X2), build & package
adapters (X3), and license / CVE / SBOM compliance (X4). X5 proved
those layers hold on Python/FastAPI. X6 proved them on Go/Gin. X7 is
the first non-service consumer: it proves the framework is
deliverable-agnostic and wires the **cargo-dist** adapter (new in
X3) into a real end-to-end path.

## Outputs

A rendered Rust 2021 project tree that:

- builds with `cargo build --release` and produces `target/release/<name>`
  on any of the five X0 profiles
- passes `cargo test --no-fail-fast` + `cargo llvm-cov --summary-only`
  at ≥ 75% coverage — matches `COVERAGE_THRESHOLDS["rust"]` in
  `backend.software_simulator`
- uses clap 4.x derive macros for argument parsing (no hand-rolled
  `std::env::args()`) with automatic `--help` / `--version` /
  completions
- uses anyhow for error context throughout `main` / commands; library
  modules surface `Result<_, E>` concretely
- runs a tokio 1.x multi-thread runtime (async CLI — ready for HTTP
  fetch / fs watch / parallel subcommand work out of the gate)
- emits a `dist-workspace.toml` + `Cargo.toml` metadata block that
  `cargo-dist` (via `backend.build_adapters.CargoDistAdapter`)
  packages into release archives for the 5 target triples
- passes the three X4 compliance gates (SPDX license / CVE scan / SBOM
  emit — `cargo` ecosystem via `cargo-license` + `Cargo.toml` fallback)

## Choice knobs

| Knob           | Values                            | Default              |
|----------------|-----------------------------------|----------------------|
| `bin_name`     | crate binary name                | `project_name` slug  |
| `runtime`      | `tokio` \| `sync`                 | `tokio`              |
| `subcommands`  | subset of `init,run,status,version` | all four           |
| `completions`  | `on` \| `off`                     | `on`                 |
| `cross_targets`| subset of 5 target triples        | all five (see below) |
| `compliance`   | `on` \| `off`                     | `on`                 |

Default `cross_targets`:
- `x86_64-unknown-linux-gnu`
- `aarch64-unknown-linux-gnu`
- `x86_64-pc-windows-msvc`
- `aarch64-apple-darwin`
- `x86_64-apple-darwin`

See `configs/skills/skill-rust-cli/tasks.yaml` for the DAG each knob
routes through.

## How to render

```python
from pathlib import Path
from backend.rust_cli_scaffolder import render_project, ScaffoldOptions

outcome = render_project(
    out_dir=Path("/tmp/my-cli"),
    options=ScaffoldOptions(
        project_name="my-cli",
        bin_name="mycli",
        runtime="tokio",
        completions=True,
        compliance=True,
    ),
)
print(f"Rendered {len(outcome.files_written)} files, {outcome.bytes_written} bytes")
```

## cargo-dist wiring

The rendered `dist-workspace.toml` + `[workspace.metadata.dist]`
block in `Cargo.toml` target Linux x86_64/arm64 (gnu), Windows MSVC,
and macOS arm64/x86_64 with `--release` profile, `strip = true`, and
`lto = "fat"` — matching the backend-rust role skill's "CLI binary
≤ 8 MiB" budget. The X3 `CargoDistAdapter` validates the config via
`cargo dist plan` (offline) and produces archives via
`cargo dist build --artifacts=all` when `push=False`.

## cli-tooling role conformance

The scaffold implements the three cli-tooling role mandatory
conventions:

1. `--version` prints `{name} {semver} ({git_sha})` where `git_sha`
   is injected at build time via `build.rs` (short-form, 7 chars;
   `unknown` when not a git tree).
2. `--help` is clap-generated (with examples section via
   `#[command(after_help = ...)]`).
3. `--json` flag toggles machine-readable output on every subcommand
   that prints data; stderr vs stdout separation follows the
   role rule (logs → stderr, data → stdout).

Exit codes: `0` success / `1` generic error / `2` usage error
(clap's default on parse failure) / `130` on SIGINT (via a tokio
ctrl-c handler that triggers graceful shutdown).

Shell completions ship for bash / zsh / fish / pwsh via
`clap_complete`; the `completions` subcommand emits them on demand
so release archives stay slim.
