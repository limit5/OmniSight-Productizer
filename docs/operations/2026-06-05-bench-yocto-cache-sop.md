# Bench Yocto cache SOP -- WSL2 DrvFs sstate and downloads

**Date filed:** 2026-06-05
**Ticket:** OP-2085 (Phase 0 / P0.E.2 L04.5)
**Status:** operator-runnable bench SOP

---

## 1. Rationale
Phase 0 real-hardware acceptance needs repeated Yocto builds for RK3588,
RV1126, QCS6490, and Genio 1200. The P0.E.2 design records expected wall-clock
as 4-8 hours native Linux, 6-12 hours on WSL2, then 30-60 minutes native or
1-2 hours WSL2 when sstate is warm.

- `TMPDIR`: high-churn unpack, compile, package, and image assembly output.
- `SSTATE_DIR` and `DL_DIR`: reusable cache artifacts and source downloads.

Keep `TMPDIR` in the WSL2 checkout so active builds use Linux filesystem
semantics. Put `SSTATE_DIR` and `DL_DIR` under a Windows-side DrvFs path so a
new worktree, clean checkout, or runner overlay reset can reuse them. The
P0.E.2 design calls this out as risk R3 mitigation.

Rough references: P0.E.2 design section 4.8 and risk R3; Microsoft WSL DrvFs
drive mounts under `/mnt/<drive-letter>`; Docker WSL best practices for active
Linux workloads on the Linux filesystem. Scope is host-side cache placement
only; this does not change recipes, BSP layers, flash scripts, SDK mirrors, CI,
credentials, or board-specific build commands.

## 2. Windows-side cache path
Use a drive with at least 200 GB free:

```text
D:\yocto-cache\
```

If `D:` is unavailable, choose another Windows drive and replace
`/mnt/d/yocto-cache` consistently below.

## 3. WSL2 mount verification
DrvFs normally auto-mounts Windows drives under `/mnt/c`, `/mnt/d`, and so on.
Verify the chosen drive exists from the WSL2 distro before editing Yocto config:

```bash
ls /mnt/d
mkdir -p /mnt/d/yocto-cache
ls /mnt/d/yocto-cache
```

If `/mnt/d` is missing, confirm the drive exists in Windows Explorer, check
`/etc/wsl.conf`, restart WSL, then rerun the `ls` checks.

## 4. Yocto local.conf snippet
Add the cache paths to the active build's `build/conf/local.conf`:

```bitbake
SSTATE_DIR = "/mnt/d/yocto-cache/sstate"
DL_DIR = "/mnt/d/yocto-cache/downloads"
TMPDIR = "${TOPDIR}/tmp"
```

Keep `TMPDIR` under `${TOPDIR}`; only the reusable cache goes to DrvFs.

## 5. First-time setup

Create the cache directories before the first `bitbake`:

```bash
mkdir -p /mnt/d/yocto-cache/{sstate,downloads}
chmod 777 /mnt/d/yocto-cache /mnt/d/yocto-cache/{sstate,downloads}
df -h /mnt/d/yocto-cache
```

Run the first build normally after the snippet is in place. Record the first-run
wall-clock and the first warm-cache rebuild wall-clock in the P0.E.2 ticket or
the per-board build ticket.

## 6. Corruption recovery

If bitbake reports stale sstate output or repeated task failures that disappear
after forcing a task rerun, clear only sstate first:

```bash
rm -rf /mnt/d/yocto-cache/sstate
mkdir -p /mnt/d/yocto-cache/sstate
chmod 777 /mnt/d/yocto-cache/sstate
```

Then run a full rebuild. Keep downloads unless fetcher checksums identify a
corrupt archive.

## 7. Sharing across four boards

Use the same `SSTATE_DIR` and `DL_DIR` for RK3588, RV1126, QCS6490, and Genio
1200. Yocto sstate keys include recipe, task, machine-relevant metadata,
toolchain, and dependency hashes, so shared recipes automatically reuse cache
while board-specific outputs remain separate. Do not create one cache directory
per board unless debugging suspected machine-specific corruption.

Operator wall-clock confirmation is the verification gate for this SOP. There is
no CI test because the cache lives on the physical WSL2 bench host.
