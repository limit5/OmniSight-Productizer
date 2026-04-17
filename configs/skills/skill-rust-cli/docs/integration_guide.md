# SKILL-RUST-CLI Integration Guide

X7 #303. Third software-vertical skill pack — validates that the
X0-X4 framework holds on Rust (cargo + rustc + cargo-dist) after
X5 SKILL-FASTAPI (#301) proved it on Python and X6 SKILL-GO-SERVICE
(#302) proved it on Go. First non-service deliverable — the output
is a single-file native binary, not a long-running server.

## Render a project

```python
from pathlib import Path
from backend.rust_cli_scaffolder import ScaffoldOptions, render_project

outcome = render_project(
    out_dir=Path("/tmp/my-cli"),
    options=ScaffoldOptions(
        project_name="my-cli",
        bin_name="mycli",
        runtime="tokio",          # or "sync"
        completions=True,
        compliance=True,
    ),
)
print(f"Rendered {len(outcome.files_written)} files, {outcome.bytes_written} bytes")
```

## Output tree

```
my-cli/
├── Cargo.toml                (crate metadata + deps + [workspace.metadata.dist])
├── rust-toolchain.toml       (stable 1.76 pin)
├── dist-workspace.toml       (cargo-dist anchor)
├── build.rs                  (injects BUILD_GIT_SHA at compile time)
├── rustfmt.toml
├── clippy.toml
├── deny.toml                 (cargo-deny license/ban/source/advisory config)
├── spdx.allowlist.json       (X4 compliance — denies GPL/AGPL)
├── Makefile
├── .env.example
├── .gitignore
├── README.md
├── src/
│   ├── main.rs               (tokio runtime + ExitCode routing)
│   ├── cli.rs                (clap derive Command tree)
│   ├── error.rs              (thiserror + anyhow bridge)
│   ├── logging.rs            (tracing-subscriber, TTY-gated ANSI)
│   └── commands/
│       ├── mod.rs
│       ├── init.rs
│       ├── run.rs
│       ├── status.rs
│       ├── version.rs
│       └── completions.rs
├── tests/
│   └── cli_integration.rs    (assert_cmd — version / --json / exit-code contract)
└── scripts/
    └── check_cov.sh          (cargo llvm-cov + 75% floor)
```

## Quick start (after render)

```bash
cd my-cli
cargo build                    # quick compile
make test                      # cargo test + cargo llvm-cov 75% floor
cargo run -- --help            # see the surface
cargo run -- version --json    # machine-readable
make dist-plan                 # cargo dist plan (offline)
```

## Framework gates validated

| X-series | What the scaffold exercises |
|----------|----------------------------|
| X0 | `linux-x86_64-native` (plus 4 siblings via cargo-dist matrix) |
| X1 | `cargo llvm-cov --summary-only` + 75% floor (COVERAGE_THRESHOLDS["rust"]) |
| X2 | cli-tooling role (Rust row) + backend-rust role anti-patterns |
| X3 | `CargoDistAdapter` resolves `Cargo.toml` + `dist-workspace.toml` |
| X4 | SPDX allowlist + CVE scan + SBOM via `backend.software_compliance` |

## cargo-dist

`[workspace.metadata.dist]` in Cargo.toml targets five triples —
Linux x86_64/arm64 (gnu), Windows MSVC, macOS arm64/x86_64 — with
`cargo-dist-version` pinned for reproducibility. The X3
`CargoDistAdapter` runs `cargo dist build` when invoked, or
`cargo dist plan` offline for validation.

## cli-tooling role conformance cheat-sheet

- `--version` → `{name} {semver} ({git_sha})` (build.rs injects SHA)
- `--help` → clap-generated with `after_help` examples
- `--json` → every data subcommand serialises via serde_json to stdout
- exit `0 / 1 / 2 / 130` — main returns ExitCode; clap owns 2; tokio ctrl_c owns 130
- logs → stderr (tracing-subscriber with `.with_writer(std::io::stderr)`)
- data → stdout (`println!`)
- ANSI → gated on `is_terminal()` and disabled under `--json`
- completions → `clap_complete` bash/zsh/fish/pwsh
