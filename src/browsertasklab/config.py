from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="BTL_", env_file=".env", extra="ignore")
    base_url: str = "http://127.0.0.1:8000"
    runtime_dir: Path = Path(".runtime")
    provider: str = "openai"
    model: str = "gpt-5.4-mini-2026-03-17"
    model_base_url: str | None = None
    headless: bool = True
    action_timeout_ms: int = Field(default=2000, ge=100, le=10000)
    max_retries: int = Field(default=2, ge=0, le=3)
    price_input: float | None = None
    price_cached: float | None = None
    price_output: float | None = None
    price_model: str | None = None
    price_verified_on: str | None = None
    price_source: str | None = None

    def prepare(self):
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        (self.runtime_dir / "screenshots").mkdir(exist_ok=True)
        (self.runtime_dir / "evaluations").mkdir(exist_ok=True)

    def pricing(self) -> dict | None:
        values = (self.price_input, self.price_cached, self.price_output)
        if (self.price_model != self.model or any(v is None or v < 0 for v in values)
                or not self.price_verified_on or not self.price_source):
            return None
        return {"input": self.price_input, "cached": self.price_cached,
                "output": self.price_output, "model": self.model,
                "verified_on": self.price_verified_on, "source": self.price_source}
