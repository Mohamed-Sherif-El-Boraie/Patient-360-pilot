"""Calculate the cost of model calls saved before the cost columns existed (migration 003). Run it over SSH:

    python /app/tools/backfill_costs.py               # dry run: says what would change, writes nothing
    python /app/tools/backfill_costs.py --apply       # writes it, all in one transaction
    python /app/tools/backfill_costs.py --apply --include-failed

What it does, for the calls of DELIVERED work (``record_processing.status = 'delivered'``):
  * a translation call (the Arabic one) is always Amazon Nova 2 Lite: a row that names another model is corrected;
  * a call with no cost gets one, from the tokens already saved and the price valid NOW in ``model_prices``
    (so a price changed since the call is applied to old calls too; change prices first only if that is wanted).
A call with an unknown token count or no price keeps a NULL cost: a cost is never guessed. Safe to run again: it only
touches calls that still have no cost, or a translation call with the wrong model.
"""
import argparse
import sys
from decimal import Decimal
from typing import Any

import psycopg
from psycopg import sql as pgsql
from psycopg.rows import dict_row

from patient360.adapters.postgres.pricing import ARABIC_MODEL_ID, call_cost, normalize_model_id, price_of
from patient360.config.settings import SettingsError, load_settings

COST_COLUMNS = ("input_cost_usd", "output_cost_usd", "cache_cost_usd", "total_cost_usd", "price_input_per_1m",
                "price_output_per_1m")
SELECT = (
    "SELECT m.visit_id, m.analysis, m.purpose, m.model_id, m.mode, m.input_tokens, m.output_tokens, "
    "m.cache_creation_5m_input_tokens, m.cache_creation_1h_input_tokens, m.cache_read_input_tokens, "
    "m.total_cost_usd FROM ai_model_calls m JOIN record_processing p ON p.visit_id = m.visit_id "
    "AND p.analysis = m.analysis WHERE p.status = ANY(%s) ORDER BY m.visit_id, m.analysis, m.purpose")
UPDATE = ("UPDATE ai_model_calls SET model_id = %(model_id)s, " + ", ".join(f"{c} = %({c})s" for c in COST_COLUMNS)
          + " WHERE visit_id = %(visit_id)s AND analysis = %(analysis)s AND purpose = %(purpose)s")


def usage_of(row: dict[str, Any]) -> dict[str, Any]:
    """The ``usage`` object AWS gave, rebuilt from the saved columns, so ``call_cost`` is the one place that prices."""
    return {"input_tokens": row["input_tokens"], "output_tokens": row["output_tokens"],
            "cache_read_input_tokens": row["cache_read_input_tokens"],
            "cache_creation": {"ephemeral_1h_input_tokens": row["cache_creation_1h_input_tokens"],
                               "ephemeral_5m_input_tokens": row["cache_creation_5m_input_tokens"]}}


def rebuild(row: dict[str, Any], price_for) -> dict[str, Any] | None:
    """The new values of one call, or ``None`` when there is nothing to change.
    ``price_for(model, mode)`` returns the price row or ``None``."""
    corrected = row["purpose"] == "translation" and normalize_model_id(row["model_id"]) != normalize_model_id(
        ARABIC_MODEL_ID)
    if not corrected and row["total_cost_usd"] is not None:
        return None
    model = ARABIC_MODEL_ID if corrected else row["model_id"]
    price = price_for(model, row["mode"]) if row["mode"] else None      # a call with no mode cannot be priced
    cost = call_cost(usage_of(row), price) or {}
    return {"model_id": model, **{c: cost.get(c) for c in COST_COLUMNS}, "corrected": corrected,
            "priced": cost.get("total_cost_usd") is not None}


def backfill(conn: psycopg.Connection, statuses: list[str], apply: bool) -> dict[str, Any]:
    rows = conn.execute(SELECT, (statuses,)).fetchall()
    summary: dict[str, Any] = {"calls_seen": len(rows), "to_change": 0, "model_corrected": 0, "priced": 0,
                               "left_null": 0, "total_usd": Decimal(0)}
    with conn.transaction():
        for row in rows:
            new = rebuild(row, lambda model, mode: price_of(conn, model, mode))
            if new is None:
                continue
            summary["to_change"] += 1
            summary["model_corrected"] += new["corrected"]
            summary["priced"] += new["priced"]
            summary["left_null"] += not new["priced"]
            summary["total_usd"] += new["total_cost_usd"] or Decimal(0)
            if apply:
                conn.execute(UPDATE, {**new, "visit_id": row["visit_id"], "analysis": row["analysis"],
                                      "purpose": row["purpose"]})
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Calculate the cost of old model calls (dry run unless --apply).")
    parser.add_argument("--apply", action="store_true", help="write the changes (without it nothing is written)")
    parser.add_argument("--include-failed", action="store_true", help="also the calls of work that failed")
    args = parser.parse_args(argv)
    try:
        s = load_settings()
    except SettingsError as error:
        print(f"backfill_costs.py: settings: {error}", file=sys.stderr)
        return 1
    with psycopg.connect(host=s.pg_host, port=s.pg_port, dbname=s.pg_database, user=s.pg_user,
                         password=s.pg_password, sslmode=s.pg_ssl_mode, connect_timeout=10,
                         row_factory=dict_row) as conn:
        if s.pg_schema:
            conn.execute(pgsql.SQL("SET search_path TO {}").format(pgsql.Identifier(s.pg_schema)))
        has_costs = conn.execute("SELECT 1 FROM information_schema.columns WHERE table_schema = current_schema() "
                                 "AND table_name = 'ai_model_calls' AND column_name = 'total_cost_usd'").fetchone()
        if not has_costs:
            print("backfill_costs.py: migration 003 is not applied here: run `python -m patient360.migrate` first",
                  file=sys.stderr)
            return 1
        summary = backfill(conn, ["delivered", "failed"] if args.include_failed else ["delivered"], args.apply)
    print(("APPLIED" if args.apply else "DRY RUN (nothing written; add --apply to write)") + ":")
    print(f"  calls looked at          {summary['calls_seen']}")
    print(f"  calls to change          {summary['to_change']}")
    print(f"    model set to Nova Lite {summary['model_corrected']}")
    print(f"    cost calculated        {summary['priced']}   (sum {summary['total_usd']:.4f} USD)")
    print(f"    left without a cost    {summary['left_null']}   (no price, no mode, or a token count unknown)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
