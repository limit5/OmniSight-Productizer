import logging
import os

from pydantic_settings import BaseSettings

_startup_logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    app_name: str = "OmniSight Engine"
    debug: bool = False  # Set OMNISIGHT_DEBUG=true for development
    # H7 audit (2026-04-19): dedicated CI bypass for startup validation
    # so the e2e job doesn't have to re-purpose OMNISIGHT_DEBUG. DEBUG
    # carries "chatty logs + dev shortcuts"; CI_MODE carries ONLY
    # "strict-mode validation relaxed". Keeping them independent means
    # a future debug-only hot-path (verbose log, extra metric) won't
    # accidentally leak through CI — and a CI-specific mode won't
    # accidentally bring dev-only code paths into the probe.
    ci_mode: bool = False  # Set OMNISIGHT_CI_MODE=true only in CI runners
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
    # Default model for the ollama provider. Empty → the llm_adapter
    # falls back to its hardcoded ``llama3.1`` literal, which is fine
    # for dev boxes that preloaded that model but wrong for every
    # ollama deployment that didn't (e.g. Path B production where
    # the ``ai_engine`` container only carries ``gemma4:*`` +
    # ``gemma2:*`` + ``nomic-embed-text``). Keeping this as a
    # separate field (rather than reusing ``llm_model``) means
    # ``llm_model`` can stay pinned to the primary provider's model
    # (e.g. ``claude-opus-4-7`` for Anthropic) while the ollama
    # fallback path still resolves to a model that's actually loaded.
    # Consumed by ``backend.agents.llm::get_llm`` in the per-
    # provider model resolution branch.
    ollama_model: str = ""

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

    # ── BP.N.3 Web Search Tool knobs ──
    # Provider selection is intentionally default-off. BP.N.1 shipped the
    # Tavily client and per-tenant spend tracker; BP.N.3 only declares the
    # operator knobs so later BP.N rows can wire the tool into selected
    # guild loadouts without surprising existing deployments.
    #
    # ``web_search_provider``: none | tavily | exa | perplexity. Only
    # Tavily has a concrete client in BP.N.1; Exa/Perplexity are accepted
    # config values so operators can stage env before their adapters land.
    # ``web_search_daily_budget_usd`` is a per-tenant daily cap passed to
    # ``backend.web_search.WebSearchCostTracker``.
    #
    # Module-global state audit (Step 1, type-1): Settings values are
    # immutable literals derived by each worker from the same env/.env
    # source; mutable per-tenant spend state remains in Redis or the
    # documented per-worker dev fallback inside ``backend.web_search``.
    web_search_provider: str = "none"
    web_search_daily_budget_usd: float = 5.00

    # ── W11.2 Website Cloning Backend ──
    # Which CloneSource backend the W11 cloning pipeline picks up at
    # runtime. Empty = auto-select (Firecrawl when an API key is in env,
    # else Playwright). Operators that want to *force* the air-gap path
    # even when a Firecrawl key is configured (dev box mirroring prod
    # creds; regulated tenant sharing config bundle) flip this to
    # ``playwright`` explicitly. Honored by
    # ``backend.web.make_clone_source(settings=...)``.
    clone_backend: str = ""  # "" | firecrawl | playwright
    # Firecrawl SaaS API key. Empty = SaaS backend disabled even if
    # ``clone_backend`` is set to ``firecrawl`` (constructor raises
    # FirecrawlConfigError so the failure surfaces at boot rather than
    # on first clone request).
    firecrawl_api_key: str = ""
    # Override for the Firecrawl base URL — useful for operators
    # running the OSS Firecrawl server in a private network. Empty =
    # the default ``https://api.firecrawl.dev`` SaaS endpoint.
    firecrawl_base_url: str = ""
    # Browser to drive when the Playwright self-host backend is in use.
    # One of ``chromium`` / ``firefox`` / ``webkit``; empty = chromium.
    # Pinned in config (vs hardcoded in PlaywrightSource) so air-gap
    # operators can swap engines without code change.
    playwright_browser: str = ""

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
    # Y-prep.3 (#289) — JIRA inbound automation routing knobs. Promoted
    # out of pure ``OMNISIGHT_*`` env vars so the Notifications-tab UI
    # can edit them and ``_SHARED_KV_STR_FIELDS`` mirrors them across
    # uvicorn workers (otherwise a wizard-side edit on worker-A would
    # leave workers B/C/D treating events on the old whitelist until
    # restart). Both are CSV-friendly: ``jira_intake_label`` accepts a
    # single label today (``omnisight-intake`` default) but the router
    # is forward-compatible if we widen to multi-label OR; ``jira_done_statuses``
    # is a CSV list of "consider this status terminal" values that fire
    # the artifact-packaging pipeline (default ``Done,Closed`` matches
    # vanilla JIRA workflows). Empty string ⇒ router falls back to the
    # built-in defaults — same precedence as the env-var path.
    jira_intake_label: str = ""           # Default ``omnisight-intake`` if empty
    jira_done_statuses: str = ""          # CSV; default ``Done,Closed`` if empty
    notification_pagerduty_key: str = ""  # PagerDuty Events API v2 routing key
    # R9 row 2936 (#315) — L4_SMS leg for P1 (系統崩潰) fan-out. Empty
    # disables the SMS leg (PagerDuty alone covers the paging tier in
    # that case). Generic HTTP webhook URL (Twilio Programmable SMS
    # gateway, AWS SNS HTTP endpoint, or operator-side SMS bridge);
    # ``_send_sms`` POSTs ``{to, message, severity, source}`` JSON.
    notification_sms_webhook: str = ""    # SMS gateway webhook URL (P1)
    notification_sms_to: str = ""         # SMS destination (CSV; on-call phone number(s))
    notification_max_retries: int = 3     # Max retry attempts for failed dispatches
    notification_retry_backoff: int = 30  # Seconds between retry attempts (exponential)
    # R9 row 2940 (#315) — L1_LOG_EMAIL leg for P3 (自動修復中) digest.
    # All empty = digest still fires but only emits a structured summary
    # log line (no SMTP send). Operators piping log aggregator → email
    # can still get a digest without configuring SMTP here.
    notification_email_smtp_host: str = ""     # SMTP hostname (empty = log-only fallback)
    notification_email_smtp_port: int = 587    # SMTP port (587=submission/STARTTLS; 465=SMTPS; 25=plain)
    notification_email_smtp_user: str = ""     # SMTP auth user (empty = no auth)
    notification_email_smtp_password: str = "" # SMTP auth password
    notification_email_smtp_use_tls: bool = True  # STARTTLS on port 587
    notification_email_from: str = ""          # From: header (defaults to smtp_user when empty)
    notification_email_to: str = ""            # CSV list of recipient addresses
    notification_email_digest_interval_s: int = 3600  # 1h default — P3 is informational
    notification_email_digest_max_buffer: int = 500   # Cap buffer; drop oldest on overflow

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
    email_webhook_secret: str = ""      # FS.4.3 email bounce/complaint webhook token

    # ── FS.8 Stripe / Billing ──
    # Read per request by ``backend.stripe_billing``. There is no
    # module-global cache; every worker derives the same values from
    # Settings/env and sends Stripe-hosted sessions to the caller.
    stripe_secret_key: str = ""
    stripe_checkout_price_id: str = ""
    stripe_checkout_success_url: str = ""
    stripe_checkout_cancel_url: str = ""
    stripe_billing_portal_return_url: str = ""
    stripe_webhook_secret: str = ""
    stripe_api_base_url: str = "https://api.stripe.com/v1"

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

    # M4 audit (2026-04-19): per-user LLM-call rate limit.
    # Prevents a single authenticated user from exhausting the global
    # token budget by spamming requests. This is the INTERIM fix —
    # proper per-tenant daily $ budget enforcement (the audit's full
    # ask) requires a schema migration + per-request usage logging
    # and is tracked as a follow-up PR. This cap catches the abuse
    # pattern that matters most in practice: "one user fires 1000
    # expensive /invoke calls". Default 200/h is generous for real
    # work (human-paced rarely exceeds 30/h) but hard-stops abuse.
    # Set to 0 to disable.
    llm_calls_per_user_per_hour: int = 200

    # M7 audit (2026-04-19): optional bearer token for /metrics.
    # Leave empty → endpoint is open (backwards compatible; Next.js
    # rewrites don't expose it to the internet anyway). Set to a
    # strong random secret → /metrics requires that token via
    # `?token=<value>` or `Authorization: Bearer <value>`.
    metrics_token: str = ""

    # S2-9 (#354) — auth-by-default baseline mode. Declared here so the
    # pydantic Settings extra='forbid' gate doesn't reject the key when
    # it's present in .env; the middleware itself reads os.environ
    # directly so changing the mode at runtime stays a single env-var
    # write without a Settings re-instantiation. Value is one of
    # "log" | "enforce" | "off" — see backend/auth_baseline.py.
    auth_baseline_mode: str = "log"

    # H6 audit (2026-04-19): Tier-2 "networked" sandbox — operator gate.
    # T2 gives a sandbox container bridge-network access via the
    # omnisight-egress-t2 iptables ACL. It's still safer than host
    # networking but widens the attack surface vs. Tier-1 `--network
    # none`. Before this gate existed, any call site that asked for
    # tier="networked" got the bridge with no env-level check; a
    # compromised LLM / backend could therefore issue networked tasks
    # without ops approval. Keep false in production; set to true only
    # on deployments that have a written reason to need T2 egress.
    t2_networked_tier_allowed: bool = False

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

    # ── Y6 #282 row 2 — Workspace hierarchy root + per-tenant default quota ──
    # Promoted to Settings so operators can re-target the on-disk layout
    # away from the legacy ``./.agent_workspaces/`` tree without touching
    # source. ``workspace_root`` is the parent of the row-1 five-layer
    # hierarchy ``{root}/{tenant_id}/{product_line}/{project_id}/
    # {agent_id}/{repo_url_hash}/`` — change it once and all of provision,
    # cleanup_orphan_worktrees, the row-4 migration script and the row-6
    # GC reaper follow. The default ``./data/workspaces`` aligns with the
    # rest of the runtime data dir (`data/omnisight.db`,
    # `data/audit_chain/...`) so a single bind-mount in compose covers
    # everything stateful, while the legacy `.agent_workspaces` dir is
    # left untouched until the row-4 migrator moves it. Resolved relative
    # to the process CWD when not absolute (matches the existing
    # ``database_path`` semantic). Read at module import via
    # ``backend.workspace`` only — never re-read at request time, so a
    # mid-flight change requires a restart (the on-disk hierarchy itself
    # is the authoritative state, not the env knob).
    #
    # ``workspace_quota_mb_default`` is the per-tenant **default** soft
    # cap that row-5 (`tenant_quota.check_hard_quota`) consults when a
    # tenant has no explicit override row in `tenant_quota_overrides`.
    # 0 = unlimited (current behaviour, preserved as default until row-5
    # ships the actual enforcement so flipping just this row cannot
    # silently start denying writes). Operators set it non-zero once
    # they want fleet-wide enforcement; per-tenant overrides remain the
    # finer-grained knob. Unit is MB (not bytes) to match how operators
    # think about disk and how the Tenants UI surfaces quota.
    #
    # Module-global state audit (per implement_phase_step.md Step 1,
    # type-1 answer): both fields are immutable Settings literals
    # derived once at process boot from env / .env — every uvicorn
    # worker computes the same value from the same source so cross-
    # worker consistency is automatic.
    workspace_root: str = "./data/workspaces"
    workspace_quota_mb_default: int = 0

    # ── Y6 #282 row 6 — Background workspace GC reaper ──
    # Hourly async task (lifespan-scoped, see ``backend/workspace_gc.py``)
    # that walks the row-1 hierarchy and reclaims disk from agents that
    # are long-finished but whose worktrees were never explicitly cleaned
    # up. Three knobs:
    #
    # ``keep_recent_workspaces_stale_days`` — leaf workspaces whose
    # ``mtime`` is older than this AND whose ``agent_id`` is not in the
    # in-process active registry (``backend.workspace._workspaces``)
    # AND whose ``.git/index.lock`` is missing-or-stale get moved into
    # ``{workspace_root}/_trash/`` for cool-down.
    #
    # ``workspace_gc_trash_ttl_days`` — entries inside ``_trash/`` whose
    # *own* ``mtime`` (= the move-to-trash time) is older than this get
    # hard-deleted. Decoupled from the stale-days knob so operators can
    # tune "how long until we touch you" and "how long after we trashed
    # you we keep the remains" independently.
    #
    # ``workspace_gc_interval_s`` — sweep cadence. Default 3600s (1 h)
    # matches the Y6 row-6 spec; lower bound is soft (the sweep itself
    # is cheap, but running more often than once per minute offers no
    # benefit because the staleness window is days).
    #
    # Module-global state audit (per implement_phase_step.md Step 1,
    # type-1 answer): all three fields are immutable Settings literals
    # derived once at process boot from env / .env — every uvicorn
    # worker reads the same value from the same source. Per-worker GC
    # loops racing on the same on-disk hierarchy is type-3 intentional
    # (filesystem ops are idempotent — ``shutil.rmtree`` is naturally
    # idempotent against missing paths, ``rename(src, dst)`` either
    # wins or sees ENOENT and the loser quietly drops the file from
    # its candidate set; see ``backend/workspace_gc.py`` module
    # docstring).
    keep_recent_workspaces_stale_days: int = 30
    workspace_gc_trash_ttl_days: int = 7
    workspace_gc_interval_s: float = 3600.0

    # ── W14.3 — Live web-preview CF Tunnel ingress ──
    # Dynamic per-sandbox ingress rules in the operator's existing
    # Cloudflare Tunnel (B12). When ``OMNISIGHT_TUNNEL_HOST`` is set,
    # ``backend.routers.web_sandbox.get_manager()`` instantiates a
    # ``CFIngressManager`` and threads it into ``WebSandboxManager``;
    # on launch the manager calls ``PUT /accounts/{id}/cfd_tunnel/
    # {id}/configurations`` to add ``preview-{sandbox_id}.{tunnel_host}``
    # → ``http://127.0.0.1:{host_port}/``, on stop it removes that rule.
    # Empty ``tunnel_host`` ⇒ W14.3 is OFF and ``ingress_url`` stays
    # ``None`` (the W14.2 host-port URL is the only reachable address —
    # equivalent to the deployed-inactive pre-W14.3 dev path). All four
    # knobs are needed — partial config logs a warning at construction
    # and falls back to OFF rather than 500'ing every launch.
    # Module-global state audit (Step 1, type-1): values are immutable
    # Settings literals — every uvicorn worker derives the same
    # ``CFIngressManager`` config from the same source. Cross-worker
    # consistency: each worker fetches the live tunnel config before
    # mutating it, so the canonical state is the CF API itself (Step 1,
    # type-2). Race on simultaneous launches is bounded — workers
    # idempotently merge their own hostname; W14.10 will replace this
    # with PG-serialised mutation when alembic 0059 lands.
    tunnel_host: str = ""           # e.g. "ai.sora-dev.app"
    cf_api_token: str = ""          # CF API token (Account:Cloudflare Tunnel:Edit)
    cf_account_id: str = ""         # CF account UUID
    cf_tunnel_id: str = ""          # CF tunnel UUID

    # ── W14.4 — Cloudflare Access SSO lock for live web-preview ──
    # On top of W14.3's dynamic ingress rule, every per-sandbox public
    # hostname is registered as a Cloudflare Access self-hosted app so
    # an unauthenticated visitor is bounced to the operator's CF Access
    # IdP (Google / GitHub / OIDC etc). The app's policy carries the
    # launching operator's email + the optional admin allowlist below,
    # so the OIDC token CF Access issues lines up with the OmniSight
    # session that requested the preview.
    #
    # All four CF env knobs (TUNNEL_HOST + CF_API_TOKEN + CF_ACCOUNT_ID
    # + CF_ACCESS_TEAM_DOMAIN) are required to enable W14.4 — partial
    # config logs an info-level note at construction and falls back to
    # the W14.3-only path (publicly-reachable URL, no SSO gate). Note
    # that W14.4 needs an additional ``Account:Cloudflare Access:Edit``
    # scope on top of the W14.3 ``Account:Cloudflare Tunnel:Edit``
    # scope; reusing a single CF API token with both scopes is the
    # recommended deployment.
    #
    # ``cf_access_team_domain`` — ``<team>.cloudflareaccess.com``;
    # used as the OIDC issuer URL the JWT ``iss`` claim carries (and
    # for the dashboard URL the operator hits to manage policies).
    #
    # ``cf_access_default_emails`` — CSV of admin emails always added
    # to every per-sandbox policy. Useful so an on-call admin can take
    # over a preview without the launching operator's session being
    # live. Empty ⇒ only the launching operator's email is in the
    # policy.
    #
    # ``cf_access_session_duration`` — how long a successful login
    # lasts before CF Access re-authenticates. Format ``Ns/Nm/Nh/Nd``.
    # Default 30m.
    #
    # ``cf_access_aud_tag`` — Optional fixed CF Access AUD UUID. When
    # set, downstream JWT verifiers (W14.6 frontend handler, W14.7 HMR
    # proxy) can enforce ``claims["aud"]`` carries it. Empty ⇒ AUD is
    # not enforced (CF Access still verifies the signature; only the
    # in-OmniSight cross-check is relaxed).
    #
    # Module-global state audit (Step 1, type-1): values are immutable
    # Settings literals — every uvicorn worker derives the same
    # ``CFAccessManager`` config from the same source. Cross-worker
    # consistency: each worker fetches the live applications list
    # before mutating it, so the canonical state is the CF Access
    # API itself (Step 1, type-2). Race window on simultaneous launches
    # for the same sandbox_id is bounded by the loser's idempotent
    # GET-by-name look-up; W14.10 will replace this with PG-serialised
    # mutation when alembic 0059 lands.
    cf_access_team_domain: str = ""       # e.g. "acme.cloudflareaccess.com"
    cf_access_default_emails: str = ""    # CSV: "admin@example.com,oncall@example.com"
    cf_access_session_duration: str = ""  # default "30m" when empty
    cf_access_aud_tag: str = ""           # Optional fixed CF Access AUD UUID

    # ── W14.5 — Live web-preview idle-timeout auto-kill reaper ──
    # Per-uvicorn-worker daemon thread that walks
    # :class:`backend.web_sandbox.WebSandboxManager` once per
    # ``web_sandbox_reap_interval_s`` and calls ``stop(reason=
    # "idle_timeout")`` on any sandbox whose ``last_request_at`` is
    # more than ``web_sandbox_idle_timeout_s`` behind the wall clock.
    # ``stop()`` already removes the W14.3 CF Tunnel ingress rule and
    # W14.4 CF Access SSO app on its way out, so an idle-killed sandbox
    # also frees the public hostname slot — that is the "刪 ingress"
    # half of the W14.5 row.
    #
    # Default ``1800.0`` (30 min) matches the W14.5 row spec; lower
    # values are useful only for unit tests that want to drive the
    # reaper deterministically (the reaper module enforces a soft
    # floor of 1.0s + ceiling of 86400.0s on the timeout, and a
    # 0.05s..3600s range on the interval, so a misconfigured knob
    # raises ``IdleReaperError`` at construction rather than silently
    # disabling the sweep).
    #
    # Module-global state audit (Step 1, type-3): the reaper holds a
    # per-instance ``threading.Thread`` + ``threading.Event`` — bound
    # to the per-worker :class:`backend.web_sandbox.WebSandboxManager`
    # singleton. Each worker reaps only the sandboxes it itself
    # launched; cross-worker reaping of orphaned containers is
    # **W14.10 territory** (PG-backed audit table + orchestrator-level
    # reaper). That deferral is the row's "intentionally per-worker"
    # type-3 answer per ``docs/sop/implement_phase_step.md`` Step 1.
    web_sandbox_idle_timeout_s: float = 1800.0
    web_sandbox_reap_interval_s: float = 60.0

    # ── W14.9 — Live web-preview cgroup resource limits ──
    # Hard caps applied at ``docker run`` time on every per-workspace
    # web-preview sidecar. Defaults match the W14.9 row spec: 2 GiB
    # RAM / 1 CPU / 5 GiB writable-layer disk. Operators don't need to
    # set these knobs to get the documented behaviour — leaving them
    # empty falls through to the row-spec defaults.
    #
    # ``web_sandbox_memory_limit`` — docker-style size (``2g``,
    # ``512m``, raw bytes). The launcher passes ``--memory <bytes>``
    # plus ``--memory-swap <bytes>`` (set equal) to disable swap-based
    # cap escape. Range floor 64 MiB, ceiling 64 GiB.
    #
    # ``web_sandbox_cpu_limit`` — fractional CPU count (``1``,
    # ``0.5``, ``2``). Mapped to ``--cpus N``. Range floor 0.05,
    # ceiling 64.
    #
    # ``web_sandbox_storage_limit`` — docker-style size for the
    # container's writable layer; passed as ``--storage-opt size=``.
    # **Caveat**: docker only honours this on storage drivers that
    # support per-container quotas (overlay2 with xfs project quotas,
    # devicemapper, btrfs). On overlay2-on-ext4 (most dev boxes) the
    # cap is silently ignored — operators on those hosts can set this
    # to ``off`` / ``0`` / ``none`` to skip the flag entirely so the
    # spec doesn't lie about enforcement. Range floor 256 MiB,
    # ceiling 256 GiB.
    #
    # Module-global state audit (Step 1, type-1): all three fields are
    # immutable Settings literals derived once at process boot from
    # env / .env — every uvicorn worker derives the same
    # :class:`WebPreviewResourceLimits` from the same source so the
    # cgroup contract is identical across workers without any in-
    # process cache. A misconfigured value (e.g. ``2x``) raises
    # :class:`backend.web_sandbox_resource_limits.ResourceLimitsError`
    # at construction; the router catches that and falls back to row-
    # spec defaults rather than 500'ing every launch.
    web_sandbox_memory_limit: str = ""    # default "2g"
    web_sandbox_cpu_limit: str = ""       # default "1"
    web_sandbox_storage_limit: str = ""   # default "5g"; "off" disables

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

    # ─── Declared-only-to-satisfy-extra=forbid fields ────────────────
    # Phase-3-Runtime-v2 SP-3.1 (2026-04-20): these env vars are read
    # elsewhere in the codebase via ``os.environ.get(...)`` directly
    # (not through ``settings.X``) — either because the reading site
    # predates this Settings class, or because the variable is re-read
    # at runtime and a cached Settings value would be wrong. We still
    # need them DECLARED here because pydantic-settings' default
    # ``extra='forbid'`` gate rejects any env var (or .env line) that
    # doesn't map to a Settings field.
    #
    # Matches the existing pattern documented at the ``auth_baseline_mode``
    # declaration earlier in this class — see its header comment for the
    # full rationale.
    #
    # Before SP-3.1: these env vars existed in operator ``.env`` files
    # but weren't declared here, so anything that imported ``backend.db``
    # (which instantiates Settings at module-load time via
    # ``_resolve_db_path()``) crashed in test/dev environments that had
    # the real .env on the path. Tests worked around it with
    # ``monkeypatch.chdir(tmp_path)`` to hide the .env — a workaround
    # that SP-3.1 replaces with this proper declaration.

    # Used by: backend/config.py::validate_startup_config, auth_baseline
    auth_mode: str = "open"
    # Used by: backend/config.py::validate_startup_config (HTTPS gate)
    cookie_secure: str = ""
    # Used by: backend/routers/health.py (deep-probe gating, C3 audit)
    readyz_deep_check: str = ""
    # Used by: backend/auth.py::ensure_default_admin (bootstrap)
    admin_email: str = ""
    admin_password: str = ""
    # Used by: backend/config.py::validate_startup_config (secret gate)
    decision_bearer: str = ""
    # Used by: CF tunnel ingress runbook (Caddy :8080 path routing)
    public_hostname: str = ""
    cloudflare_tunnel_token: str = ""
    # Frontend-side env var that appears in the prod ``.env`` file
    # because operators use ONE .env for both frontend and backend.
    # Declared without OMNISIGHT_ prefix so pydantic-settings finds it
    # in the raw .env line ``next_public_api_url=...``.
    next_public_api_url: str = ""
    # Phase 5-5 (#multi-account-forge) kill-switch. Read via
    # ``os.environ`` in :mod:`backend.legacy_credential_migration`
    # so the migration hook can be disabled without code change.
    # Declared here only to satisfy the SP-3.1 pattern (operator
    # `.env` lines must map to a Settings field). Value of ``skip``
    # bypasses the legacy → ``git_accounts`` migration entirely;
    # any other value (including empty) leaves it active.
    credential_migrate: str = ""
    # Phase 5b-5 (#llm-credentials) kill-switch. Read via
    # ``os.environ`` in :mod:`backend.legacy_llm_credential_migration`
    # so the migration hook can be disabled without code change.
    # Declared here only to satisfy the SP-3.1 pattern (operator
    # `.env` lines must map to a Settings field). Value of ``skip``
    # bypasses the legacy ``.env`` → ``llm_credentials`` migration
    # entirely; any other value (including empty) leaves it active.
    llm_credential_migrate: str = ""

    # ── AS.6.1 OmniSight self-login OAuth (Sign in with X) ──
    # Per-vendor OAuth 2.0 client credentials for the AS.6.1 /
    # FX2.D9.7 SSO buttons. Empty value
    # ⇒ that provider's /authorize endpoint returns 501 "not
    # configured" so operators can ship the binary without wiring
    # any provider, then enable them one-by-one. The vendor catalog
    # (authorize/token/userinfo URLs, default scopes, OIDC flag) is
    # owned by ``backend.security.oauth_vendors`` — these knobs
    # carry only the per-deployment caller identity.
    #
    # ``oauth_redirect_base_url`` is the public base URL OmniSight
    # is reachable at (``https://omnisight.example.com``); the
    # callback redirect_uri is ``{base}/api/v1/auth/oauth/{vendor}/
    # callback``. Empty ⇒ /authorize falls back to the request's
    # own host header, which works for local dev but NOT for prod
    # deployments behind a reverse proxy because the forwarded
    # Host may differ from what the OAuth provider has on file.
    #
    # ``oauth_flow_signing_key`` keys the HMAC-SHA256 signature on
    # the in-flight FlowSession cookie that round-trips state +
    # PKCE verifier + nonce between /authorize and /callback. Empty
    # ⇒ derived from ``decision_bearer`` via SHA-256 so existing
    # deployments don't need a new env var (decision_bearer is
    # already required by L1 startup gate). Setting both means
    # operators can rotate one without invalidating the other.
    #
    # Module-global state audit (per implement_phase_step.md SOP §1,
    # type-1 answer): all 23 fields are immutable Settings literals
    # derived once at process boot from env / .env — every uvicorn
    # worker reads the same value from the same source so cross-
    # worker FlowSession cookie verification is deterministic
    # without any in-process cache.
    oauth_google_client_id: str = ""
    oauth_google_client_secret: str = ""
    oauth_github_client_id: str = ""
    oauth_github_client_secret: str = ""
    oauth_microsoft_client_id: str = ""
    oauth_microsoft_client_secret: str = ""
    oauth_apple_client_id: str = ""
    oauth_apple_client_secret: str = ""
    oauth_discord_client_id: str = ""
    oauth_discord_client_secret: str = ""
    oauth_gitlab_client_id: str = ""
    oauth_gitlab_client_secret: str = ""
    oauth_bitbucket_client_id: str = ""
    oauth_bitbucket_client_secret: str = ""
    oauth_slack_client_id: str = ""
    oauth_slack_client_secret: str = ""
    oauth_notion_client_id: str = ""
    oauth_notion_client_secret: str = ""
    oauth_salesforce_client_id: str = ""
    oauth_salesforce_client_secret: str = ""
    oauth_salesforce_login_base_url: str = ""
    oauth_redirect_base_url: str = ""
    oauth_flow_signing_key: str = ""

    # Test harness sets OMNISIGHT_DOTENV_FILE=".env.test" (see
    # backend/tests/conftest.py) so pytest runs get the safe-default
    # credentials instead of the developer's real ``.env``. Prod /
    # dev / runtime leave the env var unset and fall back to ``.env``.
    # Resolved at class-definition time; pytest's conftest.py sets the
    # env var at module-load, before ANY ``from backend.config import ..``.
    model_config = {
        "env_file": os.environ.get("OMNISIGHT_DOTENV_FILE", ".env"),
        "env_prefix": "OMNISIGHT_",
    }

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
#  Phase 5-10 (#multi-account-forge) legacy credential deprecation
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#
# Phase 5-1 through 5-9 shipped the ``git_accounts`` table + CRUD API
# + UI as the authoritative source for per-tenant, multi-account forge
# credentials. The ``Settings`` scalar / JSON-map fields below are the
# previous generation and are now deprecated:
#
#   * READ stays OK — ``backend.git_credentials._build_registry`` still
#     synthesises a virtual ``git_accounts``-shaped row from whichever
#     of these is set, so operator deployments that never populated
#     ``git_accounts`` keep working unchanged until Phase 5-5's
#     lifespan auto-migration converts them on next backend boot.
#   * WRITE (via ``PUT /runtime/settings``) is considered deprecated.
#     Callers that write a legacy field emit an ``audit.log`` row with
#     ``action=settings.legacy_credential_write`` so the trail records
#     *who* kept the old UI alive; the response also carries a
#     ``deprecations`` block so the frontend can surface a yellow
#     "move to SYSTEM INTEGRATIONS → Git Accounts" banner.
#
# The registry is a plain ``dict[str, str]`` mapping the Settings field
# name to a short replacement hint — the hint is what shows up in the
# audit-log ``after`` dict and the PUT response's ``deprecations`` key,
# so it needs to be accurate (grep-able when debugging later) but
# short (UI surface).
#
# Fields intentionally NOT listed here (and why):
#   * ``gerrit_enabled`` — master switch / feature flag, not a
#     credential. Stays as a Settings scalar even after migration.
#   * ``gerrit_replication_targets`` — list of git *destinations*
#     (post-merge push), not a credential. See Phase 5-7 HANDOFF.
#   * ``jira_intake_label`` / ``jira_done_statuses`` — routing knobs
#     for JIRA inbound automation, not the JIRA credential itself.
#   * ``ollama_base_url`` / ``*_api_key`` — LLM-provider credentials.
#     Phase 5b has a separate ``llm_credentials`` table; those
#     deprecation hooks land with 5b-6, not here.

