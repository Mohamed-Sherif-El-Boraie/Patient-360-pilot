"""tools/set_price.py: the argument checks need no database; the price change runs on a real PostgreSQL."""
import importlib.util
from decimal import Decimal
from pathlib import Path

import psycopg
import pytest
from psycopg.rows import dict_row

SPEC = importlib.util.spec_from_file_location("set_price", Path(__file__).parents[3] / "tools" / "set_price.py")
set_price = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(set_price)

BASE = ["claude-haiku-5-5", "--mode", "on_demand", "--in", "0.10", "--out", "0.50"]


def test_the_arguments_are_read_as_decimals():
    args = set_price.parse(BASE + ["--cache-write-1h", "0.20", "--cache-read", "0.01"])
    assert (args.input_per_1m, args.output_per_1m) == (Decimal("0.10"), Decimal("0.50"))
    assert (args.cache_write_1h_per_1m, args.cache_read_per_1m) == (Decimal("0.20"), Decimal("0.01"))
    assert set_price.parse(["--list"]).list


@pytest.mark.parametrize("argv", [
    ["m", "--mode", "spot", "--in", "1", "--out", "1"],              # unknown mode
    ["m", "--mode", "on_demand", "--in", "0", "--out", "1"],         # not positive
    ["m", "--mode", "on_demand", "--in", "-1", "--out", "1"],
    ["m", "--mode", "on_demand", "--in", "abc", "--out", "1"],
    ["m", "--mode", "on_demand", "--in", "NaN", "--out", "1"],
    ["m", "--mode", "on_demand", "--in", "1"],                       # --out missing
    ["--mode", "on_demand", "--in", "1", "--out", "1"],              # no model
])
def test_a_bad_argument_stops_with_exit_2(argv):
    with pytest.raises(SystemExit) as stop:
        set_price.parse(argv)
    assert stop.value.code == 2


def test_the_price_change_closes_the_current_row_and_opens_a_new_one(db):
    with psycopg.connect(db._pool.conninfo, row_factory=dict_row) as conn:
        args = set_price.parse(["us.anthropic.claude-haiku-5-5-v1:0", "--mode", "on_demand", "--in", "1", "--out", "2"])
        before, after = set_price.set_price(conn, args)
        assert (before["input_per_1m"], after["input_per_1m"]) == (Decimal("0.10"), Decimal("1"))
        rows = conn.execute("SELECT * FROM model_prices WHERE model_id = 'claude-haiku-5-5' AND mode = 'on_demand' "
                            "ORDER BY effective_from").fetchall()
    assert [r["effective_to"] is None for r in rows] == [False, True]      # one current row
    assert rows[0]["effective_to"] == rows[1]["effective_from"]            # no gap, no overlap
