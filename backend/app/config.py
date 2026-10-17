"""Runtime settings. Every secret comes from the environment (Secret Manager on Cloud Run)."""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Provider keys. Empty means the provider is not connected.
    anthropic_api_key: str = ""
    gemini_api_key: str = ""

    # Optional model pins. Empty means "pick from the provider's live model list".
    claude_model: str = "claude-opus-5-5"
    gemini_model: str = ""

    # Google Cloud Vertex AI. When on, the app's own service account calls the models (billed to the Google Cloud
    # project), so no API keys are needed. A saved Claude or Gemini API key stays as the other route for that model.
    vertex_ai: bool = False
    vertex_location: str = "global"  # Claude and Gemini
    vertex_claude_model: str = ""  # empty: same as claude_model
    vertex_gemini_model: str = "gemini-4-argon"
    # Open models on Vertex AI that may join a team (ids from gateway/catalog.py), best first.
    open_models: str = "glm,deepseek,qwen,kimi"
    # Models per run: the working frontier models (Claude, Gemini) first, then the open models that suit the task.
    team_size: int = 4

    # Access control: everyone must enter this code before using the app.
    # Empty disables the gate (local development only).
    app_access_code: str = ""
    session_secret: str = "dev-only-change-me"

    # Where keys entered in the app are stored: "env" (local) or "secretmanager" (Cloud Run).
    key_store: str = "env"

    # Simulated agents for automated tests only. Off in the real app.
    allow_simulated: bool = False
    simulated_delay: float = 0.6

    # Time is the limit for a run, not tokens or calls. In the last minutes SASI stops new work and delivers
    # the complete result from what is done.
    run_time_limit_minutes: float = 60
    wrap_up_minutes: float = 5
    # Optional caps; 0 means no cap.
    max_model_calls_per_run: int = 0
    max_tokens_per_run: int = 0
    eris_max_tokens_per_run: int = 0
    # Longest answer one call may write (thinking gets extra room on top). Eris runs write whole training scripts.
    max_output_tokens_per_call: int = 16_000
    eris_max_output_tokens_per_call: int = 32_000
    max_tasks_per_plan: int = 6
    max_revisions_per_task: int = 2
    max_final_fix_rounds: int = 2  # the other models test the final result; the integrator fixes what they find
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
