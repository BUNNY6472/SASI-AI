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

    # Google Cloud Vertex AI. When on, the app's own service account calls the models (billed to the Google Cloud
    # project), so no API keys are needed. A saved API key stays as the other route for that model.
    vertex_ai: bool = False
    vertex_location: str = "global"  # Claude and Gemini
    vertex_openai_location: str = "us-central1"  # OpenAI's models on Vertex are served from this region
    vertex_claude_model: str = ""  # empty: same as claude_model
    vertex_gemini_model: str = "gemini-4-argon"
    vertex_openai_model: str = "openai/gpt-oss-120b-maas"
    # Models whose saved API key is tried before Vertex. ChatGPT by default: Vertex only offers OpenAI's open-weight
    # models, while an OpenAI key reaches the newest GPT model.
    key_first: str = "chatgpt"

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
    max_model_calls_per_run: int = 60
    max_output_tokens_per_call: int = 4000
    max_tokens_per_run: int = 500_000
    # Project Eris runs carry the full guidebook and whole training scripts, so they get more room.
    eris_max_tokens_per_run: int = 1_000_000
    eris_max_output_tokens_per_call: int = 12_000
    max_tasks_per_plan: int = 5
    max_revisions_per_task: int = 1
    max_final_fix_rounds: int = 1  # the other models test the final result; the integrator fixes what they find
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
