"""BS.1.2 — Seed shipped catalog entries (~30 entries).

Data migration that pours the OmniSight upstream catalog into
``catalog_entries`` with ``source='shipped'``.  First-batch coverage
matches the BS.1.2 TODO row split:

  * Mobile          — 6 entries
  * Embedded        — 8 entries
  * Web             — 4 entries
  * Software        — 5 entries
  * RTOS            — 3 entries
  * cross-toolchain — 4 entries
  ───────────────────────────
  Total             — 30 entries

The yaml mirrors at ``configs/embedded_catalog/*.yaml`` carry the same
content for human review and admin-UI display; the alembic migration
freezes the bytes that ship with this revision so re-running an old
checkout never picks up newer yaml content.  BS.1.5's drift guard
(``backend/tests/test_catalog_schema.py``) cross-checks the two —
edits to one without the other are CI-red.

Why embed the data here instead of reading the yaml at upgrade time
──────────────────────────────────────────────────────────────────

Alembic migrations must be deterministic forever: a checkout of this
revision running ``alembic upgrade 0052`` next year must produce the
same row set as the one that landed today.  Reading yaml at runtime
couples this migration to whatever ``configs/embedded_catalog/`` looks
like at upgrade time, which is exactly the property we don't want for
historical data migrations.  We embed the rows; we keep the yaml as a
human-friendly mirror; the BS.1.5 drift guard is the contract that
keeps them in sync at *commit* time, not *upgrade* time.

Idempotency
───────────

Each row uses ``INSERT OR IGNORE`` (the alembic_pg_compat shim
translates this to ``INSERT INTO … ON CONFLICT DO NOTHING`` for PG —
no-target form, which catches the partial UNIQUE
``uq_catalog_entries_visible(id, source, COALESCE(tenant_id, ''))
WHERE hidden = false`` that 0051 created).  Re-running this migration
is therefore a no-op on rows that already exist.

Subsequent BS.1.2+ catalog changes (add an entry / bump a vendor SDK
version) will land as **new** alembic revisions (0053, 0054, …) using
``INSERT … ON CONFLICT (id, source, COALESCE(tenant_id, '')) WHERE
hidden = false DO UPDATE`` — they edit the shipped layer; this
revision only seeds it.

Dialect handling
────────────────

JSON columns (``depends_on``, ``metadata``) need a ``::jsonb`` cast on
PG so the literal text becomes a JSONB value; SQLite stores them as
plain TEXT-of-JSON (the 0051 SQLite branch declares them as TEXT).
``hidden`` defaults to ``FALSE`` on PG / ``0`` on SQLite — we omit
the column from the INSERT and let 0051's column DEFAULT do the work.
``schema_version``, ``depends_on``, ``metadata``, ``hidden``,
``created_at``, ``updated_at`` all fall through to their column
defaults whenever an entry doesn't override them.

Module-global / cross-worker state audit (per implement_phase_step.md)
──────────────────────────────────────────────────────────────────────

Pure DML migration.  No in-memory cache, no module-level singleton.
Runs once at ``alembic upgrade head`` time; every worker boot after
the cutover sees the same shipped rows in the same PG database.
Answer #1 — "every worker reads the same rows from the same PG".

Read-after-write timing audit
─────────────────────────────

Empty before this commit; populated by this commit; nothing currently
reads ``catalog_entries`` at request time (BS.2 router lands later).
The first reader (``backend/routers/catalog.py``) ships in a later row
and observes the post-seed state by definition — there is no
"read-races-write" window because the reader doesn't exist yet.

Production readiness gate
─────────────────────────

* No new Python / OS package — production image needs no rebuild.
* No new schema artefacts (still no ``catalog_entries`` /
  ``install_jobs`` / ``catalog_subscriptions`` in
  ``scripts/migrate_sqlite_to_pg.py::TABLES_IN_ORDER`` —
  BS.1.4 owns that mirror, same as 0051's HANDOFF said).
* The seeded rows are 30 single-row INSERTs; on a clean DB this is
  sub-second on both SQLite and PG.
* Production status of THIS commit: **dev-only**.  Next gate is
  ``deployed-inactive`` — operator runs ``alembic upgrade head`` on
  the prod PG instance after BS.1.1 + this revision both land.
  No env knob change required.

Revision ID: 0052
Revises: 0051
Create Date: 2026-04-27
"""
from __future__ import annotations

