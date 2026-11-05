from typing import Self

from pydantic import (
    Field,
    Json,
    PositiveFloat,
    PositiveInt,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

from metabase_sync.errors import ConfigError, describe


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="METABASE_", env_file=".env", extra="ignore", frozen=True
    )

    url: str = ""
    api_key: SecretStr | None = None
    headers: Json[dict[str, str]] = Field(default={}, validate_default=False)
    timeout: PositiveFloat = 120.0
    concurrency: PositiveInt = 12

    @field_validator("api_key", mode="before")
    @classmethod
    def _blank_is_unset(cls, value: object) -> object:
        return value or None

    @model_validator(mode="after")
    def _complete(self) -> Self:
        if not self.url:
            raise ValueError("METABASE_URL is not set")
        if self.api_key is None and not self.headers:
            raise ValueError("set METABASE_API_KEY, or credentials in METABASE_HEADERS")
        return self

    @property
    def base(self) -> str:
        return self.url.rstrip("/")


def load_settings() -> Settings:
    try:
        return Settings()
    except ValidationError as e:
        raise ConfigError(
            "; ".join(describe(e, env_prefix="METABASE_")),
            hint="export METABASE_URL=https://metabase.example.com, and create an "
            "API key under Admin settings > Authentication > API keys",
        ) from e
