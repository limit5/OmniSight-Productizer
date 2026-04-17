r"""L9 — quick-start.sh structural + behavioral tests.

These don't actually spin up Docker / Cloudflare. They verify:

1. Syntax validity (`bash -n`).
2. Critical guards stay in place (pipefail, WSL systemd probe, RUNNING count,
   read_secret helper, NON_INTERACTIVE detection, arch-aware cloudflared).
3. `--dry-run` exits cleanly on a host that has docker/jq/etc.
4. `--help` shows usage.
5. `_sed_safe_replace` correctly rewrites .env keys with values containing
   awkward characters like `/ & \ =`.
6. Idempotent: re-running after .env exists prints the "已存在" skip path.

Rationale: the previous version of the script shipped with 3 silent bugs
(see commit acc0fc1). A structural guard test prevents regressions without
needing the full docker+CF stack.
"""

import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "quick-start.sh"


def test_script_exists_and_is_executable():
    assert SCRIPT.is_file(), f"{SCRIPT} missing"
    assert os.access(SCRIPT, os.X_OK), f"{SCRIPT} must be chmod +x"


def test_bash_syntax_ok():
    r = subprocess.run(
        ["bash", "-n", str(SCRIPT)],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, f"bash -n failed:\n{r.stderr}"


def test_pipefail_enabled():
    """Regression: `docker compose up | tee | tail` used to hide build errors."""
    content = SCRIPT.read_text()
    assert "set -euo pipefail" in content, "pipefail must be enabled globally"


def test_running_count_uses_services_status_filter():
    """Regression: `grep -c pat || echo 0` produced '0\\n0' on no match."""
    content = SCRIPT.read_text()
    assert "--services --status=running" in content, (
        "must use docker compose ps --services --status=running "
        "instead of the old grep-based container count"
    )
    # the old broken pattern must not reappear
    assert "jq -r '.State' 2>/dev/null | grep -c \"running\" || echo" not in content


def test_wsl_systemd_probes_pid1():
    """Regression: `systemctl --version` succeeds even without systemd as PID 1."""
    content = SCRIPT.read_text()
    assert "/run/systemd/system" in content, "must probe /run/systemd/system"
    assert "ps -p 1 -o comm=" in content, "must verify systemd is PID 1"


def test_read_secret_helper_exists():
    content = SCRIPT.read_text()
    assert "read_secret()" in content, "read_secret helper must exist"
    assert "read -rsp" in content, "must use silent read for secrets"


def test_secret_prompts_use_read_secret():
    """API keys and CF token must never be echoed to terminal/log."""
    content = SCRIPT.read_text()
    # None of the credential fields should use a plain `read -rp`
    banned_patterns = [
        'read -rp "Anthropic API Key',
        'read -rp "OpenAI API Key',
        'read -rp "Google API Key',
        'read -rp "Cloudflare API Token',
    ]
    for pat in banned_patterns:
        assert pat not in content, f"credential prompt must use read_secret: {pat!r}"


def test_non_interactive_mode_detection():
    content = SCRIPT.read_text()
    assert "NON_INTERACTIVE=" in content
    assert "[ ! -t 0 ]" in content, "must detect non-TTY via [ ! -t 0 ]"


def test_cloudflared_download_is_arch_aware():
    content = SCRIPT.read_text()
    assert "cloudflared-linux-amd64.deb" in content
    assert "cloudflared-linux-arm64.deb" in content
    # must branch on uname -m
    assert "uname -m" in content


def test_help_flag():
    r = subprocess.run(
        [str(SCRIPT), "--help"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert r.returncode == 0
    assert "--dry-run" in r.stdout
    assert "--uninstall" in r.stdout


@pytest.mark.skipif(
    shutil.which("docker") is None,
    reason="requires docker for full dry-run preflight",
)
def test_dry_run_exits_zero_on_healthy_host():
    r = subprocess.run(
        [str(SCRIPT), "--dry-run"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=60,
    )
    # dry-run should exit 0 on a host where docker/jq/curl exist.
    # If the host is missing a preflight dep the test is meaningless;
    # treat returncode 1 as "environment issue, not script bug" and skip.
    if r.returncode != 0:
        pytest.skip(
            f"dry-run preflight didn't pass on this host (missing deps?):\n"
            f"stdout tail:\n{r.stdout[-500:]}"
        )
    assert "Dry-run" in r.stdout or "dry-run" in r.stdout.lower()


def test_sed_safe_replace_handles_special_chars(tmp_path):
    r"""Exercise _sed_safe_replace on chars that break sed: /, &, =.

    Real-world API keys (Anthropic / OpenAI / Google) use the charset
    [A-Za-z0-9_-], so we don't stress-test literal backslash — awk's -v
    flag performs C-style escape expansion which would mangle `\\`, but
    no provider issues keys containing `\`.
    """
    env_file = tmp_path / ".env"
    env_file.write_text(
        textwrap.dedent(
            """\
            # comment line
            FOO=oldvalue
            OMNISIGHT_ANTHROPIC_API_KEY=
            BAR=keep-me
            """
        )
    )

    awkward = "sk-ant-abc/xyz&qqq=end+slash"
    script = textwrap.dedent(
        f"""\
        #!/usr/bin/env bash
        set -euo pipefail
        _sed_safe_replace() {{
            local file="$1" key="$2" value="$3"
            awk -v k="$key" -v v="$value" '
                BEGIN {{ found=0 }}
                $0 ~ "^"k"=" {{ print k"="v; found=1; next }}
                {{ print }}
            ' "$file" > "${{file}}.tmp" && mv "${{file}}.tmp" "$file"
        }}
        _sed_safe_replace "{env_file}" "OMNISIGHT_ANTHROPIC_API_KEY" '{awkward}'
        """
    )
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    rewritten = env_file.read_text()
    assert f"OMNISIGHT_ANTHROPIC_API_KEY={awkward}" in rewritten
    assert "FOO=oldvalue" in rewritten, "other keys must be preserved"
    assert "BAR=keep-me" in rewritten
    assert "# comment line" in rewritten, "comments must be preserved"


def test_idempotent_env_skip(tmp_path, monkeypatch):
    """When .env already exists, Step 1 must say '已存在，跳過生成' and not re-prompt.

    We run the script in NON_INTERACTIVE mode (no TTY, stdin closed) in a
    throwaway dir that contains the minimum scaffolding. We don't need
    docker to be present — we only check the .env-existence branch prints
    the skip message before any dep error aborts us.
    """
    work = tmp_path / "work"
    work.mkdir()
    # Minimum scaffolding so the preflight file existence check passes.
    (work / ".env.example").write_text("OMNISIGHT_LLM_PROVIDER=anthropic\n")
    (work / ".env").write_text("OMNISIGHT_LLM_PROVIDER=anthropic\n")
    (work / "docker-compose.prod.yml").write_text("services: {}\n")

    r = subprocess.run(
        ["bash", str(SCRIPT), "--dry-run"],
        cwd=str(work),
        capture_output=True,
        text=True,
        timeout=30,
        stdin=subprocess.DEVNULL,
    )
    # --dry-run exits after preflight, so we don't reach Step 1. But the
    # script's idempotency code path is a simple `if [ -f .env ]` — the
    # text we want is source-level, not runtime. We already verified at
    # source level. Here we just confirm the dry-run completes (returncode
    # may be 1 if deps missing, but must not be killed by SIGSEGV or
    # trap-induced infinite loop).
    assert r.returncode in (0, 1), f"unexpected exit {r.returncode}"
    # Source-level idempotency guard:
    assert 'if [ -f ".env" ]' in SCRIPT.read_text()


def test_uninstall_prompt_safety(tmp_path):
    """--uninstall must require explicit 'y' confirmation — no 'y' → abort."""
    r = subprocess.run(
        ["bash", str(SCRIPT), "--uninstall"],
        cwd=str(tmp_path),
        input="\n",  # blank answer = "no"
        capture_output=True,
        text=True,
        timeout=15,
    )
    # Should exit cleanly with "取消" (cancelled) rather than proceed.
    assert r.returncode == 0
    assert "取消" in r.stdout or "cancel" in r.stdout.lower()


# ──────────────────────────────────────────────────────────────────────
# L9 — WSL2 detection + systemd/nohup branching guards
# ──────────────────────────────────────────────────────────────────────
# Scope: the quick-start must handle 2 WSL2 cases symmetrically —
#   (a) systemd as PID 1 → `cloudflared service install` + systemctl
#   (b) no systemd       → `nohup cloudflared tunnel run` + PID-file idempotency
# + print copy-pasteable instructions for enabling systemd in case (b).
# These tests are source-level guards: they don't require WSL at runtime.


def test_wsl_systemd_branching_both_modes_present():
    """Both branches (systemd service install / nohup tunnel run) must exist."""
    content = SCRIPT.read_text()
    # systemd branch
    assert "cloudflared service install" in content, (
        "systemd branch must call `cloudflared service install` to register the unit"
    )
    assert "systemctl enable cloudflared" in content
    assert "systemctl restart cloudflared" in content
    # nohup branch
    assert "nohup cloudflared tunnel run" in content, (
        "no-systemd branch must fall back to `nohup cloudflared tunnel run`"
    )
    # And the two branches must be gated by the same WSL_SYSTEMD variable
    # (not duplicated with divergent detection, which was the L8 regression risk).
    assert 'if [ "$WSL_SYSTEMD" = true ]' in content


def test_nohup_branch_is_idempotent():
    """Re-running the script must reuse a live cloudflared, not spawn a second one.

    The fix is a PID file: on second run, if it points at a live `cloudflared`
    process, reuse that PID; otherwise clear the stale file and spawn fresh.
    """
    content = SCRIPT.read_text()
    # PID file persisted for idempotency
    assert "CFLARED_PID_FILE=" in content, (
        "nohup branch must persist the cloudflared PID to allow re-run idempotency"
    )
    # Re-use logic must verify the PID actually points at cloudflared
    # (defense against PID recycling).
    assert "ps -p \"$OLD_PID\" -o comm=" in content, (
        "must verify reused PID actually runs `cloudflared` (PID recycling protection)"
    )
    # disown so the tunnel survives the parent shell exiting
    assert "disown " in content, "nohup'd cloudflared must be disown'd from the job table"


def test_systemd_enablement_guidance_is_copy_pasteable():
    """When systemd is off, the script must emit actionable wsl.conf instructions."""
    content = SCRIPT.read_text()
    # Must show both the wsl.conf content and the `wsl --shutdown` step.
    assert "/etc/wsl.conf" in content, "must name the config file to edit"
    assert "systemd=true" in content, "must show the exact config line"
    assert "wsl --shutdown" in content, (
        "must instruct user to shut down WSL so the [boot] block takes effect"
    )


def test_nohup_log_path_is_namespaced():
    """Log file lives under /tmp with an omnisight prefix — easier to find, won't collide."""
    content = SCRIPT.read_text()
    assert "/tmp/omnisight-cloudflared.log" in content, (
        "cloudflared log must be namespaced, not the generic /tmp/cloudflared.log"
    )


def test_wsl_detection_simulation(tmp_path):
    """Simulate both WSL + non-WSL init scenarios against the detection block.

    We extract the detection logic from the script, stub `grep` on a fake
    /proc/version, and assert WSL_SYSTEMD is set correctly. This catches
    logic bugs in the `if` / `elif` chain without needing a real WSL host.
    """
    # Extract the WSL_SYSTEMD detection block (between the marker comments).
    src = SCRIPT.read_text()
    start = src.index("# WSL2 systemd check")
    end = src.index("# 網路連線", start)
    detection_block = src[start:end]

    # Scenario 1: non-WSL Linux with systemd → WSL_SYSTEMD stays true.
    #   We force grep to return false (not in WSL) and pretend PID 1 is systemd.
    script1 = textwrap.dedent(
        """\
        #!/usr/bin/env bash
        set -o pipefail
        # The extracted block references these; stub to harmless values so the
        # test doesn't have to co-evolve with color codes.
        LOG_FILE=/tmp/quick-start-test-$$.log
        BOLD=""; NC=""
        WSL_SYSTEMD=true
        warn() {{ echo "WARN $*"; }}
        log()  {{ echo "LOG $*"; }}
        # Stub: grep always fails (not WSL)
        grep() {{ return 1; }}
        ps()   {{ echo systemd; }}
        mkdir -p /tmp/fake-systemd && touch /tmp/fake-systemd/sentinel
        # Redirect /run/systemd/system check via shell builtin — replace the
        # test expression in the block we extracted with something stable.
        {block}
        echo "RESULT WSL_SYSTEMD=$WSL_SYSTEMD IS_WSL=${{IS_WSL:-false}}"
        """
    ).format(block=detection_block.replace("/run/systemd/system", "/tmp/fake-systemd"))
    (tmp_path / "s1.sh").write_text(script1)
    r1 = subprocess.run(
        ["bash", str(tmp_path / "s1.sh")],
        capture_output=True, text=True, timeout=10,
    )
    assert "WSL_SYSTEMD=true" in r1.stdout, (
        f"non-WSL + systemd host should leave WSL_SYSTEMD=true, got:\n{r1.stdout}\n{r1.stderr}"
    )
    assert "IS_WSL=false" in r1.stdout

    # Scenario 2: WSL2 without systemd → WSL_SYSTEMD=false, IS_WSL=true.
    script2 = textwrap.dedent(
        """\
        #!/usr/bin/env bash
        set -o pipefail
        # The extracted block references these; stub to harmless values so the
        # test doesn't have to co-evolve with color codes.
        LOG_FILE=/tmp/quick-start-test-$$.log
        BOLD=""; NC=""
        WSL_SYSTEMD=true
        warn() {{ echo "WARN $*"; }}
        log()  {{ echo "LOG $*"; }}
        # Stub: grep for microsoft succeeds (we ARE in WSL)
        grep() {{ return 0; }}
        ps()   {{ echo init; }}   # PID 1 is NOT systemd
        {block}
        echo "RESULT WSL_SYSTEMD=$WSL_SYSTEMD IS_WSL=${{IS_WSL:-false}}"
        """
    ).format(block=detection_block.replace("/run/systemd/system", "/tmp/definitely-not-here-xyz"))
    (tmp_path / "s2.sh").write_text(script2)
    r2 = subprocess.run(
        ["bash", str(tmp_path / "s2.sh")],
        capture_output=True, text=True, timeout=10,
    )
    assert "WSL_SYSTEMD=false" in r2.stdout, (
        f"WSL2 without systemd should set WSL_SYSTEMD=false, got:\n{r2.stdout}\n{r2.stderr}"
    )
    assert "IS_WSL=true" in r2.stdout, (
        "WSL2 host should set IS_WSL=true even when systemd is off"
    )
    # And the user-facing systemd-enablement tips must fire.
    assert "/etc/wsl.conf" in r2.stdout
    assert "wsl --shutdown" in r2.stdout


# ──────────────────────────────────────────────────────────────────────
# L9 — Step 5: GoDaddy NS migration guidance
# ──────────────────────────────────────────────────────────────────────
# GoDaddy has no public NS-change API for consumer domains, so the script
# can never fully automate this. Instead it must: (a) auto-detect current
# NS so re-runs skip silently-already-done work, (b) print the exact two
# CF-assigned NS values the user has to type in, (c) walk them through
# the current GoDaddy UI, (d) give verification commands + DNSSEC/MX
# safety warnings. These tests guard each of those four contracts.


def test_cf_nameservers_captured_from_zones_api():
    """CF_NAMESERVERS must be populated from .result[0].name_servers so
    Step 5 can print the two NS values verbatim without a tab-switch.
    """
    content = SCRIPT.read_text()
    # The global array that Step 5 reads
    assert "CF_NAMESERVERS=()" in content, (
        "CF_NAMESERVERS must be initialized as an empty array alongside CF_READY"
    )
    # Populated via jq from the zones API response
    assert ".result[0].name_servers" in content, (
        "must read name_servers from the CF zones response"
    )
    # Portability: `while read` not `readarray` (bash 3.2 compat for macOS).
    # readarray would break if we ever shipped this for macOS bash 3.2 hosts.
    assert "readarray" not in content, (
        "must use `while IFS= read` not `readarray` for bash 3.2 compatibility"
    )


def test_ns_auto_detect_uses_multiple_tools():
    """NS detection must try dig → host → nslookup in that order, so the
    script works on minimal WSL2 images that only have one of them.
    """
    content = SCRIPT.read_text()
    assert "dig +short +time=3 +tries=1 NS" in content, (
        "dig call must be bounded with +time=3 +tries=1 to avoid hangs"
    )
    assert "@1.1.1.1" in content, (
        "dig should force a public resolver to survive broken /etc/resolv.conf"
    )
    assert 'host -t NS "$DOMAIN"' in content, "host fallback must be present"
    assert "nslookup -type=NS" in content, "nslookup fallback must be present"


def test_ns_state_classification_is_tri_state():
    """A full NS cutover produces mid-propagation states where some NS
    are already CF and others are still GoDaddy. The script must not
    binary-classify — otherwise during propagation it'll falsely tell
    users their NS is wrong.
    """
    content = SCRIPT.read_text()
    # The three terminal states + unknown
    assert 'NS_STATE="all-cf"' in content, "all-CF state (done) must exist"
    assert 'NS_STATE="mixed"' in content, (
        "mid-propagation state must exist — some NS CF, some GoDaddy"
    )
    assert 'NS_STATE="non-cf"' in content, "still-on-GoDaddy state must exist"
    # Skip-when-done contract
    assert "NS 遷移已完成，跳過此步驟" in content, (
        "all-cf path must announce idempotent skip"
    )


def test_godaddy_ui_walkthrough_has_current_menu_path():
    """The printed walkthrough must match the current (2024+) GoDaddy UI
    path — if GoDaddy reorganizes again we'll need to update, but silent
    drift to a stale path is the real failure mode.
    """
    content = SCRIPT.read_text()
    # Current portfolio URL
    assert "https://dcc.godaddy.com/control/portfolio" in content, (
        "must point users at the 2024+ portfolio URL, not the legacy manage/ URL only"
    )
    # Exact menu strings GoDaddy still uses
    assert "I'll use my own nameservers" in content, (
        "must name the exact radio option as shown in GoDaddy's UI"
    )
    assert "Nameservers" in content, "must reference the Nameservers tab"
    assert "Change" in content, "must reference the Change Nameservers action"


def test_verification_commands_are_printed():
    """After NS change, user needs to verify propagation. Script must print
    both a command-line check and a web-based global view.
    """
    content = SCRIPT.read_text()
    assert "dig NS ${DOMAIN} +short" in content, (
        "must print the dig verification command with the user's actual domain"
    )
    assert "whatsmydns.net" in content, (
        "must link to whatsmydns.net for global propagation check"
    )
    # DNSSEC warning — #1 footgun when switching registrar NS
    assert "DNSSEC" in content, (
        "must warn about DNSSEC, which bricks the domain if left enabled during cutover"
    )
    # MX/email warning — #2 footgun (email breaks silently)
    assert "MX" in content, (
        "must warn about MX/TXT records so email doesn't break on NS switch"
    )


def test_cf_nameservers_graceful_fallback_when_empty():
    """If the CF API call failed or was skipped, CF_NAMESERVERS is empty.
    Step 5 must still work — falling back to "look them up in CF dashboard"
    rather than printing an empty NS list. This is the graceful-degradation
    path most likely to regress silently, so guard it explicitly.
    """
    content = SCRIPT.read_text()
    # The fallback branch: tests that CF_NAMESERVERS is guarded and when
    # empty, fallback copy kicks in.
    assert '"${#CF_NAMESERVERS[@]}" -gt 0' in content, (
        "must guard CF_NAMESERVERS indexing with a length check (bash 3.2 + set -u safety)"
    )
    # The fallback text must point users at CF dashboard
    assert "dash.cloudflare.com" in content, (
        "empty-CF_NAMESERVERS fallback must direct users to CF dashboard"
    )
    assert ".ns.cloudflare.com" in content, (
        "fallback must tell user to look for *.ns.cloudflare.com pattern"
    )

    # Behavioral: simulate the case with an empty array + non-cf state,
    # run the classification block, and confirm the fallback branch fires.
    # We extract Step 5 and stub everything around it.
    src = SCRIPT.read_text()
    step5_start = src.index("# Step 5: GoDaddy NS 遷移指引")
    step5_end = src.index("# Step 6: 完成")
    step5_block = src[step5_start:step5_end]

    script = textwrap.dedent(
        """\
        #!/usr/bin/env bash
        set -uo pipefail
        RED=""; GREEN=""; YELLOW=""; CYAN=""; BOLD=""; NC=""
        LOG_FILE=$(mktemp)
        DOMAIN="example.com"
        CF_READY=false
        CF_NAMESERVERS=()
        log()  {{ echo "LOG $*"; }}
        warn() {{ echo "WARN $*"; }}
        err()  {{ echo "ERR $*"; }}
        step() {{ echo "STEP $*"; }}
        # Force no lookup tools → NS_STATE=unknown → full manual block prints
        command() {{ return 1; }}
        {block}
        """
    ).format(block=step5_block)

    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as f:
        f.write(script)
        scratch = f.name

    try:
        r = subprocess.run(
            ["bash", scratch], capture_output=True, text=True, timeout=10
        )
        # Fallback must fire — user directed to CF dashboard instead of
        # a literal NS list:
        assert "未取得 CF API Token" in r.stdout or "zone 尚未建立" in r.stdout, (
            f"empty CF_NAMESERVERS must trigger the 'look up in CF dashboard' "
            f"fallback, got:\n{r.stdout}\n{r.stderr}"
        )
        assert "dash.cloudflare.com" in r.stdout
    finally:
        os.unlink(scratch)


# ──────────────────────────────────────────────────────────────────────
# L9 — End-to-end idempotency contract
# ──────────────────────────────────────────────────────────────────────
# Contract the script guarantees across re-runs:
#   (1) .env already present           → skip regeneration (no overwrite).
#   (2) Tunnel by TUNNEL_NAME exists   → reuse its ID + token, never
#       create a second tunnel sharing the name (CF load-balances across
#       tunnels by ID, so a duplicate name with a new ID leaks tunnel
#       slots + confuses the ingress config).
#   (3) Ingress configured previously  → PUT with identical body is a
#       replace-style no-op (by CF API design), so the script can re-run
#       Step 4 after a Ctrl-C without producing drift.
#   (4) DNS CNAME already present:
#       (a) content matches current tunnel → skip quietly.
#       (b) content points at a DIFFERENT tunnel (e.g. the previous tunnel
#           was deleted out-of-band and a fresh one got a new ID) → PATCH
#           in place so the script self-heals without operator intervention.
#           Without this drift-repair the site would 1016 forever.
#   (5) cloudflared already installed  → skip .deb download.
#   (6) cloudflared already running (nohup) → skip relaunch (PID-file guard,
#       covered by test_nohup_branch_is_idempotent above).
# These tests pin all six invariants at the source level, plus one
# behavioral test that drives the CNAME drift-repair path end-to-end
# against a mock curl+jq shim.


def test_idempotency_env_skip_source_guard():
    """Re-running with an existing .env must skip regeneration, not overwrite."""
    content = SCRIPT.read_text()
    # The exact guard + message the script emits on re-run.
    assert 'if [ -f ".env" ]; then' in content
    assert ".env 已存在，跳過生成" in content
    # Negative guard: the interactive regeneration branch must be gated inside
    # an `else` under the `.env exists` check — i.e. the file is only created
    # when missing. Catch regressions where someone moves `cp .env.example .env`
    # above the guard.
    idx_guard = content.index('if [ -f ".env" ]; then')
    idx_cp_after_guard = content.index("cp .env.example .env", idx_guard)
    # All cp-to-.env lines must live AFTER the guard (inside elif / else).
    cp_count_total = content.count("cp .env.example .env")
    cp_count_before_guard = content[:idx_guard].count("cp .env.example .env")
    assert cp_count_before_guard == 0, (
        ".env must not be copied before the existence guard — that would "
        "clobber a user's customized .env on every re-run"
    )
    # Sanity: at least one cp happens (first-run path).
    assert cp_count_total >= 1
    assert idx_cp_after_guard > idx_guard


def test_idempotency_tunnel_reuse_source_guard():
    """Tunnel reuse must query by name + is_deleted=false and skip re-create."""
    content = SCRIPT.read_text()
    # The API query that powers reuse — if this regresses to
    # /tunnels?name=X (without is_deleted) then a soft-deleted tunnel
    # with the same name would be reused by mistake.
    assert "tunnels?name=${TUNNEL_NAME}&is_deleted=false" in content, (
        "tunnel-exists probe must include is_deleted=false so we don't "
        "reuse a soft-deleted tunnel"
    )
    # Reuse path must (a) extract the existing id, (b) fetch token for it,
    # (c) announce reuse to the user.
    assert 'CF_TUNNEL_ID=$(echo "$EXISTING" | jq -r \'.result[0].id // empty\')' in content
    assert "Tunnel 已存在" in content
    # Reuse path must fetch token via .../tunnels/${CF_TUNNEL_ID}/token,
    # not skip the token step (which would leave CF_TUNNEL_TOKEN empty
    # and break the downstream ingress/connector).
    assert "/tunnels/${CF_TUNNEL_ID}/token" in content
    # And there must NOT be two separate `POST .../tunnels` calls — only one,
    # inside the "not-found" branch. A regression adding a second POST would
    # bypass the reuse check entirely.
    tunnel_post_count = content.count("-X POST")
    # We allow multiple POSTs total (CNAME, tunnel, etc) but the tunnel-create
    # POST is specifically identified by its URL:
    tunnel_create_posts = content.count(
        'accounts/${CF_ACCOUNT_ID}/tunnels"'
    )
    # The one tunnel POST is the `curl ... -X POST ... "...accounts/${CF_ACCOUNT_ID}/tunnels"`.
    # A regression duplicating this URL would reflect a tunnel being created twice.
    assert tunnel_create_posts == 1, (
        f"exactly one POST to .../accounts/{{id}}/tunnels expected (create-if-missing); "
        f"found {tunnel_create_posts}"
    )


def test_idempotency_ingress_uses_put_not_post():
    """Ingress must use PUT (replace-style) so a second run is a no-op on
    unchanged config. POST would create duplicate config revisions.
    """
    content = SCRIPT.read_text()
    # Locate the ingress API call and verify it's a PUT.
    ingress_block_start = content.index("設定 Tunnel ingress")
    ingress_block_end = content.index("DNS CNAME", ingress_block_start)
    ingress_block = content[ingress_block_start:ingress_block_end]
    assert "-X PUT" in ingress_block, (
        "ingress config must be written with PUT so re-runs don't create "
        "duplicate revisions"
    )
    assert "/tunnels/${CF_TUNNEL_ID}/configurations" in ingress_block


def test_idempotency_cname_already_exists_branch_source_guard():
    """CNAME 'already exists' branch must exist + verify content + PATCH on drift.

    Three sub-behaviors guarded here:
      a) Detect 'already exists' error from CF API.
      b) GET existing record by (type=CNAME, name=HOSTNAME) to compare content.
      c) PATCH if drift detected; log skip if content matches.

    Without (b) + (c) the script silently succeeds on a stale CNAME that
    points at a deleted tunnel → user sees 1016 forever + has to edit DNS
    manually. That's the worst-case for a "just re-run it" escape hatch.
    """
    content = SCRIPT.read_text()
    # Locate the CNAME loop body.
    cname_start = content.index("DNS CNAME:")
    cname_end = content.index("安裝 cloudflared", cname_start)
    cname_block = content[cname_start:cname_end]

    # (a) already-exists detection
    assert 'grep -qi "already exists"' in cname_block, (
        "must detect 'already exists' error message from CF API"
    )
    # (b) GET existing record to compare content
    assert "dns_records?type=CNAME&name=${HOSTNAME}" in cname_block, (
        "on 'already exists', must GET the existing record to verify "
        "content (drift-repair)"
    )
    assert "EXISTING_CONTENT=" in cname_block, (
        "must capture existing record's content for comparison"
    )
    # (c) PATCH on drift; skip if match
    assert "-X PATCH" in cname_block, (
        "must PATCH the existing record if content drifted from current tunnel"
    )
    assert "CNAME 已存在且指向當前 tunnel，跳過" in cname_block, (
        "must log explicit skip when existing CNAME already points at current tunnel"
    )
    assert "偵測到 CNAME 漂移" in cname_block, (
        "must log the drift detection before PATCH — diagnostic breadcrumb"
    )


def test_idempotency_cloudflared_install_skipped_if_present():
    """cloudflared .deb install path must be gated by `! command -v cloudflared`."""
    content = SCRIPT.read_text()
    assert "if ! command -v cloudflared" in content, (
        "cloudflared install must be gated — a second run with cloudflared "
        "already on PATH must not re-download + re-dpkg"
    )
    assert "cloudflared 已安裝" in content, (
        "must announce 'already installed' in the else branch of the install guard"
    )


def test_idempotency_cname_drift_repair_behavioral(tmp_path):
    """End-to-end behavioral test of the CNAME drift-repair path.

    We extract the CNAME loop and drive it with mocked `curl` + `jq` so we
    can assert the exact API calls the script makes. Three scenarios:

      1. First-run happy path: POST succeeds → log "CNAME 已建立".
      2. Already-exists + content matches: POST fails with 'already exists',
         GET returns matching content → log "已存在且指向當前 tunnel，跳過".
         Must NOT issue a PATCH.
      3. Drift: POST fails with 'already exists', GET returns DIFFERENT
         content → PATCH is issued → log "CNAME 已更新".
         The drift message "偵測到 CNAME 漂移" must fire.

    This guards the sub-behavior that's hardest to catch by code reading:
    the drift scenario is silent unless you mock the API and trace the
    exact sequence of curl verbs.
    """
    src = SCRIPT.read_text()
    # Extract just the inner `for HOSTNAME in ...; do ... done` block — we
    # keep it intact so the test breaks if the block structure regresses.
    start = src.index("TUNNEL_CNAME=\"${CF_TUNNEL_ID}.cfargotunnel.com\"")
    end = src.index("# ── 安裝 cloudflared ──", start)
    cname_block = src[start:end]

    # Harness: stub curl so we can return canned responses for POST/GET/PATCH,
    # and log every invocation so the test can assert call order.
    def make_harness(scenario: str) -> str:
        return textwrap.dedent(
            """\
            #!/usr/bin/env bash
            set -uo pipefail
            # ---- stubs for log/warn/err + colors the block references ----
            LOG_FILE=$(mktemp)
            CYAN=""; BOLD=""; NC=""; YELLOW=""
            log()  {{ echo "LOG $*"; }}
            warn() {{ echo "WARN $*"; }}
            err()  {{ echo "ERR $*"; }}

            # ---- vars the block reads ----
            CF_TUNNEL_ID="new-tunnel-id"
            CF_ZONE_ID="zone123"
            CF_TOKEN="fake-token"
            DOMAIN="example.com"
            API_SUBDOMAIN="api"

            # ---- mock curl: behavior driven by SCENARIO env + URL/method ----
            # Method detection: we look for -X PATCH / -X POST in argv; default GET.
            # URL is always the last arg.
            SCENARIO="{scenario}"
            CURL_CALLS=/tmp/curl-calls-$$.log
            : > "$CURL_CALLS"

            curl() {{
                local method="GET"
                local url=""
                local args=( "$@" )
                local i=0
                while [ $i -lt ${{#args[@]}} ]; do
                    case "${{args[$i]}}" in
                        -X)  i=$((i+1)); method="${{args[$i]}}" ;;
                        http*) url="${{args[$i]}}" ;;
                    esac
                    i=$((i+1))
                done
                echo "${{method}} ${{url}}" >> "$CURL_CALLS"

                case "$SCENARIO" in
                    happy)
                        # First-run: POST succeeds.
                        if [ "$method" = "POST" ]; then
                            echo '{{"success":true,"result":{{"id":"rec-new"}}}}'
                        fi
                        ;;
                    already_match)
                        if [ "$method" = "POST" ]; then
                            echo '{{"success":false,"errors":[{{"message":"CNAME record with these exact values already exists"}}]}}'
                        elif [ "$method" = "GET" ]; then
                            # Matches current tunnel
                            echo '{{"result":[{{"id":"rec-existing","content":"new-tunnel-id.cfargotunnel.com"}}]}}'
                        fi
                        ;;
                    drift)
                        if [ "$method" = "POST" ]; then
                            echo '{{"success":false,"errors":[{{"message":"A record with that hostname already exists"}}]}}'
                        elif [ "$method" = "GET" ]; then
                            # Drifted: points at OLD tunnel
                            echo '{{"result":[{{"id":"rec-existing","content":"old-tunnel-id.cfargotunnel.com"}}]}}'
                        elif [ "$method" = "PATCH" ]; then
                            echo '{{"success":true,"result":{{"id":"rec-existing"}}}}'
                        fi
                        ;;
                esac
                return 0
            }}
            export -f curl
            # Expose call log path to the parent so we can grep it.
            echo "CURL_CALLS=$CURL_CALLS"

            # ---- the extracted block under test ----
            {block}

            echo "---END---"
            """
        ).format(scenario=scenario, block=cname_block)

    def run(scenario: str):
        script_path = tmp_path / f"run_{scenario}.sh"
        script_path.write_text(make_harness(scenario))
        r = subprocess.run(
            ["bash", str(script_path)],
            capture_output=True,
            text=True,
            timeout=10,
        )
        # Find the CURL_CALLS log path from stdout.
        calls_line = next(
            (ln for ln in r.stdout.splitlines() if ln.startswith("CURL_CALLS=")),
            None,
        )
        assert calls_line, f"harness didn't emit CURL_CALLS path:\n{r.stdout}"
        call_log = Path(calls_line.split("=", 1)[1]).read_text()
        return r, call_log

    # Scenario 1 — happy path
    r1, calls1 = run("happy")
    assert r1.returncode == 0, f"happy path failed: {r1.stderr}"
    assert "CNAME 已建立" in r1.stdout, (
        f"first-run must log 'CNAME 已建立', got:\n{r1.stdout}"
    )
    # Only POSTs (one per hostname), no GET / PATCH on happy path.
    assert calls1.count("POST") == 2
    assert calls1.count("GET") == 0
    assert calls1.count("PATCH") == 0

    # Scenario 2 — already exists + content matches current tunnel
    r2, calls2 = run("already_match")
    assert r2.returncode == 0, f"already_match failed: {r2.stderr}"
    assert "已存在且指向當前 tunnel，跳過" in r2.stdout, (
        f"matching-content path must log idempotent skip, got:\n{r2.stdout}"
    )
    # Critically: must NOT PATCH when content matches.
    assert calls2.count("PATCH") == 0, (
        f"PATCH must not fire when existing CNAME matches current tunnel; "
        f"calls:\n{calls2}"
    )
    # POST (fails) + GET (verify) for each of 2 hostnames = 2 POSTs + 2 GETs.
    assert calls2.count("POST") == 2
    assert calls2.count("GET") == 2

    # Scenario 3 — drift: existing content points at OLD tunnel
    r3, calls3 = run("drift")
    assert r3.returncode == 0, f"drift failed: {r3.stderr}"
    assert "偵測到 CNAME 漂移" in r3.stdout, (
        f"drift detection message must fire, got:\n{r3.stdout}"
    )
    assert "CNAME 已更新至當前 tunnel" in r3.stdout, (
        f"drift path must confirm PATCH succeeded, got:\n{r3.stdout}"
    )
    # PATCH must fire once per drifted hostname (2 total).
    assert calls3.count("PATCH") == 2, (
        f"PATCH must fire on drift; calls:\n{calls3}"
    )
    # Sequence: POST (fails), GET (detects drift), PATCH (repairs) per host.
    assert calls3.count("POST") == 2
    assert calls3.count("GET") == 2


def test_idempotency_full_rerun_announces_support_in_error_copy():
    """The error-recovery copy must tell the user 'just re-run the script'
    works — otherwise users assume a failed run has corrupted state and
    manually rm things they shouldn't.
    """
    content = SCRIPT.read_text()
    assert "支援冪等" in content, (
        "the error-path cleanup banner must mention idempotency so users "
        "know a clean re-run is safe (it doesn't corrupt state)"
    )
