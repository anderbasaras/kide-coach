"""Estimated cost of an Anthropic API call, from its token usage.

Prices are USD per million tokens, copied from the official pricing page
(https://platform.claude.com/docs/en/about-claude/pricing) on ``PRICES_CHECKED``.
They are an *estimate* for the user's own budgeting: the invoice is the source of truth.
When Anthropic changes its prices or releases a model, update ``PRICES``.

Cache writes cost 1.25x the base input price (5-minute TTL) or 2x (1-hour TTL); cache
reads have a per-model multiplier. Claude Haiku 5.5 has a second price tier for prompts
longer than 100k tokens (counting cache reads and writes). An unknown model has no
estimate (``None``) rather than a wrong one.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

PRICES_CHECKED = "2026-11-26"
PRICING_URL = "https://platform.claude.com/docs/en/about-claude/pricing"

CACHE_WRITE_5M = Decimal("1.25")
CACHE_WRITE_1H = Decimal("2")
MILLION = Decimal(1_000_000)
USD_QUANTUM = Decimal("0.000001")  # llm_calls.estimated_cost_usd has 6 decimals


@dataclass(frozen=True)
class Tier:
    input: Decimal
    output: Decimal


@dataclass(frozen=True)
class ModelPrice:
    base: Tier
    cache_read_multiplier: Decimal
    # (prompt-token threshold, tier used above it), e.g. Haiku 5.5 above 100k tokens.
    long_prompt: tuple[int, Tier] | None = None

    def tier_for(self, prompt_tokens: int) -> Tier:
        if self.long_prompt is not None and prompt_tokens > self.long_prompt[0]:
            return self.long_prompt[1]
        return self.base


def _d(value: str) -> Decimal:
    return Decimal(value)


PRICES: dict[str, ModelPrice] = {
    "claude-haiku-5-5": ModelPrice(
        base=Tier(_d("0.10"), _d("0.50")),
        cache_read_multiplier=_d("0.1"),
        long_prompt=(100_000, Tier(_d("0.50"), _d("2.50"))),
    ),
    "claude-sonnet-5-5": ModelPrice(Tier(_d("2"), _d("10")), cache_read_multiplier=_d("0.05")),
    "claude-opus-5-5": ModelPrice(Tier(_d("4"), _d("20")), cache_read_multiplier=_d("0.05")),
    "claude-fable-5-2": ModelPrice(Tier(_d("8"), _d("40")), cache_read_multiplier=_d("0.025")),
}


@dataclass(frozen=True)
class TokenUsage:
    """Token counts of one call. ``input`` excludes cached tokens, as the API reports it."""

    input: int = 0
    output: int = 0
    cache_write_5m: int = 0
    cache_write_1h: int = 0
    cache_read: int = 0

    @property
    def cache_write(self) -> int:
        return self.cache_write_5m + self.cache_write_1h

    @property
    def prompt(self) -> int:
        """Every input token of the request: uncached, written to and read from the cache."""
        return self.input + self.cache_write + self.cache_read

    def __add__(self, other: TokenUsage) -> TokenUsage:
        return TokenUsage(
            input=self.input + other.input,
            output=self.output + other.output,
            cache_write_5m=self.cache_write_5m + other.cache_write_5m,
            cache_write_1h=self.cache_write_1h + other.cache_write_1h,
            cache_read=self.cache_read + other.cache_read,
        )


def price_for(model: str) -> ModelPrice | None:
    """Price of ``model``; dated ids (``claude-sonnet-5-5-20261101``) match their family."""
    if model in PRICES:
        return PRICES[model]
    matches = [key for key in PRICES if model.startswith(f"{key}-")]
    return PRICES[max(matches, key=len)] if matches else None


def estimate_cost(model: str, usage: TokenUsage) -> Decimal | None:
    """Estimated USD cost of one call, or ``None`` if the model's price is unknown."""
    price = price_for(model)
    if price is None:
        return None
    tier = price.tier_for(usage.prompt)
    input_cost = (
        usage.input * tier.input
        + usage.cache_write_5m * tier.input * CACHE_WRITE_5M
        + usage.cache_write_1h * tier.input * CACHE_WRITE_1H
        + usage.cache_read * tier.input * price.cache_read_multiplier
    )
    total = (input_cost + usage.output * tier.output) / MILLION
    return total.quantize(USD_QUANTUM)
