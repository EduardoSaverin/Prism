"""Build adapters and the gateway from the config."""

from __future__ import annotations

from collections.abc import Callable, Mapping

from prism.config import ConfigError, PrismConfig, ProviderConfig, ProviderKind
from prism.core.gateway import Gateway
from prism.core.keys import KeyPool, build_key_pool
from prism.core.policies.registry import PolicyNotAvailableError
from prism.providers.anthropic import AnthropicAdapter
from prism.providers.base import ProviderAdapter
from prism.providers.gemini import GeminiAdapter
from prism.providers.openai_compat import OpenAICompatAdapter, OpenAICompatPreset

_UNSUPPORTED_BY_MOST = frozenset({"logit_bias", "logprobs", "top_logprobs"})

PRESETS: dict[ProviderKind, OpenAICompatPreset] = {
    ProviderKind.OPENAI: OpenAICompatPreset(
        "https://api.openai.com/v1", token_field="max_completion_tokens"
    ),
    ProviderKind.GROQ: OpenAICompatPreset(
        "https://api.groq.com/openai/v1",
        drop_params=_UNSUPPORTED_BY_MOST,
        strip_message_names=True,
    ),
    ProviderKind.MISTRAL: OpenAICompatPreset(
        "https://api.mistral.ai/v1",
        stream_usage_option=False,
        drop_params=_UNSUPPORTED_BY_MOST | {"user", "stream_options"},
        rename_params={"seed": "random_seed"},
    ),
    ProviderKind.CEREBRAS: OpenAICompatPreset(
        "https://api.cerebras.ai/v1",
        stream_usage_option=False,
        drop_params=frozenset({"tool_stream"}),
    ),
    ProviderKind.NVIDIA_NIM: OpenAICompatPreset(
        "https://integrate.api.nvidia.com/v1", stream_usage_option=False
    ),
    ProviderKind.CLOUDFLARE: OpenAICompatPreset(None, stream_usage_option=False),
    ProviderKind.OLLAMA: OpenAICompatPreset(
        "http://localhost:11434/v1",
        requires_key=False,
        drop_params=_UNSUPPORTED_BY_MOST | {"tool_choice", "user", "n"},
    ),
    ProviderKind.OPENAI_COMPATIBLE: OpenAICompatPreset(None, requires_key=False),
}

KEYLESS_KINDS = {ProviderKind.OLLAMA, ProviderKind.OPENAI_COMPATIBLE}

RETIRED: dict[ProviderKind, str] = {
    ProviderKind.GITHUB_MODELS: "GitHub Models was retired on 2026-07-30 and has no API.",
}

AdapterFactory = Callable[[ProviderConfig], ProviderAdapter]


def build_adapter(provider: ProviderConfig) -> ProviderAdapter:
    if provider.kind in RETIRED:
        raise ConfigError(f"provider '{provider.id}': {RETIRED[provider.kind]}")
    if provider.kind is ProviderKind.ANTHROPIC:
        return AnthropicAdapter(
            provider.id, base_url=provider.base_url, timeout_s=provider.timeout_s
        )
    if provider.kind is ProviderKind.GEMINI:
        return GeminiAdapter(provider.id, base_url=provider.base_url, timeout_s=provider.timeout_s)
    preset = PRESETS[provider.kind]
    return OpenAICompatAdapter(
        provider.id, preset, base_url=provider.base_url, timeout_s=provider.timeout_s
    )


def build_gateway(
    config: PrismConfig,
    environ: Mapping[str, str],
    *,
    adapter_factory: AdapterFactory = build_adapter,
) -> Gateway:
    adapters: dict[str, ProviderAdapter] = {}
    pools: dict[str, KeyPool] = {}
    for provider in config.providers:
        adapters[provider.id] = adapter_factory(provider)
        pools[provider.id] = build_key_pool(
            provider, environ, keyless=provider.kind in KEYLESS_KINDS
        )
    try:
        return Gateway(config, adapters, pools)
    except PolicyNotAvailableError as exc:
        raise ConfigError(str(exc)) from exc
