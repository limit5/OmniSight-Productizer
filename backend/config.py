from pydantic import Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    app_name: str = "OmniSight Engine"
    debug: bool = True
    api_prefix: str = "/api/v1"

    # Frontend origin for CORS
    frontend_origin: str = "http://localhost:3000"

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

    # Ollama (local, no key needed)
    ollama_base_url: str = "http://localhost:11434"

    # LLM parameters
    llm_temperature: float = 0.3

    # ── Git Authentication ──
    git_ssh_key_path: str = "~/.ssh/id_ed25519"  # SSH key for private repos
    github_token: str = ""  # GitHub Personal Access Token
    gitlab_token: str = ""  # GitLab Personal Access Token
    gitlab_url: str = ""  # Self-hosted GitLab URL (empty = gitlab.com)

    # ── Token Budget & Resilience ──
    token_budget_daily: float = 0.0  # USD per day (0 = unlimited)
    token_warn_threshold: float = 0.8  # 80% → emit warning
    token_downgrade_threshold: float = 0.9  # 90% → auto switch to cheaper model
    token_freeze_threshold: float = 1.0  # 100% → stop all LLM calls
    token_fallback_provider: str = "ollama"  # Provider to downgrade to at 90%
    token_fallback_model: str = "llama3.1"  # Model to downgrade to at 90%
    llm_fallback_chain: str = "anthropic,openai,google,groq,ollama"  # Failover priority

    # ── Notification Routing ──
    notification_slack_webhook: str = ""  # Slack Incoming Webhook URL
    notification_slack_mention: str = ""  # Slack user ID to @ for L3 events
    notification_jira_url: str = ""  # Jira base URL (e.g. https://jira.company.com)
    notification_jira_token: str = ""  # Jira API token
    notification_jira_project: str = ""  # Jira project key (e.g. OMNI)
    notification_pagerduty_key: str = ""  # PagerDuty Events API v2 routing key

    # ── Gerrit Code Review ──
    gerrit_enabled: bool = False  # Master switch for Gerrit integration
    gerrit_url: str = ""  # Web UI URL, e.g. "https://gerrit.sora.services"
    gerrit_ssh_host: str = ""  # SSH host for push + CLI, e.g. "gerrit.sora.services"
    gerrit_ssh_port: int = 29418  # Gerrit SSH port (default 29418)
    gerrit_project: str = ""  # Project path, e.g. "project/omnisight-core"
    gerrit_replication_targets: str = ""  # Comma-separated remote names for post-merge push

    # Docker isolation
    docker_enabled: bool = True  # enable container execution for agents
    docker_image: str = "omnisight-agent:latest"
    docker_network: str = "none"  # none = no network (secure), bridge = allow network

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
