"""Cross-engine verification: does PostgreSQL agree with DuckDB?

A load that finishes without error is not a load that is correct. Row counts
can match while values are silently mangled -- a float written where an integer
was expected, a timestamp shifted by a timezone, a NULL turned into a zero.

This runs the same aggregates against both engines and compares them. It is the
check that lets the README claim the PostgreSQL layer works, rather than that it
ran.

Usage:
    python -m src.transform.verify_postgres
"""
from __future__ import annotations

import sys

import psycopg2

from src.ingest.build_duckdb import connect as duck_connect
from src.transform.load_postgres import pg_connect

# (label, duckdb sql, postgres sql). Written separately rather than shared,
# because a shared string would hide a dialect difference that changes results.
CHECKS: list[tuple[str, str, str]] = [
    ("dim_product row count",
     "SELECT count(*) FROM dim_product",
     "SELECT count(*) FROM coveo.dim_product"),

    ("fct_session row count",
     "SELECT count(*) FROM fct_session",
     "SELECT count(*) FROM coveo.fct_session"),

    ("fct_product_performance row count",
     "SELECT count(*) FROM fct_product_performance",
     "SELECT count(*) FROM coveo.fct_product_performance"),

    ("sessions reaching detail",
     "SELECT count(*) FROM fct_session WHERE reached_detail",
     "SELECT count(*) FROM coveo.fct_session WHERE reached_detail"),

    ("sessions reaching add",
     "SELECT count(*) FROM fct_session WHERE reached_add",
     "SELECT count(*) FROM coveo.fct_session WHERE reached_add"),

    ("sessions reaching purchase",
     "SELECT count(*) FROM fct_session WHERE reached_purchase",
     "SELECT count(*) FROM coveo.fct_session WHERE reached_purchase"),

    ("strict-sequence purchases",
     "SELECT count(*) FROM fct_session WHERE strict_purchase",
     "SELECT count(*) FROM coveo.fct_session WHERE strict_purchase"),

    ("cart abandonments",
     "SELECT count(*) FROM fct_session WHERE cart_abandoned",
     "SELECT count(*) FROM coveo.fct_session WHERE cart_abandoned"),

    ("purchases without add",
     "SELECT count(*) FROM fct_session WHERE purchase_without_add",
     "SELECT count(*) FROM coveo.fct_session WHERE purchase_without_add"),

    ("total events across sessions",
     "SELECT sum(n_events) FROM fct_session",
     "SELECT sum(n_events) FROM coveo.fct_session"),

    # The column whose HUGEINT->float64 downcast broke the first load attempt.
    ("total search clicks",
     "SELECT sum(n_search_clicks) FROM fct_session",
     "SELECT sum(n_search_clicks) FROM coveo.fct_session"),

    ("total search impressions",
     "SELECT sum(n_search_impressions) FROM fct_session",
     "SELECT sum(n_search_impressions) FROM coveo.fct_session"),

    ("zero-result searches",
     "SELECT sum(n_zero_result_searches) FROM fct_session",
     "SELECT sum(n_zero_result_searches) FROM coveo.fct_session"),

    ("SKUs missing from catalog",
     "SELECT count(*) FROM dim_product WHERE NOT in_catalog",
     "SELECT count(*) FROM coveo.dim_product WHERE NOT in_catalog"),

    ("SKUs with unknown price tier",
     "SELECT count(*) FROM dim_product WHERE price_tier = '(unknown)'",
     "SELECT count(*) FROM coveo.dim_product WHERE price_tier = '(unknown)'"),

    ("product views total",
     "SELECT sum(sessions_viewed) FROM fct_product_performance",
     "SELECT sum(sessions_viewed) FROM coveo.fct_product_performance"),

    # Rounded because float accumulation order differs between engines; the
    # point is agreement to a sane tolerance, not bit-identity.
    ("median-ish: mean capped duration (2dp)",
     "SELECT round(avg(duration_sec_capped), 2) FROM fct_session",
     "SELECT round(avg(duration_sec_capped)::numeric, 2) FROM coveo.fct_session"),

    ("earliest session start",
     "SELECT min(session_start)::VARCHAR FROM fct_session",
     "SELECT min(session_start)::text FROM coveo.fct_session"),

    ("latest session start",
     "SELECT max(session_start)::VARCHAR FROM fct_session",
     "SELECT max(session_start)::text FROM coveo.fct_session"),
]


def norm(v):
    """Compare numerically where possible so 53209 == Decimal('53209')."""
    if v is None:
        return None
    try:
        f = float(v)
        return round(f, 2)
    except (TypeError, ValueError):
        return str(v).strip()


def main() -> int:
    duck = duck_connect(read_only=True)
    try:
        pg = pg_connect()
    except psycopg2.OperationalError as e:
        raise SystemExit(f"PostgreSQL not reachable: {e}")

    cur = pg.cursor()
    print(f"{'Check':<40} {'DuckDB':>22} {'PostgreSQL':>22}   Result")
    print("-" * 96)

    failures = 0
    for label, dsql, psql_ in CHECKS:
        d = norm(duck.execute(dsql).fetchone()[0])
        cur.execute(psql_)
        p = norm(cur.fetchone()[0])
        ok = d == p
        if not ok:
            failures += 1

        def show(v):
            return f"{v:,.2f}".rstrip("0").rstrip(".") if isinstance(v, float) else str(v)

        print(f"{label:<40} {show(d):>22} {show(p):>22}   {'match' if ok else 'MISMATCH'}")

    print("-" * 96)
    cur.close()
    pg.close()
    duck.close()

    if failures:
        print(f"\n{failures} of {len(CHECKS)} checks MISMATCHED. "
              "The serving layer does not agree with the warehouse.")
        return 1

    print(f"\nAll {len(CHECKS)} checks match. PostgreSQL agrees with DuckDB.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