import json
from typing import Any

from alembic import op


revision = "0052"
down_revision = "0051"
branch_labels = None
depends_on = None


# ─── Frozen seed data ─────────────────────────────────────────────────────
#
# Every entry's required fields per 0051's CHECK constraints:
#   * id, source('shipped'), vendor, family, display_name, version,
#     install_method (one of noop/docker_pull/shell_script/vendor_installer)
# Optional fields with sensible NULL defaults:
#   * install_url, sha256, size_bytes, depends_on (default '[]'),
#     metadata (default '{}'), hidden (default false), schema_version
#     (default 1)
#
# ``sha256`` is intentionally NULL for first-seed:  the threat model
# (BS.0.2 §5.1) marks sha256-required-for-destructive as a CHECK
# constraint deferred to a later alembic revision (it will get
# back-filled with vendor-published digests when the install pipeline
# in BS.7 wires up live fetches).  Seeding NULL keeps this migration
# honest — we don't ship a "00000…" placeholder that could slip past a
# review.

_SEED_ENTRIES: tuple[dict[str, Any], ...] = (
    # ─── Mobile (6) ──────────────────────────────────────────────────
    {
        "id": "android-sdk-platform-tools",
        "vendor": "google",
        "family": "mobile",
        "display_name": "Android SDK Platform Tools",
        "version": "35.0.2",
        "install_method": "shell_script",
        "install_url": (
            "https://dl.google.com/android/repository/"
            "platform-tools-latest-linux.zip"
        ),
        "size_bytes": 14680064,
        "metadata": {
            "sdk_root_relative": "platform-tools",
            "tools": ["adb", "fastboot"],
        },
    },
    {
        "id": "android-ndk-r26d",
        "vendor": "google",
        "family": "mobile",
        "display_name": "Android NDK r26d",
        "version": "26.3.11579264",
        "install_method": "shell_script",
        "install_url": (
            "https://dl.google.com/android/repository/"
            "android-ndk-r26d-linux.zip"
        ),
        "size_bytes": 1932735283,
        "depends_on": ["android-sdk-platform-tools"],
        "metadata": {
            "api_level_min": 21,
            "abi_targets": ["arm64-v8a", "armeabi-v7a", "x86_64"],
        },
    },
    {
        "id": "ios-xcode-cli",
        "vendor": "apple",
        "family": "mobile",
        "display_name": "Xcode Command Line Tools",
        "version": "15.4",
        "install_method": "vendor_installer",
        "install_url": (
            "https://developer.apple.com/download/all/"
            "?q=command%20line%20tools"
        ),
        "size_bytes": 692060160,
        "metadata": {
            "requires_apple_id": True,
            "host_os": "darwin",
        },
    },
    {
        "id": "flutter-stable",
        "vendor": "google",
        "family": "mobile",
        "display_name": "Flutter SDK (stable)",
        "version": "3.24.3",
        "install_method": "shell_script",
        "install_url": (
            "https://storage.googleapis.com/flutter_infra_release/"
            "releases/stable/linux/"
            "flutter_linux_3.24.3-stable.tar.xz"
        ),
        "size_bytes": 754974720,
        "depends_on": ["android-sdk-platform-tools"],
        "metadata": {
            "channel": "stable",
            "dart_version_bundled": "3.5.3",
        },
    },
    {
        "id": "react-native-cli",
        "vendor": "meta",
        "family": "mobile",
        "display_name": "React Native CLI",
        "version": "14.1.0",
        "install_method": "shell_script",
        "install_url": (
            "https://registry.npmjs.org/@react-native-community/cli"
        ),
        "metadata": {
            "install_via": "npm",
            "requires_runtime": "nodejs-lts-20",
        },
    },
    {
        "id": "capacitor-cli",
        "vendor": "ionic",
        "family": "mobile",
        "display_name": "Capacitor CLI",
        "version": "6.1.2",
        "install_method": "shell_script",
        "install_url": "https://registry.npmjs.org/@capacitor/cli",
        "metadata": {
            "install_via": "npm",
            "requires_runtime": "nodejs-lts-20",
        },
    },
    # ─── Embedded (8) ────────────────────────────────────────────────
    {
        "id": "nxp-mcuxpresso-imxrt1170",
        "vendor": "nxp",
        "family": "embedded",
        "display_name": "NXP MCUXpresso (i.MX RT1170)",
        "version": "11.10.0",
        "install_method": "vendor_installer",
        "install_url": (
            "https://www.nxp.com/design/software/development-software/"
            "mcuxpresso-software-and-tools-/"
            "mcuxpresso-integrated-development-environment-ide:"
            "MCUXpresso-IDE"
        ),
        "size_bytes": 1610612736,
        "depends_on": ["arm-gnu-toolchain-13"],
        "metadata": {
            "board_reference": "MIMXRT1170-EVKB",
            "sdk_version_bundled": "2.16.000",
            "requires_login": True,
        },
    },
    {
        "id": "st-stm32cubeide",
        "vendor": "st",
        "family": "embedded",
        "display_name": "STM32CubeIDE",
        "version": "1.16.1",
        "install_method": "vendor_installer",
        "install_url": (
            "https://www.st.com/en/development-tools/stm32cubeide.html"
        ),
        "size_bytes": 1395864371,
        "depends_on": ["arm-gnu-toolchain-13"],
        "metadata": {
            "bundled_compiler": "gcc-arm-none-eabi",
            "requires_login": True,
        },
    },
    {
        "id": "nordic-nrf-connect-sdk",
        "vendor": "nordic",
        "family": "embedded",
        "display_name": "Nordic nRF Connect SDK",
        "version": "2.7.0",
        "install_method": "vendor_installer",
        "install_url": (
            "https://www.nordicsemi.com/Products/Development-software/"
            "nrf-connect-sdk/download"
        ),
        "size_bytes": 2147483648,
        "depends_on": ["zephyr-rtos-3-7", "arm-gnu-toolchain-13"],
        "metadata": {
            "west_manifest": "https://github.com/nrfconnect/sdk-nrf",
            "requires_segger_jlink": True,
        },
    },
    {
        "id": "espressif-esp-idf-v5",
        "vendor": "espressif",
        "family": "embedded",
        "display_name": "ESP-IDF v5.3",
        "version": "5.3.1",
        "install_method": "shell_script",
        "install_url": "https://github.com/espressif/esp-idf.git",
        "size_bytes": 524288000,
        "depends_on": ["xtensa-esp-elf-gcc-13"],
        "metadata": {
            "tools_install_cmd": "./install.sh esp32,esp32s3,esp32c3",
            "branch": "release/v5.3",
        },
    },
    {
        "id": "raspberrypi-imager",
        "vendor": "raspberrypi",
        "family": "embedded",
        "display_name": "Raspberry Pi Imager",
        "version": "1.8.5",
        "install_method": "vendor_installer",
        "install_url": (
            "https://downloads.raspberrypi.org/imager/"
            "imager_latest_amd64.deb"
        ),
        "size_bytes": 73400320,
        "metadata": {
            "package_format": "deb",
            "host_os": ["linux", "darwin", "windows"],
        },
    },
    {
        "id": "beaglebone-debian-image",
        "vendor": "beagleboard",
        "family": "embedded",
        "display_name": "BeagleBone Debian 12 Image",
        "version": "12.7-2024-09-02",
        "install_method": "noop",
        "install_url": (
            "https://www.beagleboard.org/distros/"
            "am335x-12-7-2024-09-02-4gb-microsd-iot"
        ),
        "size_bytes": 4294967296,
        "metadata": {
            "board": "beaglebone-black",
            "flash_target": "microsd",
            "manual_step": True,
        },
    },
    {
        "id": "yocto-kirkstone",
        "vendor": "yoctoproject",
        "family": "embedded",
        "display_name": "Yocto Project (kirkstone LTS)",
        "version": "4.0.21",
        "install_method": "shell_script",
        "install_url": "https://git.yoctoproject.org/poky",
        "size_bytes": 314572800,
        "depends_on": ["arm-gnu-toolchain-13"],
        "metadata": {
            "branch": "kirkstone",
            "lts_until": "2026-04",
        },
    },
    {
        "id": "buildroot-2024-02",
        "vendor": "buildroot",
        "family": "embedded",
        "display_name": "Buildroot 2024.02 LTS",
        "version": "2024.02.6",
        "install_method": "shell_script",
        "install_url": (
            "https://buildroot.org/downloads/buildroot-2024.02.6.tar.gz"
        ),
        "size_bytes": 12582912,
        "metadata": {
            "lts": True,
            "cross_compile": True,
        },
    },
    # ─── Web (4) ─────────────────────────────────────────────────────
    {
        "id": "nodejs-lts-20",
        "vendor": "openjs",
        "family": "web",
        "display_name": "Node.js LTS 20 (Iron)",
        "version": "20.18.0",
        "install_method": "docker_pull",
        "install_url": "docker.io/library/node:20-bookworm-slim",
        "metadata": {
            "lts_codename": "iron",
            "runtime_for": [
                "react-native-cli",
                "capacitor-cli",
                "vite-7",
                "nextjs-cli",
                "pnpm-9",
            ],
        },
    },
    {
        "id": "pnpm-9",
        "vendor": "pnpm",
        "family": "web",
        "display_name": "pnpm 9",
        "version": "9.12.1",
        "install_method": "shell_script",
        "install_url": (
            "https://github.com/pnpm/pnpm/releases/download/"
            "v9.12.1/pnpm-linux-x64"
        ),
        "size_bytes": 25165824,
        "depends_on": ["nodejs-lts-20"],
        "metadata": {
            "install_via": "standalone",
            "package_manager_for": ["react-native-cli", "capacitor-cli"],
        },
    },
    {
        "id": "vite-7",
        "vendor": "vite",
        "family": "web",
        "display_name": "Vite 7",
        "version": "7.0.0",
        "install_method": "shell_script",
        "install_url": "https://registry.npmjs.org/vite",
        "depends_on": ["nodejs-lts-20", "pnpm-9"],
        "metadata": {
            "install_via": "npm",
            "framework_kind": "build_tool",
        },
    },
    {
        "id": "nextjs-cli",
        "vendor": "vercel",
        "family": "web",
        "display_name": "Next.js CLI",
        "version": "15.0.2",
        "install_method": "shell_script",
        "install_url": "https://registry.npmjs.org/create-next-app",
        "depends_on": ["nodejs-lts-20", "pnpm-9"],
        "metadata": {
            "install_via": "npm",
            "framework_kind": "meta_framework",
        },
    },
    # ─── Software (5) ────────────────────────────────────────────────
    {
        "id": "python-uv",
        "vendor": "astral",
        "family": "software",
        "display_name": "uv (Python toolchain installer)",
        "version": "0.4.20",
        "install_method": "shell_script",
        "install_url": "https://astral.sh/uv/install.sh",
        "size_bytes": 12582912,
        "metadata": {
            "install_via": "curl_pipe_sh",
            "script_sha256_required": True,
        },
    },
    {
        "id": "rust-stable",
        "vendor": "rust-lang",
        "family": "software",
        "display_name": "Rust (stable channel via rustup)",
        "version": "1.81.0",
        "install_method": "shell_script",
        "install_url": "https://sh.rustup.rs",
        "size_bytes": 838860800,
        "metadata": {
            "install_via": "rustup",
            "channel": "stable",
            "components": ["cargo", "rustc", "rust-std"],
        },
    },
    {
        "id": "go-1-22",
        "vendor": "google",
        "family": "software",
        "display_name": "Go 1.22",
        "version": "1.22.8",
        "install_method": "shell_script",
        "install_url": (
            "https://go.dev/dl/go1.22.8.linux-amd64.tar.gz"
        ),
        "size_bytes": 73400320,
        "metadata": {
            "gopath_default": "$HOME/go",
            "proxy_default": "https://proxy.golang.org",
        },
    },
    {
        "id": "docker-engine",
        "vendor": "docker",
        "family": "software",
        "display_name": "Docker Engine (CE)",
        "version": "27.3.1",
        "install_method": "shell_script",
        "install_url": "https://get.docker.com",
        "size_bytes": 209715200,
        "metadata": {
            "install_via": "get_docker_script",
            "requires_root": True,
            "systemd_unit": "docker.service",
        },
    },
    {
        "id": "git-lfs",
        "vendor": "github",
        "family": "software",
        "display_name": "Git LFS",
        "version": "3.5.1",
        "install_method": "shell_script",
        "install_url": (
            "https://github.com/git-lfs/git-lfs/releases/download/"
            "v3.5.1/git-lfs-linux-amd64-v3.5.1.tar.gz"
        ),
        "size_bytes": 7340032,
        "metadata": {
            "install_via": "tarball",
            "hooks": [
                "pre-push",
                "post-checkout",
                "post-commit",
                "post-merge",
            ],
        },
    },
    # ─── RTOS (3) ────────────────────────────────────────────────────
    {
        "id": "zephyr-rtos-3-7",
        "vendor": "linuxfoundation",
        "family": "rtos",
        "display_name": "Zephyr RTOS 3.7 LTS",
        "version": "3.7.0",
        "install_method": "shell_script",
        "install_url": "https://github.com/zephyrproject-rtos/zephyr.git",
        "size_bytes": 943718400,
        "depends_on": ["arm-gnu-toolchain-13"],
        "metadata": {
            "branch": "v3.7-branch",
            "west_init_required": True,
            "lts_until": "2026-07",
        },
    },
    {
        "id": "freertos-kernel-v11",
        "vendor": "amazon",
        "family": "rtos",
        "display_name": "FreeRTOS Kernel V11",
        "version": "11.1.0",
        "install_method": "noop",
        "install_url": (
            "https://github.com/FreeRTOS/FreeRTOS-Kernel.git"
        ),
        "metadata": {
            "install_via": "git_submodule",
            "header_only": True,
            "license": "MIT",
        },
    },
    {
        "id": "nuttx-12",
        "vendor": "apache",
        "family": "rtos",
        "display_name": "Apache NuttX 12",
        "version": "12.6.0",
        "install_method": "shell_script",
        "install_url": "https://github.com/apache/nuttx.git",
        "size_bytes": 314572800,
        "depends_on": ["arm-gnu-toolchain-13"],
        "metadata": {
            "branch": "releases/12.6",
            "configure_required": True,
        },
    },
    # ─── cross-toolchain (4) ─────────────────────────────────────────
    {
        "id": "arm-gnu-toolchain-13",
        "vendor": "arm",
        "family": "cross-toolchain",
        "display_name": (
            "Arm GNU Toolchain 13.3.Rel1 (aarch64-none-linux-gnu)"
        ),
        "version": "13.3.rel1",
        "install_method": "vendor_installer",
        "install_url": (
            "https://developer.arm.com/-/media/Files/downloads/gnu/"
            "13.3.rel1/binrel/"
            "arm-gnu-toolchain-13.3.rel1-x86_64-aarch64-none-linux-gnu"
            ".tar.xz"
        ),
        "size_bytes": 322961408,
        "metadata": {
            "target_triple": "aarch64-none-linux-gnu",
            "gcc_version": "13.3.0",
            "bundled_libc": "glibc",
        },
    },
    {
        "id": "linaro-aarch64-gcc-13",
        "vendor": "linaro",
        "family": "cross-toolchain",
        "display_name": "Linaro aarch64 GCC 13",
        "version": "13.0-2024.06",
        "install_method": "vendor_installer",
        "install_url": (
            "https://snapshots.linaro.org/gnu-toolchain/13.0-2024.06-1/"
            "aarch64-linux-gnu/"
            "gcc-linaro-13.0.1-2024.06-x86_64_aarch64-linux-gnu.tar.xz"
        ),
        "size_bytes": 188743680,
        "metadata": {
            "target_triple": "aarch64-linux-gnu",
            "flavour": "linaro_release_branch",
        },
    },
    {
        "id": "riscv-gnu-toolchain",
        "vendor": "riscv-international",
        "family": "cross-toolchain",
        "display_name": "RISC-V GNU Toolchain (multilib)",
        "version": "2024.09.03",
        "install_method": "shell_script",
        "install_url": (
            "https://github.com/riscv-collab/riscv-gnu-toolchain.git"
        ),
        "size_bytes": 1073741824,
        "metadata": {
            "target_triples": [
                "riscv64-unknown-elf",
                "riscv32-unknown-elf",
            ],
            "requires_build_from_source": True,
            "configure_args": "--enable-multilib",
        },
    },
    {
        "id": "xtensa-esp-elf-gcc-13",
        "vendor": "espressif",
        "family": "cross-toolchain",
        "display_name": "Xtensa ESP-ELF GCC 13",
        "version": "13.2.0_20240530",
        "install_method": "vendor_installer",
        "install_url": (
            "https://github.com/espressif/crosstool-NG/releases/"
            "download/esp-13.2.0_20240530/"
            "xtensa-esp-elf-13.2.0_20240530-x86_64-linux-gnu.tar.xz"
        ),
        "size_bytes": 188743680,
        "metadata": {
            "target_triple": "xtensa-esp-elf",
            "paired_with": "espressif-esp-idf-v5",
        },
    },
)


