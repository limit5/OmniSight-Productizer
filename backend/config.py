import logging
import os

from pydantic_settings import BaseSettings

_startup_logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    app_name: str = "OmniSight Engine"
    debug: bool = False  # Set OMNISIGHT_DEBUG=true for development
    env: str = ""  # "production" triggers hard security checks (exit 78 on failure)
    api_prefix: str = "/api/v1"

    # Frontend origin for CORS (comma-separated for multiple origins)
    frontend_origin: str = "http://localhost:3000"
    extra_cors_origins: str = ""  # Additional CORS origins, comma-separated

    # Database
    database_path: str = ""  # SQLite path (empty = default data/omnisight.db)
    # G4 #2 (HA-04): full DATABASE_URL overrides database_path. Accepts both
    # `sqlite:///path/to.db` and `postgresql+asyncpg://user:pw@host:5432/db`.
    # When set, the abstraction in backend/db_url.py dispatches to the right
    # async driver (aiosqlite / asyncpg). Legacy callers that only read
    # `database_path` continue to work for SQLite deployments.
    database_url: str = ""

    # I9/I10: Redis (rate limiting, multi-worker shared state)
    redis_url: str = ""  # e.g. redis://localhost:6379/0

    # I10: uvicorn worker count (0 = auto: CPU_count / 2, min 2)
    workers: int = 0

    # ── LLM Provider Configuration ──
    # Which provider to use: anthropic | google | openai | xai | groq | deepseek | together | ollama
    llm_provider: str = "anthropic"

    # Model name per provider (defaults below, override via env)
    llm_model: str = ""  # auto-selected per provider if empty

    # API keys (set via environment variables)
    anthropic_api_key: str = ""
    google_api_key: str = ""
    openai_api_key: str = ""
    xai_api_key: str = ""
    groq_api_key: str = ""
    deepseek_api_key: str = ""
    together_api_key: str = ""

    # OpenRouter (aggregator — single key accesses 200+ models)
    openrouter_api_key: str = ""

    # Ollama (local, no key needed)
    ollama_base_url: str = "http://localhost:11434"

    # LLM parameters
    llm_temperature: float = 0.3

    # ── Git Authentication ──
    git_ssh_key_path: str = "~/.ssh/id_ed25519"  # Default SSH key (fallback)
    github_token: str = ""  # Default GitHub PAT (fallback)
    gitlab_token: str = ""  # Default GitLab PAT (fallback)
    gitlab_url: str = ""  # Default self-hosted GitLab URL (fallback)
    # Multi-repo credential maps (JSON: {"host": "token/path"})
    git_credentials_file: str = ""  # Path to git_credentials.yaml (optional)
    git_ssh_key_map: str = ""       # JSON: {"github.com": "~/.ssh/id_github", ...}
    github_token_map: str = ""      # JSON: {"github.com": "ghp_...", "github.enterprise.com": "ghp_..."}
    gitlab_token_map: str = ""      # JSON: {"gitlab.com": "glpat-...", "gitlab.internal.com": "glpat-..."}

    # ── Token Budget & Resilience ──
    token_budget_daily: float = 0.0  # USD per day (0 = unlimited)
    # L1-06: hourly burn-rate kill-switch. A daily budget catches slow
    # drift; an hourly cap catches the spike that blows a whole
    # month's budget in one runaway retry loop. Recommended: set to
    # token_budget_daily / 12 or so — calibrate once, adjust after
    # the first week of observed traffic. 0 = disabled.
    token_budget_hourly: float = 0.0
    token_warn_threshold: float = 0.8  # 80% → emit warning
    token_downgrade_threshold: float = 0.9  # 90% → auto switch to cheaper model
    token_freeze_threshold: float = 1.0  # 100% → stop all LLM calls
    token_fallback_provider: str = "ollama"  # Provider to downgrade to at 90%
    token_fallback_model: str = "llama3.1"  # Model to downgrade to at 90%
    llm_fallback_chain: str = "anthropic,openai,google,groq,deepseek,openrouter,ollama"  # Failover priority

    # ── RTK Output Compression ──
    rtk_enabled: bool = True  # Enable output compression for token savings
    rtk_compression_threshold: int = 1000  # Only compress outputs > this many bytes
    rtk_binary_timeout: float = 2.0  # Timeout for RTK binary (seconds)
    rtk_dedup_lines: bool = True  # Remove duplicate consecutive lines (Python fallback)
    rtk_strip_progress: bool = True  # Remove progress bars and spinner lines
    rtk_track_savings: bool = True  # Track compression metrics

    # ── Notification Routing ──
    notification_slack_webhook: str = ""  # Slack Incoming Webhook URL
    notification_slack_mention: str = ""  # Slack user ID to @ for L3 events
    notification_jira_url: str = ""  # Jira base URL (e.g. https://jira.company.com)
    notification_jira_token: str = ""  # Jira API token
    notification_jira_project: str = ""  # Jira project key (e.g. OMNI)
    notification_pagerduty_key: str = ""  # PagerDuty Events API v2 routing key
    notification_max_retries: int = 3     # Max retry attempts for failed dispatches
    notification_retry_backoff: int = 30  # Seconds between retry attempts (exponential)

    # ── R1 (#307) ChatOps Interactive Integration ──
    chatops_discord_webhook: str = ""     # Discord Incoming Webhook URL (outbound)
    chatops_discord_public_key: str = ""  # Discord application public key (interaction verify)
    chatops_teams_webhook: str = ""       # Teams Incoming Webhook URL (outbound)
    chatops_teams_secret: str = ""        # HMAC-SHA256 secret for Teams bot callback
    chatops_line_channel_token: str = ""  # Line Messaging API channel access token
    chatops_line_channel_secret: str = "" # Line channel secret (X-Line-Signature verify)
    chatops_line_to: str = ""             # Line target user / group ID (push)
    chatops_authorized_users: str = ""    # Comma-separated user IDs allowed to inject
    chatops_hint_rate_per_5min: int = 3   # Max inject hints per agent per 5-minute window
    chatops_hint_max_length: int = 2000   # Sanitized hint text max char length

    # ── Gerrit Code Review ──
    gerrit_enabled: bool = False  # Master switch for Gerrit integration
    gerrit_url: str = ""  # Web UI URL, e.g. "https://gerrit.sora.services"
    gerrit_ssh_host: str = ""  # SSH host for push + CLI, e.g. "gerrit.sora.services"
    gerrit_ssh_port: int = 29418  # Gerrit SSH port (default 29418)
    gerrit_project: str = ""  # Project path, e.g. "project/omnisight-core"
    gerrit_replication_targets: str = ""  # Comma-separated remote names for post-merge push
    # Multi-instance Gerrit (JSON list of {url, ssh_host, ssh_port, project, webhook_secret})
    gerrit_instances: str = ""

    # ── Webhook Secrets (External → Internal) ──
    gerrit_webhook_secret: str = ""     # Default Gerrit webhook secret (fallback)
    github_webhook_secret: str = ""     # HMAC-SHA256 signature verification
    gitlab_webhook_secret: str = ""     # X-Gitlab-Token header verification
    jira_webhook_secret: str = ""       # Bearer token verification

    # ── CI/CD Pipeline Triggers ──
    ci_github_actions_enabled: bool = False
    ci_jenkins_enabled: bool = False
    ci_jenkins_url: str = ""
    ci_jenkins_user: str = ""
    ci_jenkins_api_token: str = ""
    ci_gitlab_enabled: bool = False

    # ── Release Packaging ──
    github_repo: str = ""              # owner/repo slug for GitHub Releases API
    gitlab_project_id: str = ""        # GitLab project ID or URL-encoded path
    release_enabled: bool = False      # Enable release upload on merge
    release_draft: bool = True         # Create as draft (requires manual publish)

    # Docker isolation
    docker_enabled: bool = True  # enable container execution for agents
    docker_image: str = "omnisight-agent:latest"
    docker_network: str = "none"  # none = no network (secure), bridge = allow network
    docker_memory_limit: str = "1g"    # container memory cap
    docker_cpu_limit: str = "2"        # container CPU cap

    # Phase 64-A S1: Tier 1 sandbox runtime. "runsc" (gVisor) gives a
    # user-space kernel — blocks most container-escape CVEs. We auto-fall
    # back to "runc" if runsc isn't installed, so dev boxes (mac/WSL2)
    # still work; CI/prod should ensure runsc is present.
    docker_runtime: str = "runsc"  # runsc | runc

    # Phase 64-A S2: Tier 1 egress whitelist. DOUBLE-GATED on purpose —
    # opening any egress weakens the air-gap, so it requires both an
    # explicit hostname list AND the boolean `allow_egress` flag. Either
    # one missing → falls back to `--network none`.
    t1_allow_egress: bool = False
    t1_egress_allow_hosts: str = ""  # CSV: "github.com,gerrit.internal:29418"

    # Phase 64-A S3: image immutability check. CSV of acceptable
    # `sha256:...` digests for the agent docker image. Empty (default)
    # = no check, preserving today's behaviour. Set this in prod to
    # refuse a tampered/swapped image at launch time.
    docker_image_allowed_digests: str = ""

    # Phase 64-A S4: hard upper bound on a sandbox's wall-clock lifetime.
    # The watchdog SIGKILLs the container after this many seconds even
    # if its commands look "in progress" — this is the killswitch that
    # protects against infinite loops the per-command BASH_TIMEOUT
    # cannot catch (e.g. a build that legitimately sleeps for hours).
    # Set to 0 to disable (NOT recommended in prod). Default 45 min.
    sandbox_lifetime_s: int = 2700

    # Phase 64-C-SSH: SSH runner for cross-arch target boards.
    ssh_runner_enabled: bool = True
    ssh_runner_timeout: int = 300
    ssh_runner_heartbeat_interval: int = 30
    ssh_runner_max_output_bytes: int = 10_000
    ssh_credentials_file: str = ""

    # Phase 64-D D3: per-exec output size cap. If exec_in_container's
    # stdout+stderr exceeds this many bytes, we truncate and append a
    # one-line marker. Defends Tier-0 LLM context from being blown up
    # by a runaway command (gcc dumping 5 MB of warnings, for example).
    # 0 = disabled. Default 10 KB matches the design spec.
    sandbox_max_output_bytes: int = 10_000

    # O8 (#271): orchestration execution mode. "monolith" keeps every
    # agent run going through the LangGraph StateGraph in-process (legacy
    # path since v0.1.0). "distributed" routes the same run through
    # queue_backend.push → worker pool → Gerrit so a single orchestrator
    # can scale horizontally. Both modes MUST emit the same SSE event
    # sequence — parity is enforced by test_orchestration_mode.py. Default
    # stays "monolith" so upgrading the binary alone never changes runtime
    # behaviour; operators flip the env var explicitly per tenant/stage.
    orchestration_mode: str = "monolith"
    # O8 rollback knob: how long dispatch() will block waiting for a
    # distributed worker to finish before falling back to the monolith
    # path. 0 disables the fallback (production default — surface the
    # timeout instead of silently executing twice). Non-zero enables the
    # grey-deploy dual-write: useful during migration to see "did the
    # queue eat my task?" without blocking the user indefinitely.
    orchestration_distributed_wait_s: float = 600.0

    # H2 row 1514: Coordinator turbo auto-derate opt-out.
    # When true (default), a sustained host-CPU breach (> 80% for 30s) will
    # transparently shrink turbo's parallel budget from 8 → 2 (supervised).
    # Operators on a dedicated host who would rather ride out a spike than
    # lose throughput can set this to false. Because the safety net is then
    # off, switching INTO turbo mode while h2_auto_derate=false requires an
    # explicit `confirm_turbo=True` hand-off (API: ?confirm_turbo=true) so
    # the caller can't accidentally put a host under OOM pressure with no
    # backstop.
    h2_auto_derate: bool = True

    # M5: prewarm pool multi-tenant safety. Values:
    #   * "per_tenant" — pool bucketed by tenant_id; A's prewarm cannot
    #     be consumed by B (default; SaaS-safe).
    #   * "shared" — single global pool (legacy v1 behavior). Faster but
    #     cross-tenant filesystem residue risk; only safe for single
    #     tenant or fully-trusted deployments.
    #   * "disabled" — skip prewarm entirely, trade 300ms cold-start for
    #     zero speculative-container residue (high-security customers).
    # Orthogonal to OMNISIGHT_PREWARM_ENABLED: if the env flag is off,
    # prewarm is off regardless. When the env flag is on, this policy
    # controls bucketing + cleanup semantics.
    prewarm_policy: str = "per_tenant"

    model_config = {"env_file": ".env", "env_prefix": "OMNISIGHT_"}

    def get_model_name(self) -> str:
        """Return the model name, using provider-specific defaults if not set."""
        if self.llm_model:
            return self.llm_model
        defaults = {
            "anthropic": "claude-sonnet-4-20250514",
            "google": "gemini-1.5-pro",
            "openai": "gpt-4o",
            "xai": "grok-3-mini",
            "groq": "llama-3.3-70b-versatile",
            "deepseek": "deepseek-chat",
            "together": "meta-llama/Meta-Llama-3.1-70B-Instruct-Turbo",
            "ollama": "llama3.1",
        }
        return defaults.get(self.llm_provider, "claude-sonnet-4-20250514")


