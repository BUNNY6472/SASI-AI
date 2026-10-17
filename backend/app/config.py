"""Runtime settings. Every secret comes from the environment (Secret Manager on Cloud Run)."""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Provider keys. Empty means the provider is not connected.
    anthropic_api_key: str = ""
    openai_api_key: str = ""
    gemini_api_key: str = ""

    # Optional model pins. Empty means "pick from the provider's live model list".
    claude_model: str = "claude-opus-5-5"
    openai_model: str = ""
    gemini_model: str = ""

    # Access control: everyone must enter this code before using the app.
    # Empty disables the gate (local development only).
    app_access_code: str = ""
    session_secret: str = "dev-only-change-me"

    # Where keys entered in the app are stored: "env" (local) or "secretmanager" (Cloud Run).
    key_store: str = "env"

    # Simulated agents for automated tests only. Off in the real app.
    allow_simulated: bool = False
    simulated_delay: float = 0.6

    # Run limits (protect the budget).
    max_model_calls_per_run: int = 40
    max_output_tokens_per_call: int = 4000
    max_tokens_per_run: int = 200_000
    max_tasks_per_plan: int = 5
    max_revisions_per_task: int = 1
    max_concurrent_runs: int = 3

    # Storage.
    storage_backend: str = "memory"  # "memory" or "bigquery"
    gcp_project: str = ""
    bigquery_dataset: str = "sasi_data"

    # Static frontend directory (built Next.js export).
    static_dir: str = "static"


@lru_cache
def get_settings() -> Settings:
    return Settings()
