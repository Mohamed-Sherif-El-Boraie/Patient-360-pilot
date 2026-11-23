"""tools/backfill_costs.py: the calculation of one old call (no database needed; the SQL runs on a real PostgreSQL)."""
import importlib.util
from decimal import Decimal
from pathlib import Path

TOOL = Path(__file__).parents[1] / "tools" / "backfill_costs.py"
SPEC = importlib.util.spec_from_file_location("backfill_costs", TOOL)
backfill = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(backfill)

HAIKU = {"input_per_1m": Decimal("0.10"), "output_per_1m": Decimal("0.50"), "cache_write_1h_per_1m": Decimal("0.20"),
         "cache_read_per_1m": Decimal("0.01")}
NOVA = {"input_per_1m": Decimal("0.429"), "output_per_1m": Decimal("3.597"), "cache_write_1h_per_1m": None,
        "cache_read_per_1m": None}
PRICES = {("claude-haiku-5-5", "on_demand"): HAIKU, ("nova-2-lite", "on_demand"): NOVA}


def price_for(model, mode):
    return PRICES.get((backfill.normalize_model_id(model), mode))


def call(**kw):
    row = {"visit_id": 1, "analysis": "visit", "purpose": "analysis", "model_id": "eu.anthropic.claude-haiku-5-5",
           "mode": "on_demand", "input_tokens": 4025, "output_tokens": 1886, "cache_creation_5m_input_tokens": None,
           "cache_creation_1h_input_tokens": 0, "cache_read_input_tokens": 3999, "total_cost_usd": None}
    return {**row, **kw}


def test_an_english_call_gets_its_cost_from_the_saved_tokens():
    new = backfill.rebuild(call(), price_for)
    assert new["model_id"] == "eu.anthropic.claude-haiku-5-5" and new["corrected"] is False and new["priced"] is True
    assert new["input_cost_usd"] == Decimal(4025) * Decimal("0.10") / 1_000_000
    assert new["total_cost_usd"] == (Decimal(4025) * Decimal("0.10") + Decimal(1886) * Decimal("0.50")
                                     + Decimal(3999) * Decimal("0.01")) / 1_000_000
    assert new["price_input_per_1m"] == Decimal("0.10")


def test_a_translation_call_that_names_haiku_becomes_nova_lite_and_is_priced_as_nova():
    new = backfill.rebuild(call(purpose="translation", input_tokens=557, output_tokens=1041,
                                cache_read_input_tokens=1500), price_for)
    assert new["model_id"] == backfill.ARABIC_MODEL_ID and new["corrected"] is True
    assert new["total_cost_usd"] == (Decimal(557) * Decimal("0.429") + Decimal(1041) * Decimal("3.597")) / 1_000_000
    assert new["cache_cost_usd"] is None                         # Nova has no cache price: no cache cost invented


def test_a_call_that_is_done_is_left_alone_so_a_second_run_changes_nothing():
    assert backfill.rebuild(call(total_cost_usd=Decimal("0.001")), price_for) is None
    done = call(purpose="translation", model_id=backfill.ARABIC_MODEL_ID, total_cost_usd=Decimal("0.002"))
    assert backfill.rebuild(done, price_for) is None


def test_unknown_tokens_no_mode_or_no_price_leave_the_cost_empty_never_zero():
    assert backfill.rebuild(call(input_tokens=None), price_for)["total_cost_usd"] is None
    assert backfill.rebuild(call(mode=None), price_for)["priced"] is False
    assert backfill.rebuild(call(model_id="claude-unknown-9"), price_for)["total_cost_usd"] is None
    wrong = backfill.rebuild(call(purpose="translation", mode=None), price_for)
    assert wrong["corrected"] is True and wrong["total_cost_usd"] is None     # the model is still fixed
