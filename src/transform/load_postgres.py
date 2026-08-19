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


def pg_connect():
    return psycopg2.connect(
        host=config.PG["host"], port=config.PG["port"],
        dbname=config.PG["dbname"], user=config.PG["user"],
        password=config.PG["password"],
    )


def assert_no_hugeint(duck, duck_table: str, cols: list[str]) -> None:
    """Fail early and legibly on HUGEINT columns.

    DuckDB's sum() over an integer column returns HUGEINT (int128) so it cannot
    overflow. pandas has no int128 dtype, so such a column silently arrives as
    float64, and `to_csv` then writes "0.0" where PostgreSQL expects "0" for an
    INTEGER column.

    The failure surfaces as `invalid input syntax for type integer: "0.0"` from
    deep inside COPY, naming a row number rather than a cause. Checking the
    declared types up front turns that into a message that says what to do.
    """
    types = {n: t for n, t, *_ in duck.execute(f"DESCRIBE {duck_table}").fetchall()}
    offenders = [c for c in cols if "HUGEINT" in types.get(c, "").upper()]
    if offenders:
        raise SystemExit(
            f"\n{duck_table}: HUGEINT column(s) {offenders} cannot round-trip "
            f"through pandas.\n"
            f"They arrive as float64 and COPY will reject them as '0.0'.\n"
            f"Fix at the source: cast to ::BIGINT in the SQL that builds "
            f"{duck_table}, not here."
        )


# Below this size, dropping and rebuilding indexes costs more than it saves.
REBUILD_INDEXES_ABOVE = 1_000_000


def _capture_and_drop_indexes(cur, pg_table: str) -> list[str]:
    """Drop a table's indexes and unique/PK constraints, returning the DDL to
    restore them.

    Bulk loading into an indexed table makes PostgreSQL maintain every index
    per row. On fct_session the primary-key index over a 64-char hash is 587 MB
    of the 680 MB index total, and maintaining it dominates the load. Building
    the same index once, in bulk, from already-loaded data is far cheaper than
    inserting into it 4.9M times.

    CHECK constraints are deliberately left in place: they cost almost nothing
    per row and they are the point of having them -- a load that violates the
    funnel invariants should fail, not be waved through for speed.
    """
    ddl: list[str] = []

    cur.execute("""
        SELECT c.conname, pg_get_constraintdef(c.oid)
        FROM pg_constraint c
        WHERE c.conrelid = %s::regclass AND c.contype IN ('p', 'u')
    """, (pg_table,))
    constraints = cur.fetchall()

    cur.execute("""
        SELECT i.indexrelid::regclass::text, pg_get_indexdef(i.indexrelid)
        FROM pg_index i
        LEFT JOIN pg_constraint c ON c.conindid = i.indexrelid
        WHERE i.indrelid = %s::regclass AND c.conname IS NULL
    """, (pg_table,))
    indexes = cur.fetchall()

    for name, definition in indexes:
        ddl.append(definition)
        cur.execute(f"DROP INDEX {name}")
    for name, definition in constraints:
        ddl.append(f"ALTER TABLE {pg_table} ADD CONSTRAINT {name} {definition}")
        cur.execute(f"ALTER TABLE {pg_table} DROP CONSTRAINT {name}")

    return ddl


def copy_table(duck, pg, duck_table: str, pg_table: str, cols: list[str]) -> int:
    col_sql = ", ".join(cols)
    assert_no_hugeint(duck, duck_table, cols)
    total = duck.execute(f"SELECT count(*) FROM {duck_table}").fetchone()[0]
    print(f"  {duck_table} -> {pg_table}  ({total:,} rows)")

    # Route the data through a CSV file written by DuckDB, rather than through
    # pandas DataFrames. The intermediate file lands in COVEO_WORK_DIR (outside
    # the repo, outside cloud sync) and is deleted afterwards.
    #
    # ---- how this was arrived at, including the wrong turn -----------------
    #
    # The first version paged with LIMIT/OFFSET and serialised via pandas:
    # 518.5s for fct_session's 4.9M rows.
    #
    # Profiling attributed that to pandas -- to_csv 57.9%, DuckDB fetch 29.5%,
    # PostgreSQL COPY only 12.5% -- so the obvious fix was to bypass pandas.
    # Doing exactly that changed the total from 518.5s to 507.1s. Essentially
    # nothing.
    #
    # The profile was wrong, and wrong in an instructive way: it copied into a
    # TEMP table, which has no indexes. That made COPY look nearly free when on
    # the real table it was doing the bulk of the work -- maintaining a 587 MB
    # primary-key index over a 64-char hash, one row at a time, plus four
    # secondary indexes.
    #
    # Measuring the real table instead gave the actual answer, and both changes
    # together (native CSV export + drop/rebuild indexes around the load) took
    # fct_session from 518.5s to 156.7s: 26.5s writing CSV, 58.9s copying,
    # 41.2s rebuilding indexes in bulk.
    #
    # The lesson worth keeping: a benchmark that omits the indexes is not a
    # benchmark of the load.
    tmp_csv = config.INTERIM_DIR / f"_load_{duck_table}.csv"
    tmp_csv.parent.mkdir(parents=True, exist_ok=True)

    t0 = time.time()
    duck.execute(f"""
        COPY (SELECT {col_sql} FROM {duck_table})
        TO '{tmp_csv.as_posix()}'
        (FORMAT CSV, HEADER FALSE, NULLSTR '\\N')
    """)
    t_write = time.time() - t0

    rebuild = total > REBUILD_INDEXES_ABOVE
    t_copy = t_index = 0.0
    try:
        with pg.cursor() as cur:
            cur.execute(f"TRUNCATE {pg_table} CASCADE")

            index_ddl = _capture_and_drop_indexes(cur, pg_table) if rebuild else []

            t1 = time.time()
            with tmp_csv.open("r", encoding="utf-8", newline="") as fh:
                cur.copy_expert(
                    f"COPY {pg_table} ({col_sql}) "
                    f"FROM STDIN WITH (FORMAT csv, NULL '\\N')",
                    fh,
                )
            t_copy = time.time() - t1

            t2 = time.time()
            for statement in index_ddl:
                cur.execute(statement)
            t_index = time.time() - t2

            pg.commit()
            cur.execute(f"SELECT count(*) FROM {pg_table}")
            moved = cur.fetchone()[0]
    finally:
        tmp_csv.unlink(missing_ok=True)

    elapsed = time.time() - t0
    detail = f"{t_write:,.1f}s csv + {t_copy:,.1f}s copy"
    if rebuild:
        detail += f" + {t_index:,.1f}s indexes"
    print(f"    {moved:>10,} rows in {elapsed:,.1f}s "
          f"({moved / elapsed if elapsed else 0:,.0f} rows/s; {detail})")

    # A partial load is worse than a failed one: it looks like success.
    if moved != total:
        raise SystemExit(
            f"{duck_table}: loaded {moved:,} rows but source has {total:,}. "
            "Refusing to continue with a partial load."
        )
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
