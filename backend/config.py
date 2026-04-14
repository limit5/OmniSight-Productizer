from pydantic import Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    app_name: str = "OmniSight Engine"
    debug: bool = False  # Set OMNISIGHT_DEBUG=true for development
    api_prefix: str = "/api/v1"

    # Frontend origin for CORS (comma-separated for multiple origins)
    frontend_origin: str = "http://localhost:3000"
    extra_cors_origins: str = ""  # Additional CORS origins, comma-separated

    # Database
    database_path: str = ""  # SQLite path (empty = default data/omnisight.db)

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
