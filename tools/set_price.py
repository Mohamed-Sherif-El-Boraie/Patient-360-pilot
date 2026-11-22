"""Change the price of a model (USD per 1M tokens). Reads the same PG* settings as the app. Run it over SSH:

    python /app/tools/set_price.py --list
    python /app/tools/set_price.py claude-haiku-5-5 --mode on_demand --in 0.10 --out 0.50 \\
        --cache-write-1h 0.20 --cache-read 0.01
    python /app/tools/set_price.py claude-sonnet-4-6 --mode batch_inference --in 3.00 --out 15.00

The current row of that model and mode is closed (effective_to = now) and a new one opened, in one transaction. The
model id may be the Bedrock id or the short name: both are normalized. Calls saved before keep the price they were
billed at; only calls saved after the change use the new price.
"""
import argparse
import sys
from decimal import Decimal, InvalidOperation

import psycopg
from psycopg import sql as pgsql
from psycopg.rows import dict_row

from patient360.adapters.postgres.pricing import normalize_model_id
from patient360.config.settings import SettingsError, load_settings

COLUMNS = ("model_id, mode, effective_from, effective_to, input_per_1m, output_per_1m, cache_write_1h_per_1m, "
           "cache_read_per_1m")


def money(text: str) -> Decimal:
    try:
        value = Decimal(text)
    except InvalidOperation:
        raise argparse.ArgumentTypeError(f"{text!r} is not a number") from None
    if not value.is_finite() or value <= 0 or value >= 10_000:
        raise argparse.ArgumentTypeError(f"{text!r} must be more than 0 and less than 10000 (USD per 1M tokens)")
    return value


def parse(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Set the price of a model, or list the prices.")
    parser.add_argument("model_id", nargs="?")
    parser.add_argument("--list", action="store_true", help="show every price row and stop")
    parser.add_argument("--mode", choices=("on_demand", "batch_inference"))
    parser.add_argument("--in", dest="input_per_1m", type=money)
    parser.add_argument("--out", dest="output_per_1m", type=money)
    parser.add_argument("--cache-write-1h", dest="cache_write_1h_per_1m", type=money)
    parser.add_argument("--cache-read", dest="cache_read_per_1m", type=money)
    args = parser.parse_args(argv)
    if not args.list and None in (args.model_id, args.mode, args.input_per_1m, args.output_per_1m):
        parser.error("set_price.py: give <model_id> --mode, --in and --out (or use --list)")
    return args


def show(rows: list[dict]) -> None:
    for r in rows:
        print(f"  {r['model_id']:20} {r['mode']:16} in {r['input_per_1m']} out {r['output_per_1m']} "
              f"cache_write_1h {r['cache_write_1h_per_1m']} cache_read {r['cache_read_per_1m']}  "
              f"from {r['effective_from']:%Y-%m-%d %H:%M} to {r['effective_to'] or 'now (current)'}")


def set_price(conn: psycopg.Connection, args: argparse.Namespace) -> tuple[dict | None, dict]:
    """Close the current row, open the new one, in one transaction. Returns (the row replaced or None, the new row)."""
    model = normalize_model_id(args.model_id)
    with conn.transaction():
        before = conn.execute(f"SELECT {COLUMNS} FROM model_prices WHERE model_id = %s AND mode = %s "
                              "AND effective_to IS NULL FOR UPDATE", (model, args.mode)).fetchone()
        conn.execute("UPDATE model_prices SET effective_to = now() "
                     "WHERE model_id = %s AND mode = %s AND effective_to IS NULL", (model, args.mode))
        after = conn.execute(
            "INSERT INTO model_prices (model_id, mode, input_per_1m, output_per_1m, cache_write_1h_per_1m, "
            f"cache_read_per_1m) VALUES (%s, %s, %s, %s, %s, %s) RETURNING {COLUMNS}",
            (model, args.mode, args.input_per_1m, args.output_per_1m, args.cache_write_1h_per_1m,
             args.cache_read_per_1m)).fetchone()
    return before, after


def main(argv: list[str] | None = None) -> int:
    args = parse(argv)
    try:
        s = load_settings()
    except SettingsError as error:
        print(f"set_price.py: settings: {error}", file=sys.stderr)
        return 1
    with psycopg.connect(host=s.pg_host, port=s.pg_port, dbname=s.pg_database, user=s.pg_user,
                         password=s.pg_password, sslmode=s.pg_ssl_mode, connect_timeout=10,
                         row_factory=dict_row) as conn:
        if s.pg_schema:        # the schema exists already: the app made it
            conn.execute(pgsql.SQL("SET search_path TO {}").format(pgsql.Identifier(s.pg_schema)))
        if args.list:
            show(conn.execute(f"SELECT {COLUMNS} FROM model_prices ORDER BY model_id, mode, effective_from").fetchall())
            return 0
        before, after = set_price(conn, args)
    print("before:" if before else "before: no current price")
    if before:
        show([before])
    print("after:")
    show([after])
    return 0


if __name__ == "__main__":
    sys.exit(main())