settings = Settings()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  L1-03: startup-time config validation
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#
# Called from backend.main lifespan. Catches common deploy-day
# mistakes that would otherwise manifest as confusing 401s or
# silent "provider unreachable" logs hours later.

def _mask(v: str, show: int = 4) -> str:
    """Redact secrets for logging: keep first + last `show` chars."""
    if not v:
        return "(empty)"
    if len(v) <= show * 2 + 3:
        return "***"
    return f"{v[:show]}…{v[-show:]}"


# Provider key prefixes we recognise. Wrong prefix = almost certainly
# a copy-paste error; we warn but don't block so private deployments
# (proxies, Bedrock-style wrappers) still work.
_PROVIDER_PREFIXES: dict[str, tuple[str, ...]] = {
    "anthropic_api_key":  ("sk-ant-",),
    "openai_api_key":     ("sk-", "sk-proj-"),
    "google_api_key":     (),            # Google keys are varied
    "xai_api_key":        ("xai-",),
    "groq_api_key":       ("gsk_",),
    "deepseek_api_key":   ("sk-",),
    "together_api_key":   (),            # Together keys are varied
    "openrouter_api_key": ("sk-or-",),
}

_MIN_BEARER_LEN = 16  # 128-bit entropy, roughly


class ConfigValidationError(SystemExit):
    """Startup-time settings rejected — refuse to boot (exit 78 = EX_CONFIG)."""
    def __init__(self, message: str):
        super().__init__(78)
        self.message = message

    def __str__(self) -> str:
        return self.message


