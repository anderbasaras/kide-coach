from decimal import Decimal

from kide.llm.pricing import PRICES, TokenUsage, estimate_cost, price_for


def test_sonnet_plain_call():
    # 1M input at $2 + 1M output at $10
    usage = TokenUsage(input=1_000_000, output=1_000_000)
    assert estimate_cost("claude-sonnet-5-5", usage) == Decimal("12.000000")


def test_cache_writes_and_reads_use_their_multipliers():
    usage = TokenUsage(cache_write_5m=1_000_000, cache_write_1h=1_000_000, cache_read=1_000_000)
    # Opus 5.5: 5m write $5, 1h write $8, read $0.20 (0.05x)
    assert estimate_cost("claude-opus-5-5", usage) == Decimal("13.200000")


def test_haiku_long_prompt_tier_counts_cached_tokens():
    short = TokenUsage(input=50_000, cache_read=50_000, output=1_000)
    long = TokenUsage(input=50_000, cache_read=50_001, output=1_000)
    # <=100k: 50k*0.10 + 50k*0.01 + 1k*0.50 ; >100k: 50k*0.50 + 50,001*0.05 + 1k*2.50
    assert estimate_cost("claude-haiku-5-5", short) == Decimal("0.006000")
    assert estimate_cost("claude-haiku-5-5", long) == Decimal("0.030000")


def test_dated_model_ids_match_their_family_and_unknown_models_have_no_estimate():
    assert price_for("claude-sonnet-5-5-20261101") is PRICES["claude-sonnet-5-5"]
    assert price_for("claude-sonnet-5") is None
    assert estimate_cost("some-other-model", TokenUsage(input=10)) is None


def test_usage_totals():
    usage = TokenUsage(input=1, output=2, cache_write_5m=3, cache_write_1h=4, cache_read=5)
    assert usage.cache_write == 7
    assert usage.prompt == 13
    assert (usage + usage).output == 4
