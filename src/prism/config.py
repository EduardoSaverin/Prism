"""Loading and checking the Prism config file.

The config file describes providers and routes. It never holds secrets: a key
entry names an environment variable, and the secret is read from there.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, model_validator


class ConfigError(Exception):
    """The config file is missing, unreadable or wrong."""


class ProviderKind(StrEnum):
    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    GEMINI = "gemini"
    GROQ = "groq"
    MISTRAL = "mistral"
    CEREBRAS = "cerebras"
    NVIDIA_NIM = "nvidia_nim"
    CLOUDFLARE = "cloudflare"
    GITHUB_MODELS = "github_models"
    OLLAMA = "ollama"
    OPENAI_COMPATIBLE = "openai_compatible"


class PolicyName(StrEnum):
    PRIORITY = "priority"
    WEIGHTED = "weighted"
    ROUND_ROBIN = "round_robin"
    LATENCY = "latency"
    COST = "cost"
    QUOTA = "quota"


_KEYLESS_KINDS = {ProviderKind.OLLAMA, ProviderKind.OPENAI_COMPATIBLE}


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class KeyConfig(_Strict):
    env: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    weight: int = Field(default=1, ge=1)
    daily_quota: int | None = Field(default=None, ge=1)


class PriceConfig(_Strict):
    """What one model costs, in US dollars per million tokens."""

    input: Decimal = Field(ge=0, allow_inf_nan=False)
    output: Decimal = Field(ge=0, allow_inf_nan=False)


class ProviderConfig(_Strict):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]*$")
    kind: ProviderKind
    base_url: str | None = None
    keys: tuple[KeyConfig, ...] = ()
    timeout_s: float = Field(default=60.0, gt=0)
    # Prices by model name, as the provider names its models.
    prices: dict[str, PriceConfig] = Field(default_factory=lambda: {})

    @model_validator(mode="after")
    def _check_provider(self) -> Self:
        if any(not model.strip() for model in self.prices):
            raise ValueError("a price needs a model name")
        if self.kind is ProviderKind.OPENAI_COMPATIBLE and not self.base_url:
            raise ValueError("base_url is required for openai_compatible providers")
        if self.kind is ProviderKind.CLOUDFLARE and not self.base_url:
            raise ValueError(
                "base_url is required for cloudflare providers: "
                "https://api.cloudflare.com/client/v4/accounts/<ACCOUNT_ID>/ai/v1"
            )
        if self.base_url and not self.base_url.startswith(("http://", "https://")):
            raise ValueError("base_url must start with http:// or https://")
        if self.kind not in _KEYLESS_KINDS and not self.keys:
            raise ValueError(f"{self.kind.value} providers need at least one key")
        return self


class TargetConfig(_Strict):
    provider: str
    model: str
    weight: int = Field(default=1, ge=1)
    # Env names of the provider keys this target may use. Empty means all of them.
    keys: tuple[str, ...] = ()


class RetryConfig(_Strict):
    max_retries: int = Field(default=2, ge=0, le=5)
    backoff_ms: int = Field(default=200, ge=0)


class CacheConfig(_Strict):
    exact: bool = False
    semantic: bool = False
    ttl_s: int = Field(default=300, ge=1)


class RouteConfig(_Strict):
    name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    policy: PolicyName = PolicyName.PRIORITY
    targets: tuple[TargetConfig, ...] = Field(min_length=1)
    retry: RetryConfig = Field(default_factory=RetryConfig)
    cache: CacheConfig = Field(default_factory=CacheConfig)
    fallback_on_content_blocked: bool = False


class CircuitBreakerConfig(_Strict):
    failure_threshold: int = Field(default=5, ge=1)
    cooldown_s: float = Field(default=30.0, gt=0)


class PrismConfig(_Strict):
    providers: tuple[ProviderConfig, ...] = Field(min_length=1)
    routes: tuple[RouteConfig, ...] = Field(min_length=1)
    circuit_breaker: CircuitBreakerConfig = Field(default_factory=CircuitBreakerConfig)

    @model_validator(mode="after")
    def _check_links(self) -> Self:
        provider_ids = [p.id for p in self.providers]
        _reject_duplicates("provider id", provider_ids)
        _reject_duplicates("route name", [r.name for r in self.routes])
        known = set(provider_ids)
        for route in self.routes:
            for target in route.targets:
                if target.provider not in known:
                    raise ValueError(
                        f"route '{route.name}' uses unknown provider '{target.provider}'"
                    )
                _check_target_keys(route.name, target, self.provider(target.provider))
        return self

    def provider(self, provider_id: str) -> ProviderConfig:
        for item in self.providers:
            if item.id == provider_id:
                return item
        raise KeyError(provider_id)


def _check_target_keys(route: str, target: TargetConfig, provider: ProviderConfig) -> None:
    """A target may only name keys that its provider defines, each once."""
    if not target.keys:
        return
    where = f"route '{route}', target {provider.id}/{target.model}"
    _reject_duplicates(f"key in {where}", list(target.keys))
    defined = [key.env for key in provider.keys]
    unknown = [env for env in target.keys if env not in defined]
    if unknown:
        choices = ", ".join(defined) if defined else "none"
        raise ValueError(
            f"{where} uses keys that provider '{provider.id}' does not define: "
            f"{', '.join(unknown)}. Its keys: {choices}"
        )


def _reject_duplicates(label: str, values: list[str]) -> None:
    seen: set[str] = set()
    for value in values:
        if value in seen:
            raise ValueError(f"duplicate {label}: '{value}'")
        seen.add(value)


def _format_errors(exc: ValidationError) -> str:
    lines: list[str] = []
    for error in exc.errors():
        where = ".".join(str(part) for part in error["loc"]) or "(top level)"
        lines.append(f"{where}: {error['msg']}")
    return "\n".join(lines)


def parse_config(data: object) -> PrismConfig:
    """Check a loaded config and return it, or raise ConfigError."""
    if not isinstance(data, dict):
        raise ConfigError("The config file must contain a mapping at the top level.")
    try:
        return PrismConfig.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(_format_errors(exc)) from exc


def load_config(path: str | Path) -> PrismConfig:
    """Read a YAML config file from disk and check it."""
    file = Path(path)
    try:
        text = file.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"Cannot read config file {file}: {exc.strerror}") from exc
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"{file} is not valid YAML: {exc}") from exc
    return parse_config(data)


def missing_env_vars(config: PrismConfig, environ: Mapping[str, str] | None = None) -> list[str]:
    """List the key variables the config names but the environment does not set."""
    env = os.environ if environ is None else environ
    names = {key.env for provider in config.providers for key in provider.keys}
    return sorted(name for name in names if not env.get(name))


def resolve_secret(key: KeyConfig, environ: Mapping[str, str] | None = None) -> SecretStr:
    """Read one key's secret from the environment."""
    env = os.environ if environ is None else environ
    value = env.get(key.env)
    if not value:
        raise ConfigError(f"Environment variable {key.env} is not set.")
    return SecretStr(value)
