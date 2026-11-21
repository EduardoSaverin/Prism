"""The shared key cache in Redis, for deployments with more than one instance.

Every instance reads and writes the same entries, so evicting a key here (as
`prism keys revoke` does) takes effect on every instance at once. Redis errors
are treated as cache misses: the gateway then reads the database.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from prism.core.governance.keys import KeyRecord

if TYPE_CHECKING:
    from redis.asyncio import Redis

log = logging.getLogger(__name__)

KEY_NAMESPACE = "prism:vk:"
DEFAULT_TTL_S = 300


class RedisKeyCache:
    def __init__(self, client: Redis, *, ttl_s: int = DEFAULT_TTL_S) -> None:
        self.client = client
        self.ttl_s = ttl_s

    async def get(self, key_hash: str) -> KeyRecord | None:
        try:
            raw = await self.client.get(KEY_NAMESPACE + key_hash)
        except Exception:
            log.warning("Redis is unavailable; reading the key from the database", exc_info=True)
            return None
        if raw is None:
            return None
        try:
            return KeyRecord.from_dict(json.loads(raw))
        except (ValueError, KeyError, TypeError):
            log.warning("Ignoring an unreadable key entry in Redis")
            return None

    async def set(self, key_hash: str, record: KeyRecord) -> None:
        try:
            await self.client.set(
                KEY_NAMESPACE + key_hash, json.dumps(record.to_dict()), ex=self.ttl_s
            )
        except Exception:
            log.warning("Could not cache a key in Redis", exc_info=True)

    async def evict(self, key_hash: str) -> None:
        try:
            await self.client.delete(KEY_NAMESPACE + key_hash)
        except Exception:
            log.warning("Could not evict a key from Redis", exc_info=True)


def connect(url: str) -> Redis:
    """Create a Redis client, with a clear message if the extra is not installed."""
    try:
        from redis.asyncio import Redis
    except ImportError as exc:  # pragma: no cover - depends on the install
        raise RuntimeError(
            "PRISM_REDIS_URL is set but the Redis client is not installed. "
            "Install it with: pip install 'prismgw[redis]'"
        ) from exc
    return Redis.from_url(url)
