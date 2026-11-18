from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from prism.config import (
    ConfigError,
    KeyConfig,
    load_config,
    missing_env_vars,
    parse_config,
    resolve_secret,
)


def valid() -> dict[str, Any]:
    return {
        "providers": [{"id": "groq", "kind": "groq", "keys": [{"env": "GROQ_KEY_1"}]}],
        "routes": [{"name": "fast", "targets": [{"provider": "groq", "model": "m"}]}],
    }


def test_the_example_config_is_valid(example_config_path: Path) -> None:
    config = load_config(example_config_path)
    assert [p.id for p in config.providers] == ["groq", "gemini", "local-ollama"]
    assert config.routes[0].name == "fast-chat"
    assert config.routes[0].retry.max_retries == 2
    assert config.routes[0].cache.exact is True
    assert config.provider("groq").keys[0].weight == 2


def test_defaults_are_filled_in() -> None:
    route = parse_config(valid()).routes[0]
    assert route.policy.value == "priority"
    assert route.retry.max_retries == 2
    assert route.cache.exact is False
    assert route.fallback_on_content_blocked is False


def test_looking_up_an_unknown_provider_fails() -> None:
    with pytest.raises(KeyError):
        parse_config(valid()).provider("nope")


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda d: d["providers"].append(dict(d["providers"][0])), "duplicate provider id"),
        (lambda d: d["routes"].append(dict(d["routes"][0])), "duplicate route name"),
        (lambda d: d["routes"][0]["targets"][0].update(provider="ghost"), "unknown provider"),
        (lambda d: d["routes"][0].update(targets=[]), "targets"),
        (lambda d: d["providers"][0].update(keys=[]), "at least one key"),
        (lambda d: d["providers"][0].update(base_url="ftp://x"), "http"),
        (lambda d: d["providers"][0].update(kind="openai_compatible"), "base_url is required"),
        (lambda d: d["providers"][0].update(kind="not-a-provider"), "kind"),
        (lambda d: d["providers"][0].update(typo_field=1), "typo_field"),
        (lambda d: d["routes"][0].update(retry={"max_retries": 99}), "max_retries"),
        (lambda d: d["providers"][0]["keys"][0].update(env="bad name"), "env"),
        (lambda d: d["providers"][0].update(id="Bad ID"), "id"),
    ],
)
def test_bad_configs_are_rejected_with_a_clear_message(change: Any, message: str) -> None:
    data = valid()
    change(data)
    with pytest.raises(ConfigError) as caught:
        parse_config(data)
    assert message in str(caught.value)


def test_local_providers_may_have_no_keys() -> None:
    data = valid()
    data["providers"] = [{"id": "local", "kind": "ollama", "base_url": "http://localhost:11434"}]
    data["routes"][0]["targets"][0]["provider"] = "local"
    assert parse_config(data).providers[0].keys == ()


def test_the_top_level_must_be_a_mapping() -> None:
    with pytest.raises(ConfigError, match="mapping"):
        parse_config(["not", "a", "mapping"])


def test_loading_a_missing_file_gives_a_clear_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="Cannot read"):
        load_config(tmp_path / "nope.yaml")


def test_loading_broken_yaml_gives_a_clear_error(tmp_path: Path) -> None:
    file = tmp_path / "bad.yaml"
    file.write_text("providers: [unclosed", encoding="utf-8")
    with pytest.raises(ConfigError, match="not valid YAML"):
        load_config(file)


def test_missing_env_vars_are_listed_once_and_sorted() -> None:
    config = parse_config(valid())
    assert missing_env_vars(config, {}) == ["GROQ_KEY_1"]
    assert missing_env_vars(config, {"GROQ_KEY_1": "x"}) == []
    assert missing_env_vars(config, {"GROQ_KEY_1": ""}) == ["GROQ_KEY_1"]


def test_resolve_secret_reads_the_environment() -> None:
    key = KeyConfig(env="GROQ_KEY_1")
    secret = resolve_secret(key, {"GROQ_KEY_1": "abc"})
    assert secret.get_secret_value() == "abc"
    assert "abc" not in repr(secret)
    with pytest.raises(ConfigError, match="GROQ_KEY_1"):
        resolve_secret(key, {})


def test_fields_cannot_be_reassigned() -> None:
    config = parse_config(valid())
    with pytest.raises(ValidationError):
        config.routes[0].retry.max_retries = 5  # type: ignore[misc]
    with pytest.raises(ValidationError):
        config.providers[0].keys[0].weight = 9  # type: ignore[misc]


def test_collections_cannot_be_changed_in_place() -> None:
    config = parse_config(valid())
    assert isinstance(config.providers, tuple)
    assert isinstance(config.routes, tuple)
    assert isinstance(config.providers[0].keys, tuple)
    assert isinstance(config.routes[0].targets, tuple)
    with pytest.raises(AttributeError):
        config.routes.clear()  # type: ignore[attr-defined]
    with pytest.raises(AttributeError):
        config.providers[0].keys.append(KeyConfig(env="EXTRA"))  # type: ignore[attr-defined]
