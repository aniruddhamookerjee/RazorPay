"""Runtime configuration, read from the environment / `.env`.

Secrets live in `.env` (git-ignored); `.env.example` documents the shape.
Import `settings` rather than reading `os.environ` anywhere else, so every
knob that affects a run is in one place and can be hashed into a run manifest.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Razorpay (test mode only) ---
    razorpay_key_id: str = ""
    razorpay_key_secret: str = ""
    razorpay_webhook_secret: str = ""

    # --- Local LLM (Ollama, open-source weights) ---
    ollama_host: str = "http://localhost:11434"
    llm_model: str = "qwen2.5:7b-instruct"
    llm_model_hinglish: str = "qwen2.5:7b-instruct"
    llm_timeout_seconds: int = 60

    # --- Runtime ---
    env: str = "dev"
    database_url: str = "sqlite:///./recovery.db"
    fixtures_dir: Path = Path("fixtures")

    # --- Reproducibility ---
    seed: int = 42

    # --- Safety caps (see WORKPLAN.md Day 4: stopping rules + compliance gate) ---
    # Debit attempts only. A notification is not a retry, and counting one
    # against this cap left the agent a single real retry per case.
    max_attempts: int = 3
    # Customer contacts (notify / re-auth request) have their own limit, so
    # messaging is bounded without eating the retry budget.
    max_contacts: int = 2
    recovery_window_days: int = 7
    afa_threshold_inr: int = 15_000
    live_subset_size: int = 100

    @property
    def razorpay_configured(self) -> bool:
        return bool(self.razorpay_key_id and self.razorpay_key_secret)

    @property
    def llm_configured(self) -> bool:
        """The LLM is optional everywhere: each caller falls back to rules or
        templates if the local model is unreachable (see WORKPLAN.md section 3)."""
        return bool(self.ollama_host and self.llm_model)


@lru_cache
def get_settings() -> Settings:
    """Cached so the `.env` is parsed once per process."""
    return Settings()


settings = get_settings()
