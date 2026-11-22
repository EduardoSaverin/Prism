"""What a request costs, from its token usage and the model's price.

Prices come from the config, in US dollars per million input and output tokens
(ADR 0010). A model with no price costs $0, and Prism logs a warning the first
time it sees that model, so a missing price is noticed without flooding the log.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal

from prism.config import PrismConfig
from prism.core.types import Usage

log = logging.getLogger(__name__)

TOKENS_PER_PRICE_UNIT = Decimal(1_000_000)
FREE = Decimal(0)


@dataclass(frozen=True)
class ModelPrice:
    """US dollars per million input and output tokens."""

    input_per_million: Decimal
    output_per_million: Decimal

    def cost(self, usage: Usage) -> Decimal:
        spent = (
            usage.prompt_tokens * self.input_per_million
            + usage.completion_tokens * self.output_per_million
        )
        return spent / TOKENS_PER_PRICE_UNIT


class PriceBook:
    """Looks up a model's price and turns token usage into US dollars."""

    def __init__(self, prices: Mapping[tuple[str, str], ModelPrice] | None = None) -> None:
        self._prices = dict(prices or {})
        self._warned: set[tuple[str, str]] = set()

    def price(self, provider_id: str, model: str) -> ModelPrice | None:
        return self._prices.get((provider_id, model))

    def cost(self, provider_id: str, model: str, usage: Usage) -> Decimal:
        price = self.price(provider_id, model)
        if price is not None:
            return price.cost(usage)
        if (provider_id, model) not in self._warned:
            self._warned.add((provider_id, model))
            log.warning(
                "No price for %s/%s, so its requests cost $0. "
                "Add it under prices for provider '%s' in the config.",
                provider_id,
                model,
                provider_id,
            )
        return FREE


def build_price_book(config: PrismConfig) -> PriceBook:
    """Collect the prices of every provider in the config."""
    return PriceBook(
        {
            (provider.id, model): ModelPrice(price.input, price.output)
            for provider in config.providers
            for model, price in provider.prices.items()
        }
    )
