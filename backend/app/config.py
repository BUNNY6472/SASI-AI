"""Runtime settings. Every secret comes from the environment (Secret Manager on Cloud Run)."""
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Provider keys. Empty means the provider is not connected.
    anthropic_api_key: str = ""
    sasi_owner_anthropic_api_key: str = ""  # isolated owner opt-in; never falls back to a legacy Claude key
    gemini_api_key: str = ""

    # Optional model pins. Empty means "pick from the provider's live model list".
    claude_model: str = "claude-opus-5-5"
    gemini_model: str = ""

    # Gemini and open models use the app's Google Cloud service account when Vertex AI is on.
    # Claude uses its own API key unless its separate Vertex route is explicitly enabled.
    # A saved Gemini API key remains a fallback behind its Vertex route.
    vertex_ai: bool = False
    trial_credits_only: bool = False  # only Vertex Gemini, except an explicitly allowed owner Claude API key
    allow_claude_api_key: bool = False  # direct Anthropic only; disables Vertex Claude and uses a separate key slot
    trial_credit_expires_at: str = ""  # optional ISO UTC cutoff; empty does not verify a credit balance
    vertex_claude_enabled: bool = False
    vertex_location: str = "global"  # Gemini and optional Vertex Claude
    vertex_claude_model: str = ""  # empty: same as claude_model
    vertex_gemini_model: str = "gemini-4-argon"
    # Open models on Vertex AI that may join a team (ids from gateway/catalog.py), best first.
    open_models: str = "glm,deepseek,qwen,kimi,minimax,gptoss"
    # Models per run: the working frontier models (Claude, Gemini) first, then the open models that suit the task.
    team_size: int = 4

    # Access control: everyone must enter this code before using the app.
    # Empty disables the gate (local development only).
    require_access_token: bool = False  # production must never start with an open gate
    app_access_code: str = ""
    session_secret: str = "dev-only-change-me"

    # Where keys entered in the app are stored: "env" (local) or "secretmanager" (Cloud Run).
    key_store: str = "env"

    # Simulated agents for automated tests only. Off in the real app.
    allow_simulated: bool = False
    simulated_delay: float = 0.6

    # The run duration is a planning target. Admitted calls keep their own allowance and finishing grace.
    run_time_limit_minutes: float = 120
    max_total_run_minutes: float = Field(default=120, ge=50, le=120)
    wrap_up_minutes: float = 5
    model_phase_min_minutes: float = Field(default=20, ge=20, le=60)
    model_phase_grace_minutes: float = Field(default=5, ge=1, le=15)
    # Optional caps; 0 means no cap.
    max_model_calls_per_run: int = 300
    max_tokens_per_run: int = 2_400_000
    eris_max_tokens_per_run: int = 3_600_000
    # Longest answer one call may write (thinking gets extra room on top). Eris runs write whole training scripts.
    max_output_tokens_per_call: int = 16_000
    eris_max_output_tokens_per_call: int = 32_000
    max_tasks_per_plan: int = 6
    max_revisions_per_task: int = 2
    max_final_fix_rounds: int = 2  # the other models test the final result; the integrator fixes what they find
    max_concurrent_runs: int = 3
    max_gap_tasks_per_run: int = 1
    max_replans_per_run: int = 1
    # Research uses the existing Gemini connection; retrieved pages never become instructions.
    enable_web_research: bool = True
    research_timeout_seconds: float = 45
    research_max_sources: int = 6

    # Storage.
    storage_backend: str = "memory"  # "memory" (tests), "sqlite" (local), or "bigquery" (cloud)
    gcp_project: str = ""
    bigquery_dataset: str = "sasi_data"
    sqlite_path: str = "data/sasi.db"

    # Static frontend directory (built Next.js export).
    static_dir: str = "static"


@lru_cache
def get_settings() -> Settings:
    return Settings()