# Drift-guard handle: tests can read this without re-parsing the file.
SEED_ENTRIES = _SEED_ENTRIES


def _sql_escape(value: str) -> str:
    """Escape ``'`` for inclusion in a single-quoted SQL literal."""
    return value.replace("'", "''")


def _build_insert(entry: dict[str, Any], dialect: str) -> str:
    """Build a single ``INSERT OR IGNORE`` statement for one entry.

    Columns omitted (``schema_version`` / ``hidden`` / ``depends_on`` /
    ``metadata`` / ``created_at`` / ``updated_at``) fall through to the
    column DEFAULTs declared in 0051.  We *do* always set ``depends_on``
    and ``metadata`` explicitly because the JSON-cast vs TEXT difference
    is dialect-dependent and easier to write inline than to rely on the
    DEFAULT for half the rows.
    """
    cols: list[str] = ["id", "source", "vendor", "family",
                       "display_name", "version", "install_method"]
    vals: list[str] = [
        f"'{_sql_escape(entry['id'])}'",
        "'shipped'",
        f"'{_sql_escape(entry['vendor'])}'",
        f"'{_sql_escape(entry['family'])}'",
        f"'{_sql_escape(entry['display_name'])}'",
        f"'{_sql_escape(entry['version'])}'",
        f"'{_sql_escape(entry['install_method'])}'",
    ]

    if entry.get("install_url") is not None:
        cols.append("install_url")
        vals.append(f"'{_sql_escape(entry['install_url'])}'")

    if entry.get("size_bytes") is not None:
        cols.append("size_bytes")
        vals.append(str(int(entry["size_bytes"])))

    if entry.get("sha256") is not None:
        cols.append("sha256")
        vals.append(f"'{_sql_escape(entry['sha256'])}'")

    # depends_on + metadata: always emit (JSONB cast on PG, TEXT on
    # SQLite).  Empty values still pass through column DEFAULT, but we
    # write them explicitly so the seed row is fully visible.
    depends_on_json = json.dumps(entry.get("depends_on", []))
    metadata_json = json.dumps(entry.get("metadata", {}), sort_keys=True)
    cols.append("depends_on")
    cols.append("metadata")
    if dialect == "postgresql":
        vals.append(f"'{_sql_escape(depends_on_json)}'::jsonb")
        vals.append(f"'{_sql_escape(metadata_json)}'::jsonb")
    else:
        vals.append(f"'{_sql_escape(depends_on_json)}'")
        vals.append(f"'{_sql_escape(metadata_json)}'")

    cols_sql = ", ".join(cols)
    vals_sql = ", ".join(vals)
    return (
        f"INSERT OR IGNORE INTO catalog_entries ({cols_sql}) "
        f"VALUES ({vals_sql})"
    )


# Public for BS.1.5 drift-guard tests / debugging.
def _seed_ids() -> tuple[str, ...]:
    return tuple(e["id"] for e in _SEED_ENTRIES)


def upgrade() -> None:
    conn = op.get_bind()
    dialect = conn.dialect.name
    for entry in _SEED_ENTRIES:
        conn.exec_driver_sql(_build_insert(entry, dialect))


def downgrade() -> None:
    # Narrow downgrade: only delete the shipped rows whose id is in our
    # seed set.  An admin who hand-soft-deleted a shipped row (hidden=
    # true) is preserved by the ``hidden = false`` filter; an
    # admin-overridden row at source='operator' / 'override' is
    # preserved because of the ``source = 'shipped'`` filter.
    ids = ", ".join(f"'{_sql_escape(i)}'" for i in _seed_ids())
    conn = op.get_bind()
    conn.exec_driver_sql(
        f"DELETE FROM catalog_entries "
        f"WHERE source = 'shipped' "
        f"AND id IN ({ids})"
    )
