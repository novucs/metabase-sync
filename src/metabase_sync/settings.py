from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime config.

    Both `METABASE_URL` and `METABASE_API_KEY` are required (no defaults). They
    can come from the process env or from a `.env` file in the current working
    directory. `state_dir` defaults to `state/` relative to the CWD, so you can
    run `metabase-sync export` from inside `infrastructure/metabase/` (or
    anywhere you keep your state tree) without flag-wrangling.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    metabase_url: str
    metabase_api_key: str
    state_dir: Path = Field(default=Path("state"))
    http_timeout_s: float = Field(default=120.0)
    http_max_retries: int = Field(default=3)
    http_retry_backoff_s: float = Field(default=1.0)


def load_settings(state_dir: Path | None = None) -> Settings:
    s = Settings()  # type: ignore[call-arg]
    if state_dir is not None:
        s = s.model_copy(update={"state_dir": state_dir})
    return s
