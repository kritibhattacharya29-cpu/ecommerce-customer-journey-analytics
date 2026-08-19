"""Load the DuckDB marts into the PostgreSQL serving layer.

Only modelled tables cross this boundary — never the 36M raw events. See
sql/postgres/01_schema.sql for the reasoning.

Transfer is done with COPY ... FROM STDIN rather than row-by-row INSERT: for a
few million rows the difference is minutes versus hours, because COPY skips
per-statement parse/plan overhead and the client-server round trip per row.

Prerequisites:
    createdb coveo_analytics
    psql -d coveo_analytics -f sql/postgres/01_schema.sql

Usage:
    python -m src.transform.load_postgres
    python -m src.transform.load_postgres --only fct_session
"""
from __future__ import annotations

import argparse
import io
import time

import psycopg2

from src import config
from src.ingest.build_duckdb import connect as duck_connect

# DuckDB table -> (postgres table, column list). Explicit column lists mean a
# schema drift on either side fails loudly instead of silently mis-aligning.
TABLES = {
    "dim_product": (
        "coveo.dim_product",
        ["product_sku_hash", "in_catalog", "category_lvl1", "category_lvl2",
         "category_lvl3", "category_hash", "category_depth", "price_bucket",
         "has_price", "price_tier"],
    ),
    "fct_session": (
        "coveo.fct_session",
        ["session_id_hash", "session_start", "session_end", "day_of_week",
         "hour_of_day", "duration_sec_raw", "duration_sec_capped", "n_events",
         "n_pageviews", "n_product_views", "n_adds", "n_removes", "n_purchases",
         "n_skus_viewed", "n_skus_added", "n_skus_purchased", "n_unique_pages",
         "reached_session", "reached_detail", "reached_add", "reached_purchase",
         "strict_detail", "strict_add", "strict_purchase",
         "purchase_without_add", "is_bounce", "cart_abandoned", "cart_emptied",
         "sec_to_first_add", "sec_to_purchase", "sec_add_to_purchase",
         "n_searches", "n_zero_result_searches", "n_search_clicks",
         "n_valid_search_clicks", "n_search_impressions", "used_search",
         "had_duplicate_events"],
    ),
    "fct_product_performance": (
        "coveo.fct_product_performance",
        ["product_sku_hash", "sessions_viewed", "sessions_added",
         "sessions_purchased", "sessions_removed", "n_views", "n_adds",
         "n_purchases", "in_catalog", "category_lvl1", "category_lvl2",
         "price_bucket", "price_tier", "view_to_add_rate",
         "add_to_purchase_rate", "view_to_purchase_rate"],
    ),
}

CHUNK = 250_000


def pg_connect():
    return psycopg2.connect(
        host=config.PG["host"], port=config.PG["port"],
        dbname=config.PG["dbname"], user=config.PG["user"],
        password=config.PG["password"],
    )


def copy_table(duck, pg, duck_table: str, pg_table: str, cols: list[str]) -> int:
    col_sql = ", ".join(cols)
    total = duck.execute(f"SELECT count(*) FROM {duck_table}").fetchone()[0]
    print(f"  {duck_table} -> {pg_table}  ({total:,} rows)")

    with pg.cursor() as cur:
        cur.execute(f"TRUNCATE {pg_table} CASCADE")

        moved = 0
        t0 = time.time()
        while moved < total:
            df = duck.execute(
                f"SELECT {col_sql} FROM {duck_table} "
                f"LIMIT {CHUNK} OFFSET {moved}"
            ).df()
            if df.empty:
                break

            buf = io.StringIO()
            df.to_csv(buf, index=False, header=False, na_rep="\\N")
            buf.seek(0)
            cur.copy_expert(
                f"COPY {pg_table} ({col_sql}) FROM STDIN WITH (FORMAT csv, NULL '\\N')",
                buf,
            )
            moved += len(df)
            pct = 100.0 * moved / total
            print(f"    {moved:>10,} / {total:,}  ({pct:5.1f}%)", end="\r", flush=True)

        pg.commit()
        print(f"    {moved:>10,} rows in {time.time() - t0:,.1f}s" + " " * 20)
    return moved


def main() -> None:
    ap = argparse.ArgumentParser(description="Load DuckDB marts into PostgreSQL.")
    ap.add_argument("--only", choices=sorted(TABLES), help="Load a single table.")
    args = ap.parse_args()

    targets = {args.only: TABLES[args.only]} if args.only else TABLES

    duck = duck_connect(read_only=True)
    try:
        pg = pg_connect()
    except psycopg2.OperationalError as e:
        raise SystemExit(
            f"Could not connect to PostgreSQL at "
            f"{config.PG['host']}:{config.PG['port']}/{config.PG['dbname']}\n\n"
            f"{e}\n"
            "PostgreSQL must be installed and running, and the schema created:\n"
            "    createdb coveo_analytics\n"
            "    psql -d coveo_analytics -f sql/postgres/01_schema.sql\n"
            "Connection settings come from .env (PGHOST/PGPORT/PGDATABASE/...)."
        )

    print(f"PostgreSQL {config.PG['host']}:{config.PG['port']}/{config.PG['dbname']}\n")
    t0 = time.time()
    # dim_product first: fct_product_performance has a FK onto it.
    for name in sorted(targets, key=lambda n: 0 if n.startswith("dim") else 1):
        pg_table, cols = targets[name]
        copy_table(duck, pg, name, pg_table, cols)

    with pg.cursor() as cur:
        cur.execute("ANALYZE coveo.dim_product")
        cur.execute("ANALYZE coveo.fct_session")
        cur.execute("ANALYZE coveo.fct_product_performance")
    pg.commit()

    print(f"\nTotal {time.time() - t0:,.1f}s")
    pg.close()
    duck.close()


if __name__ == "__main__":
    main()
