"""Model ids and the cost of one call (no database needed)."""
from decimal import Decimal as D

import pytest

from patient360.adapters.postgres.pricing import call_cost, normalize_model_id

HAIKU = {"input_per_1m": D("0.10"), "output_per_1m": D("0.50"), "cache_write_1h_per_1m": D("0.20"),
         "cache_read_per_1m": D("0.01")}
NO_CACHE = {"input_per_1m": D("1.00"), "output_per_1m": D("5.00"), "cache_write_1h_per_1m": None,
            "cache_read_per_1m": None}


@pytest.mark.parametrize("raw, expected", [
    ("claude-sonnet-4-6", "claude-sonnet-4-6"),
    ("eu.anthropic.claude-sonnet-5-5", "claude-sonnet-5-5"),
    ("us.anthropic.claude-haiku-4-5-20251001-v1:0", "claude-haiku-4-5"),
    ("global.anthropic.claude-sonnet-4-6", "claude-sonnet-4-6"),
    ("anthropic.claude-sonnet-4-6-v1:0", "claude-sonnet-4-6"),
    ("amazon.nova-2-lite-v1:0", "nova-2-lite"),
    ("us.amazon.nova-2-lite-v1:0", "nova-2-lite"),
    (" Claude-Haiku-5-5 ", "claude-haiku-5-5"),
])
def test_the_bedrock_id_and_the_short_name_normalize_to_the_same_id(raw, expected):
    assert normalize_model_id(raw) == expected


def test_a_call_is_priced_from_its_tokens_and_the_price_row():
    usage = {"input_tokens": 1_000_000, "output_tokens": 2_000_000, "cache_creation_input_tokens": 500_000,
             "cache_creation": {"ephemeral_1h_input_tokens": 500_000}, "cache_read_input_tokens": 3_000_000}
    cost = call_cost(usage, HAIKU)
    assert cost["input_cost_usd"] == D("0.10") and cost["output_cost_usd"] == D("1.00")
    assert cost["cache_cost_usd"] == D("0.10") + D("0.03")
    assert cost["total_cost_usd"] == D("0.10") + D("1.00") + D("0.13")
    assert (cost["price_input_per_1m"], cost["price_output_per_1m"]) == (D("0.10"), D("0.50"))


def test_a_call_without_cache_fields_has_no_cache_cost_and_a_total_of_input_plus_output():
    cost = call_cost({"input_tokens": 1000, "output_tokens": 100}, NO_CACHE)
    assert cost["cache_cost_usd"] is None
    assert cost["total_cost_usd"] == D("0.001") + D("0.0005")


def test_unknown_tokens_mean_unknown_cost_not_zero():
    cost = call_cost({"input_tokens": 1000, "output_tokens": None}, HAIKU)
    assert cost["input_cost_usd"] == D("0.0001")
    assert cost["output_cost_usd"] is None and cost["total_cost_usd"] is None


def test_5m_cache_tokens_are_skipped_with_a_warning_because_there_is_no_5m_price():
    from structlog.testing import capture_logs
    usage = {"input_tokens": 10, "output_tokens": 10, "cache_creation": {"ephemeral_5m_input_tokens": 7}}
    with capture_logs() as logs:
        cost = call_cost(usage, HAIKU)
    assert cost["cache_cost_usd"] is None and cost["total_cost_usd"] is not None
    assert [(e["event"], e["log_level"]) for e in logs] == [("cache_5m_tokens_not_priced", "warning")]


def test_no_price_row_means_no_cost():
    assert call_cost({"input_tokens": 1, "output_tokens": 1}, None) is None