LEGACY_CREDENTIAL_FIELDS: dict[str, str] = {
    # GitHub
    "github_token":              "git_accounts(platform='github', is_default=TRUE)",
    "github_token_map":          "git_accounts(platform='github', url_patterns=[...])",
    "github_webhook_secret":     "git_accounts(platform='github').encrypted_webhook_secret",
    # GitLab
    "gitlab_token":              "git_accounts(platform='gitlab', is_default=TRUE)",
    "gitlab_url":                "git_accounts(platform='gitlab').instance_url",
    "gitlab_token_map":          "git_accounts(platform='gitlab', url_patterns=[...])",
    "gitlab_webhook_secret":     "git_accounts(platform='gitlab').encrypted_webhook_secret",
    # Gerrit
    "gerrit_url":                "git_accounts(platform='gerrit').instance_url",
    "gerrit_ssh_host":           "git_accounts(platform='gerrit').ssh_host",
    "gerrit_ssh_port":           "git_accounts(platform='gerrit').ssh_port",
    "gerrit_project":            "git_accounts(platform='gerrit').project",
    "gerrit_instances":          "one git_accounts(platform='gerrit') row per instance",
    "gerrit_webhook_secret":     "git_accounts(platform='gerrit').encrypted_webhook_secret",
    # JIRA
    "notification_jira_url":     "git_accounts(platform='jira').instance_url",
    "notification_jira_token":   "git_accounts(platform='jira').encrypted_token",
    "notification_jira_project": "git_accounts(platform='jira').project",
    "jira_webhook_secret":       "git_accounts(platform='jira').encrypted_webhook_secret",
    # Shared SSH fallback
    "git_ssh_key_path":          "git_accounts.encrypted_ssh_key",
    "git_ssh_key_map":           "git_accounts.encrypted_ssh_key + url_patterns",
}


