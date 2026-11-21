"""Provider key pools.

The router, not the adapter, chooses which key to use for each call
(see docs/adr/0001-adapters-receive-the-chosen-key.md).
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field

from pydantic import SecretStr

from prism.config import ProviderConfig
from prism.core.resilience import Clock
from prism.core.types import ResolvedKey

log = logging.getLogger(__name__)

DEFAULT_RATE_LIMIT_COOLDOWN_S = 1.0


def key_id(provider_id: str, env: str) -> str:
    """The id of a provider key: the provider and the variable that holds it."""
    return f"{provider_id}/{env}"


@dataclass
class KeyState:
    id: str
    secret: SecretStr
    weight: int = 1
    disabled: bool = False
    cooling_until: float = 0.0


@dataclass
class KeyPool:
    """The keys of one provider and their health."""

    provider_id: str
    keys: list[KeyState]
    keyless: bool = False
    clock: Clock = time.monotonic
    rng: random.Random = field(default_factory=random.Random)

    def pick(self, allowed: Collection[str] = ()) -> ResolvedKey | None:
        """Choose a key by weight among the healthy ones.

        `allowed` limits the choice to these key ids; empty means every key.
        Returns None for keyless providers. Raises NoUsableKeyError when the
        provider needs a key and none of the allowed ones is usable right now.
        """
        if self.keyless:
            return None
        now = self.clock()
        usable = [k for k in self._candidates(allowed) if k.cooling_until <= now]
        if not usable:
            raise NoUsableKeyError(self.provider_id, self.retry_in(allowed))
        chosen = self.rng.choices(usable, weights=[k.weight for k in usable], k=1)[0]
        return ResolvedKey(id=chosen.id, secret=chosen.secret)

    def disable(self, key_id: str) -> None:
        for key in self.keys:
            if key.id == key_id and not key.disabled:
                key.disabled = True
                log.warning("Disabled key %s after an authentication failure", key_id)

    def cool_down(self, key_id: str, seconds: float | None) -> None:
        wait = DEFAULT_RATE_LIMIT_COOLDOWN_S if seconds is None else seconds
        for key in self.keys:
            if key.id == key_id:
                key.cooling_until = max(key.cooling_until, self.clock() + wait)

    def retry_in(self, allowed: Collection[str] = ()) -> float | None:
        """Seconds until a cooling key becomes usable again, if any will."""
        waiting = [k.cooling_until for k in self._candidates(allowed)]
        if not waiting:
            return None
        return max(0.0, min(waiting) - self.clock())

    def _candidates(self, allowed: Collection[str]) -> list[KeyState]:
        """Keys that are not disabled, limited to `allowed` when it is not empty."""
        return [k for k in self.keys if not k.disabled and (not allowed or k.id in allowed)]

    @property
    def usable_count(self) -> int:
        now = self.clock()
        return sum(1 for k in self.keys if not k.disabled and k.cooling_until <= now)


class NoUsableKeyError(Exception):
    def __init__(self, provider_id: str, retry_in_s: float | None) -> None:
        super().__init__(f"provider '{provider_id}' has no usable key right now")
        self.provider_id = provider_id
        self.retry_after_s = retry_in_s


def build_key_pool(
    provider: ProviderConfig,
    environ: Mapping[str, str],
    *,
    keyless: bool,
    clock: Clock = time.monotonic,
    rng: random.Random | None = None,
) -> KeyPool:
    """Read a provider's keys from the environment. Unset variables are skipped."""
    keys: list[KeyState] = []
    for key in provider.keys:
        value = environ.get(key.env)
        if not value:
            log.warning("Key variable %s for provider %s is not set", key.env, provider.id)
            continue
        keys.append(KeyState(key_id(provider.id, key.env), SecretStr(value), key.weight))
    return KeyPool(
        provider.id,
        keys,
        keyless=keyless and not keys,
        clock=clock,
        rng=rng or random.Random(),
    )
