from pathlib import Path
from typing import Any, Self

from pydantic import (
    Field,
    HttpUrl,
    Json,
    PositiveFloat,
    PositiveInt,
    SecretStr,
    ValidationError,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

from metabase_sync.errors import ConfigError, describe

HINTS = {
    ("url",): "Set METABASE_URL to the address you open Metabase at, such as "
    "https://metabase.example.com",
    (): "Create an API key under Admin settings > Authentication > API keys, and "
    "set METABASE_API_KEY",
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="METABASE_", extra="ignore", env_ignore_empty=True, frozen=True
    )

    url: HttpUrl
    api_key: SecretStr | None = None
    headers: Json[dict[str, str]] = Field(default={}, validate_default=False)
    timeout: PositiveFloat = 120.0
    concurrency: PositiveInt = 12

    @model_validator(mode="after")
    def _complete(self) -> Self:
        if self.api_key is None and not self.headers:
            raise ValueError("set METABASE_API_KEY, or credentials in METABASE_HEADERS")
        return self

    @property
    def base(self) -> str:
        return str(self.url).rstrip("/")


def load_settings(root: Path) -> Settings:
    sources: dict[str, Any] = {"_env_file": root / ".env"}
    try:
        return Settings(**sources)
    except ValidationError as e:
        raise ConfigError(
            "; ".join(describe(e, env_prefix="METABASE_")),
            hint=next(
                (HINTS[loc] for error in e.errors() if (loc := error["loc"]) in HINTS),
                None,
            ),
        ) from e
