"""Typed configuration from the environment (prefix RELAY_)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="RELAY_", env_file=".env", extra="ignore")

    env: str = "dev"
    service_name: str = "relay"
    log_level: str = "INFO"
    config_dir: Path = Path("config")

    # datastores
    database_url: str = "postgresql+asyncpg://relay_app:relay_app@localhost:5433/relay"
    database_admin_url: str = "postgresql+asyncpg://relay:relay@localhost:5433/relay"
    redis_url: str = "redis://localhost:6380/0"

    # LLMs: every call goes through the Atlas gateway (routes: fast | answer | deep | judge)
    gateway_url: str = "http://localhost:8090"
    gateway_key: SecretStr = SecretStr("dev-internal-key")
    embed_url: str | None = "http://localhost:8081"  # TEI /embed; hashing fallback if unreachable
    embed_dim: int = 384

    # Atlas RAG (support agent)
    atlas_url: str = "http://localhost:8000"
    atlas_key: SecretStr = SecretStr("")
    atlas_collections: list[str] = []

    # sandbox
    sandbox_image: str = "relay-sandbox:latest"
    sandbox_runtime: str | None = None  # "runsc" for gVisor
    sandbox_network: str = "relay_sandbox"  # internal, no route; egress only via proxy
    egress_proxy: str = "http://egress-proxy:3128"
    sandbox_mem: str = "2g"
    sandbox_cpus: float = 2.0
    sandbox_pids: int = 512
    sandbox_cmd_timeout_s: int = 300

    # integrations (all optional; agents degrade to dry-run without them)
    github_app_id: str | None = None
    github_private_key: SecretStr | None = None
    github_webhook_secret: SecretStr = SecretStr("dev-github-secret")
    slack_bot_token: SecretStr | None = None
    slack_signing_secret: SecretStr = SecretStr("dev-slack-secret")
    slack_approvals_channel: str = "#relay-approvals"
    jira_url: str | None = None
    jira_email: str | None = None
    jira_token: SecretStr | None = None
    jira_project: str = "OPS"
    notion_token: SecretStr | None = None
    notion_parent_page: str | None = None
    imap_host: str | None = None
    imap_user: str | None = None
    imap_password: SecretStr | None = None

    # security
    api_keys: list[str] = ["dev-relay-key:00000000-0000-0000-0000-000000000001"]  # key:tenant
    capture_content: bool = False
    otlp_endpoint: str | None = None

    def config_path(self, *parts: str) -> Path:
        return self.config_dir.joinpath(*parts)


@lru_cache
def get_settings() -> Settings:
    return Settings()