def is_legacy_credential_field(name: str) -> bool:
    """Return True when ``name`` is a Phase-5-deprecated credential
    field on the ``Settings`` singleton.

    Use this at write sites (``PUT /runtime/settings``, wizard /
    rotate helpers) to decide whether to emit
    ``audit.log(action="settings.legacy_credential_write", ...)``.
    Read sites MUST NOT gate on this — reads still resolve through
    the legacy shim in ``backend.git_credentials``.
    """
    return name in LEGACY_CREDENTIAL_FIELDS


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Phase 5b-6 (#llm-credentials) LLM credential deprecation
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#
# Phase 5b-1 through 5b-5 shipped the ``llm_credentials`` table +
# CRUD API + UI + lifespan auto-migration as the authoritative source
# for per-tenant, multi-account LLM provider credentials. The
# ``Settings`` scalar fields below are the previous generation and
# are now deprecated:
#
#   * READ stays OK — ``backend.llm_credential_resolver`` falls back
#     to ``settings.{provider}_api_key`` when the DB has no row for
#     a provider, so deployments that never populated
#     ``llm_credentials`` keep working unchanged. Phase 5b-5's
#     lifespan auto-migration converts them on next backend boot
#     unless ``OMNISIGHT_LLM_CREDENTIAL_MIGRATE=skip`` is set.
#   * WRITE via ``PUT /runtime/settings`` is now REJECTED. The
#     ``_UPDATABLE_FIELDS`` whitelist in
#     :mod:`backend.routers.integration` no longer lists these
#     fields, so writes return ``rejected[field]="deprecated: use
#     POST /api/v1/llm-credentials"`` + emit an ``audit.log`` row
#     with ``action=settings.legacy_llm_credential_write`` + the
#     response carries a ``deprecations`` block the UI can surface.
#
# Fields intentionally NOT listed here (and why):
#   * ``llm_provider`` / ``llm_model`` / ``llm_temperature`` /
#     ``llm_fallback_chain`` — routing / selection knobs, not
#     credentials. Still go through the Settings path in
#     Phase 5b-6; future phases may promote them to DB.
#   * ``token_*`` budget knobs — observability configuration, not
#     a credential.
#   * Git-forge credentials (``github_token`` / ``gerrit_url`` /
#     ...) — those are Phase 5-10 scope and live in
#     ``LEGACY_CREDENTIAL_FIELDS`` above.

LEGACY_LLM_CREDENTIAL_FIELDS: dict[str, str] = {
    "anthropic_api_key":  "llm_credentials(provider='anthropic').encrypted_value",
    "google_api_key":     "llm_credentials(provider='google').encrypted_value",
    "openai_api_key":     "llm_credentials(provider='openai').encrypted_value",
    "xai_api_key":        "llm_credentials(provider='xai').encrypted_value",
    "groq_api_key":       "llm_credentials(provider='groq').encrypted_value",
    "deepseek_api_key":   "llm_credentials(provider='deepseek').encrypted_value",
    "together_api_key":   "llm_credentials(provider='together').encrypted_value",
    "openrouter_api_key": "llm_credentials(provider='openrouter').encrypted_value",
    # Not a secret per se but same lifecycle: 5b-4 UI moved it into
    # ``llm_credentials(provider='ollama').metadata.base_url``.
    "ollama_base_url":    "llm_credentials(provider='ollama').metadata.base_url",
}


def is_legacy_llm_credential_field(name: str) -> bool:
    """Return True when ``name`` is a Phase-5b-deprecated LLM
    credential field on the ``Settings`` singleton.

    Use this at write sites (``PUT /runtime/settings``) to decide
    whether to reject + emit ``audit.log(action=
    "settings.legacy_llm_credential_write", ...)``. Read sites MUST
    NOT gate on this — reads still fall back through
    :mod:`backend.llm_credential_resolver`.
    """
    return name in LEGACY_LLM_CREDENTIAL_FIELDS


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

    `strict` defaults to True when *neither* `settings.debug` nor
    `settings.ci_mode` is set, False when either is on — dev workflow
    stays lenient, CI runners stay lenient (H7 audit: preferred over
    re-purposing DEBUG for CI), prod boots refuse to start with a
    known-dangerous config.
    """
    if strict is None:
        strict = not (settings.debug or settings.ci_mode)

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
        # foot-gun in prod. Audit H1 (2026-04-19): upgrade to hard error
        # under strict mode — mirrors C1's admin-password treatment, so
        # a production deploy that forgets the bearer env can't silently
        # ship open mutator endpoints.
        msg = (
            "OMNISIGHT_DECISION_BEARER is empty — Decision Engine "
            "mutator endpoints (approve/reject/undo/mode) are OPEN. "
            "Set to a strong random secret ≥ "
            f"{_MIN_BEARER_LEN} chars before exposing the URL."
        )
        (hard_errors if strict else warnings).append(msg)

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

    # ── BP.N.3: web-search provider / budget knobs ──
    web_search_provider = (settings.web_search_provider or "").strip().lower()
    if web_search_provider not in {"none", "tavily", "exa", "perplexity"}:
        msg = (
            f"OMNISIGHT_WEB_SEARCH_PROVIDER={settings.web_search_provider!r} "
            "is invalid. Valid: none / tavily / exa / perplexity."
        )
        (hard_errors if strict else warnings).append(msg)
    try:
        web_search_budget = float(settings.web_search_daily_budget_usd)
    except (TypeError, ValueError):
        msg = (
            "OMNISIGHT_WEB_SEARCH_DAILY_BUDGET_USD must be a non-negative "
            f"number, got {settings.web_search_daily_budget_usd!r}."
        )
        (hard_errors if strict else warnings).append(msg)
    else:
        if web_search_budget < 0:
            msg = (
                "OMNISIGHT_WEB_SEARCH_DAILY_BUDGET_USD must be non-negative, "
                f"got {settings.web_search_daily_budget_usd!r}."
            )
            (hard_errors if strict else warnings).append(msg)

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

    if env_name == "production" and (settings.docker_runtime or "").strip().lower() != "runsc":
        hard_errors.append(
            "ENV=production requires OMNISIGHT_DOCKER_RUNTIME=runsc so "
            "Tier-1 sandboxes use gVisor. Use non-production env for "
            "explicit runc compatibility tests."
        )

    # The bootstrap admin password ships as `omnisight-admin`. Hard-
    # fail if that's still in use under prod, so an internet-exposed
    # instance can't have its default admin trivially logged into.
    # Audit C1 (2026-04-19): both "unset" and "literal default" must be
    # hard errors under strict mode — the fallback in backend/auth.py
    # L631 silently lands the well-known credential when the env is
    # unset, and the must_change_password/428 gate is defence-in-depth,
    # not a licence to ship with deterministic defaults on exposed URLs.
    admin_pw = (os.environ.get("OMNISIGHT_ADMIN_PASSWORD") or "").strip()
    if not admin_pw:
        msg = (
            "OMNISIGHT_ADMIN_PASSWORD unset — bootstrap admin will fall "
            "back to the well-known default 'omnisight-admin' "
            "(backend/auth.py ensure_default_admin). Refuse to start in "
            "strict mode — set OMNISIGHT_ADMIN_PASSWORD to a strong "
            "random secret before exposing the URL."
        )
        (hard_errors if strict else warnings).append(msg)
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
