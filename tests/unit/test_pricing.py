import logging
from decimal import Decimal

import pytest

from prism.config import parse_config
from prism.core.pricing import ModelPrice, PriceBook, build_price_book
from prism.core.types import Usage

USAGE = Usage(prompt_tokens=1_000, completion_tokens=500)


def test_cost_charges_input_and_output_tokens_at_their_own_price() -> None:
    price = ModelPrice(input_per_million=Decimal("0.59"), output_per_million=Decimal("0.79"))
    # 1,000 x 0.59 / 1M + 500 x 0.79 / 1M
    assert price.cost(USAGE) == Decimal("0.000985")


def test_cost_is_exact_with_no_float_rounding() -> None:
    price = ModelPrice(Decimal("0.1"), Decimal("0.2"))
    usage = Usage(prompt_tokens=3, completion_tokens=0)
    assert price.cost(usage) == Decimal("0.0000003")


def test_no_tokens_cost_nothing() -> None:
    assert ModelPrice(Decimal(3), Decimal(15)).cost(Usage()) == 0


def test_the_price_book_looks_up_by_provider_and_model() -> None:
    book = PriceBook({("groq", "m"): ModelPrice(Decimal(1), Decimal(2))})
    assert book.cost("groq", "m", USAGE) == Decimal("0.002")
    assert book.price("other", "m") is None


def test_an_unpriced_model_is_free_and_warned_about_once(caplog: pytest.LogCaptureFixture) -> None:
    book = PriceBook()
    with caplog.at_level(logging.WARNING, logger="prism.core.pricing"):
        assert book.cost("groq", "m", USAGE) == 0
        assert book.cost("groq", "m", USAGE) == 0
        assert book.cost("groq", "other", USAGE) == 0
    warnings = [r.getMessage() for r in caplog.records]
    assert len(warnings) == 2
    assert "No price for groq/m" in warnings[0]
    assert "provider 'groq'" in warnings[0]


def test_prices_are_read_from_every_provider_in_the_config() -> None:
    config = parse_config(
        {
            "providers": [
                {
                    "id": "groq",
                    "kind": "groq",
                    "keys": [{"env": "K"}],
                    "prices": {"m": {"input": 1, "output": 2}},
                },
                {"id": "local", "kind": "ollama", "prices": {"m": {"input": 0, "output": 0}}},
            ],
            "routes": [{"name": "r", "targets": [{"provider": "groq", "model": "m"}]}],
        }
    )
    book = build_price_book(config)
    assert book.price("groq", "m") == ModelPrice(Decimal(1), Decimal(2))
    assert book.cost("local", "m", USAGE) == 0
