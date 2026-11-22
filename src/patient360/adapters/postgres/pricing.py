"""What a model call cost: the model id is normalized, the price row of that model and mode is read, then the cost.

Prices live in the ``model_prices`` table (USD per 1M tokens; changed with ``tools/set_price.py``). A call whose model
has no price gets a NULL cost and a warning: a cost is never guessed and never stops a save.
"""
from __future__ import annotations

import re
from decimal import Decimal
from typing import Any

import structlog

log = structlog.get_logger()

# a Bedrock id: [region.][provider.]name[-date][-v1:0], e.g. us.anthropic.claude-haiku-4-5-20251001-v1:0
_PREFIX = re.compile(r"^(?:[a-z]{2,6}\.)?(?:anthropic|amazon)\.")
_SUFFIX = re.compile(r"(?:-v\d+(?::\d+)?)?$")
_DATE = re.compile(r"-\d{8}$")
_MILLION = Decimal(1_000_000)

# The Arabic (translation) call is always this model, on demand and in batch. AWS never reports it in the output.
ARABIC_MODEL_ID = "amazon.nova-2-lite-v1:0"      # normalizes to the 'nova-2-lite' price rows of migration 003

_PRICE_SQL = ("SELECT input_per_1m, output_per_1m, cache_write_1h_per_1m, cache_read_per_1m FROM model_prices "
              "WHERE model_id = %s AND mode = %s AND effective_from <= now() "
              "AND (effective_to IS NULL OR effective_to > now()) ORDER BY effective_from DESC LIMIT 1")


def normalize_model_id(model: str) -> str:
    """``us.anthropic.claude-haiku-4-5-20251001-v1:0`` and ``claude-haiku-4-5`` both give ``claude-haiku-4-5``."""
    name = _PREFIX.sub("", model.strip().lower())
    return _DATE.sub("", _SUFFIX.sub("", name, count=1))


def price_of(conn, model: str, mode: str) -> dict[str, Any] | None:
    """The price row valid now for this model and mode, or ``None`` (with a warning naming both)."""
    row = conn.execute(_PRICE_SQL, (normalize_model_id(model), mode)).fetchone()
    if row is None:
        log.warning("model_price_missing", model_id=model, mode=mode)
    return row


def _times(tokens: int | None, price: Decimal | None) -> Decimal | None:
    if not isinstance(tokens, int) or isinstance(tokens, bool) or price is None:   # unknown, or not a count
        return None
    return Decimal(tokens) * price / _MILLION


def call_cost(usage: dict[str, Any], price: dict[str, Any] | None) -> dict[str, Decimal | None] | None:
    """The cost columns of ``ai_model_calls`` for one call, or ``None`` when there is no price.

    Unknown (missing) input or output tokens give a NULL cost, never zero. Cache tokens count only when the call
    reports them and the price has a cache price. 5-minute cache writes have no price: skipped, with a warning."""
    if price is None:
        return None
    written = usage.get("cache_creation") or {}
    if written.get("ephemeral_5m_input_tokens"):
        log.warning("cache_5m_tokens_not_priced", status="skipped")
    parts = [_times(written.get("ephemeral_1h_input_tokens"), price["cache_write_1h_per_1m"]),
             _times(usage.get("cache_read_input_tokens"), price["cache_read_per_1m"])]
    cache = sum((p for p in parts if p is not None), Decimal(0)) if any(p is not None for p in parts) else None
    cost_in = _times(usage.get("input_tokens"), price["input_per_1m"])
    cost_out = _times(usage.get("output_tokens"), price["output_per_1m"])
    total = None if cost_in is None or cost_out is None else cost_in + cost_out + (cache or Decimal(0))
    return {"input_cost_usd": cost_in, "output_cost_usd": cost_out, "cache_cost_usd": cache, "total_cost_usd": total,
            "price_input_per_1m": price["input_per_1m"], "price_output_per_1m": price["output_per_1m"]}