def validate_startup_config(strict: bool | None = None) -> list[str]:
    """Sanity-check the loaded settings + critical env vars. Returns a
    list of warnings (empty = clean). Raises `ConfigValidationError`
    on a *hard* problem if strict mode is on.

    `strict` defaults to True when `settings.debug == False`, False
    when debug is on — dev workflow stays lenient, prod boots refuse
    to start with a known-dangerous config.
    """
    if strict is None:
        strict = not settings.debug

    warnings: list[str] = []
    hard_errors: list[str] = []

    # ── Bearer token ──
    bearer = (os.environ.get("OMNISIGHT_DECISION_BEARER") or "").strip()
    if bearer:
        if len(bearer) < _MIN_BEARER_LEN:
            msg = (
                f"OMNISIGHT_DECISION_BEARER is only {len(bearer)} chars "
                f"(min {_MIN_BEARER_LEN}). Brute-forcing is cheap at "
                "that size — use at least 16."
            )
            (hard_errors if strict else warnings).append(msg)
    else:
        # Empty bearer leaves DE mutator endpoints open. Fine in dev,
        # foot-gun in prod.
        warnings.append(
            "OMNISIGHT_DECISION_BEARER is empty — Decision Engine "
            "mutator endpoints (approve/reject/undo/mode) are OPEN."
        )

    # ── Provider key shape ──
    for field, prefixes in _PROVIDER_PREFIXES.items():
        value = getattr(settings, field, "") or ""
        value = value.strip()
        if not value or not prefixes:
            continue
        if not any(value.startswith(p) for p in prefixes):
            warnings.append(
                f"{field} doesn't start with any known prefix "
                f"({'/'.join(prefixes)}); may be a paste error. "
                f"Loaded as: {_mask(value)}"
            )

    # ── LLM provider sanity: the *selected* provider must have a
    #    matching key (unless it's ollama which is local/no-auth, or
    #    the debug dev profile). ──
    provider = (settings.llm_provider or "").strip().lower()
    if provider and provider != "ollama":
        key_field = f"{provider}_api_key"
        if not getattr(settings, key_field, ""):
            msg = (
                f"llm_provider={provider!r} but {key_field.upper()} "
                "is empty — every LLM call will fail. Either set the "
                "key or switch llm_provider to 'ollama'."
            )
            (hard_errors if strict else warnings).append(msg)

    # ── T1 sandbox egress consistency ──
    if settings.t1_allow_egress and not settings.t1_egress_allow_hosts:
        warnings.append(
            "t1_allow_egress=true but t1_egress_allow_hosts is empty — "
            "no hosts will actually be reachable. Did you forget the "
            "whitelist?"
        )

    # ── M5: prewarm policy whitelist ──
    policy = (settings.prewarm_policy or "").strip().lower()
    if policy not in {"disabled", "shared", "per_tenant"}:
        msg = (
            f"OMNISIGHT_PREWARM_POLICY={settings.prewarm_policy!r} is "
            "invalid. Valid: disabled / shared / per_tenant. "
            "Defaulting to 'per_tenant' at runtime."
        )
        (hard_errors if strict else warnings).append(msg)
    elif policy == "shared":
        warnings.append(
            "OMNISIGHT_PREWARM_POLICY=shared — pre-warmed containers are "
            "not tenant-bucketed. Only safe on single-tenant or fully-"
            "trusted deployments. Use 'per_tenant' for SaaS."
        )

    # ── Internet-exposure auth (Phase 54 + L1) ──
    # The single most common foot-gun on first deploy: auth_mode=open
    # (the default) is fine for a dev box, fatal for an exposed URL.
    auth_mode = (os.environ.get("OMNISIGHT_AUTH_MODE") or "open").strip().lower()
    if auth_mode not in {"open", "session", "strict"}:
        hard_errors.append(
            f"OMNISIGHT_AUTH_MODE={auth_mode!r} unknown. "
            "Valid: open / session / strict."
        )
    elif auth_mode == "open":
        msg = (
            "OMNISIGHT_AUTH_MODE=open — the dashboard treats every "
            "request as admin. Acceptable on a dev box; never on an "
            "exposed URL. Set OMNISIGHT_AUTH_MODE=strict in prod."
        )
        (hard_errors if strict else warnings).append(msg)

    # K1: production environment MUST use strict auth mode.
    # Exit code 78 (EX_CONFIG from sysexits.h) signals a configuration
    # error to container orchestrators.
    env_name = (settings.env or os.environ.get("OMNISIGHT_ENV") or "").strip().lower()
    if env_name == "production" and auth_mode != "strict":
        hard_errors.append(
            f"ENV=production requires OMNISIGHT_AUTH_MODE=strict "
            f"(current: {auth_mode!r}). Refusing to start — exit 78."
        )

    # The bootstrap admin password ships as `omnisight-admin`. Hard-
    # fail if that's still in use under prod, so an internet-exposed
    # instance can't have its default admin trivially logged into.
    admin_pw = (os.environ.get("OMNISIGHT_ADMIN_PASSWORD") or "").strip()
    if not admin_pw:
        warnings.append(
            "OMNISIGHT_ADMIN_PASSWORD unset — bootstrap admin will use "
            "the dev default 'omnisight-admin'. SET THIS before exposing "
            "the URL or change the admin's password via /users/{id}."
        )
    elif admin_pw == "omnisight-admin":
        msg = (
            "OMNISIGHT_ADMIN_PASSWORD is the literal default "
            "'omnisight-admin'. Refuse to start in prod — this is a "
            "well-known credential."
        )
        (hard_errors if strict else warnings).append(msg)
    elif len(admin_pw) < 12:
        warnings.append(
            f"OMNISIGHT_ADMIN_PASSWORD is only {len(admin_pw)} chars; "
            "use at least 12. Better: a passphrase."
        )

    # When cookies cross a TLS boundary they MUST be `Secure` —
    # cloudflared terminates HTTPS, but the Secure flag must still be
    # set so a session cookie can't accidentally leak over a future
    # plain-HTTP path (custom browser, dev proxy, etc.).
    if auth_mode != "open" and (os.environ.get("OMNISIGHT_COOKIE_SECURE") or "").strip().lower() != "true":
        warnings.append(
            "OMNISIGHT_COOKIE_SECURE not set — session cookies are "
            "shipped without the Secure flag. Set to 'true' once you "
            "have HTTPS (Cloudflare Tunnel terminates TLS, so this is "
            "the right value behind it)."
        )

    # ── Masked summary at startup ──
    _startup_logger.info(
        "config loaded: provider=%s model=%s debug=%s "
        "bearer=%s db=%s docker_runtime=%s t1_egress=%s",
        settings.llm_provider, settings.get_model_name(), settings.debug,
        "set" if bearer else "UNSET",
        settings.database_path or "data/omnisight.db",
        settings.docker_runtime,
        "ON" if settings.t1_allow_egress else "off",
    )

    for w in warnings:
        _startup_logger.warning("config: %s", w)
    if hard_errors:
        for e in hard_errors:
            _startup_logger.error("config: %s", e)
        if strict:
            raise ConfigValidationError(
                "Refusing to start — " + "; ".join(hard_errors),
            )

    return warnings
