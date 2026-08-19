"""Raw CSV -> DuckDB ingest.

Design notes (the interesting part):

1. The raw CSVs are treated as immutable. Nothing here writes back to them.
   File order is captured explicitly as `file_row_idx` *at ingest time*, because
   Coveo warn that "the order of the events in the file is not strictly
   chronological". Capturing file order lets us later measure how badly file
   order and timestamp order disagree -- a real property of front-end telemetry
   that is destroyed forever if you sort on the way in.

2. Dense vectors are split into separate tables. `query_vector`,
   `description_vector` and `image_vector` account for roughly 1.6 GB of the
   1.7 GB search file and most of the catalog file, but no funnel,
   search-conversion or cohort question needs them. Keeping them out of the hot
   analytical tables makes every downstream scan dramatically cheaper. They are
   ingested only when --with-vectors is passed, for the ML layer.

3. Empty strings are normalised to NULL. A pageview with no product action is
   semantically "no product involved", not "the empty product".

4. The SKU list columns arrive as Python reprs -- ['abc', 'def'] -- so they are
   parsed into VARCHAR[] once, here, rather than re-parsed by every consumer.
"""
from __future__ import annotations

import argparse
import time

import duckdb

from src import config

BROWSING_COLUMNS = {
    "session_id_hash": "VARCHAR",
    "event_type": "VARCHAR",
    "product_action": "VARCHAR",
    "product_sku_hash": "VARCHAR",
    "server_timestamp_epoch_ms": "BIGINT",
    "hashed_url": "VARCHAR",
}

SEARCH_COLUMNS = {
    "session_id_hash": "VARCHAR",
    "query_vector": "VARCHAR",
    "clicked_skus_hash": "VARCHAR",
    "product_skus_hash": "VARCHAR",
    "server_timestamp_epoch_ms": "BIGINT",
}

CATALOG_COLUMNS = {
    "product_sku_hash": "VARCHAR",
    "description_vector": "VARCHAR",
    "category_hash": "VARCHAR",
    "image_vector": "VARCHAR",
    "price_bucket": "VARCHAR",  # read as text so blanks survive as NULL
}


def _csv(path, columns: dict[str, str]) -> str:
    """Build a read_csv(...) expression with an explicit, pinned schema.

    An explicit schema means a surprise value in row 30,000,000 raises instead
    of silently re-typing a column halfway through a 6 GB scan.
    """
    cols = ", ".join("'{}': '{}'".format(k, v) for k, v in columns.items())
    return (
        "read_csv('{path}', header=true, columns={{{cols}}}, "
        "nullstr='', quote='\"', escape='\"')"
    ).format(path=path.as_posix(), cols=cols)


def _nn(col: str) -> str:
    """Normalise blank / whitespace-only strings to NULL."""
    return "nullif(trim({0}), '')".format(col)


def _parse_sku_list(col: str) -> str:
    """['a', 'b'] -> ['a','b'] as VARCHAR[]. Strips brackets, quotes, spaces."""
    cleaned = r"regexp_replace({0}, '[\[\]'' ]', '', 'g')".format(col)
    return (
        "CASE WHEN {nn} IS NULL THEN NULL "
        "ELSE str_split({cleaned}, ',') END"
    ).format(nn=_nn(col), cleaned=cleaned)


def _parse_vector(col: str) -> str:
    """[0.1, -0.2] -> DOUBLE[]."""
    cleaned = r"regexp_replace({0}, '[\[\] ]', '', 'g')".format(col)
    return (
        "CASE WHEN {nn} IS NULL THEN NULL "
        "ELSE cast(str_split({cleaned}, ',') AS DOUBLE[]) END"
    ).format(nn=_nn(col), cleaned=cleaned)


def connect(read_only: bool = False) -> duckdb.DuckDBPyConnection:
    config.ensure_dirs()
    con = duckdb.connect(str(config.DUCKDB_PATH), read_only=read_only)
    con.execute("SET preserve_insertion_order = true")
    # Sorting 36M wide rows does not fit in RAM on a typical laptop, so DuckDB
    # must spill. The limit is set BELOW available memory on purpose: letting
    # DuckDB spill deliberately to a known temp directory is far better than
    # letting it claim memory the OS then has to swap, which on an 8 GB machine
    # brings the whole system to a crawl. See config.DUCKDB_MEMORY_LIMIT.
    con.execute("SET memory_limit = '{0}'".format(config.DUCKDB_MEMORY_LIMIT))
    con.execute("SET temp_directory = '{0}'".format(config.INTERIM_DIR.as_posix()))
    return con


def _timed(con, label: str, sql: str) -> None:
    print("  [{0}] ...".format(label), end="", flush=True)
    t0 = time.time()
    con.execute(sql)
    print(" {0:,.1f}s".format(time.time() - t0))


def ingest_browsing(con) -> None:
    print("browsing_train.csv (6.0 GB)")
    con.execute("DROP TABLE IF EXISTS raw_browsing")
    _timed(con, "raw_browsing", """
        CREATE TABLE raw_browsing AS
        SELECT
            (row_number() OVER ()) - 1   AS file_row_idx,
            {sid}                        AS session_id_hash,
            {etype}                      AS event_type,
            {paction}                    AS product_action,
            {sku}                        AS product_sku_hash,
            server_timestamp_epoch_ms,
            {url}                        AS hashed_url
        FROM {src}
    """.format(
        sid=_nn("session_id_hash"),
        etype=_nn("event_type"),
        paction=_nn("product_action"),
        sku=_nn("product_sku_hash"),
        url=_nn("hashed_url"),
        src=_csv(config.BROWSING_CSV, BROWSING_COLUMNS),
    ))


def ingest_search(con, with_vectors: bool) -> None:
    print("search_train.csv (1.7 GB)")
    src = _csv(config.SEARCH_CSV, SEARCH_COLUMNS)

    con.execute("DROP TABLE IF EXISTS raw_search")
    _timed(con, "raw_search", """
        CREATE TABLE raw_search AS
        SELECT
            (row_number() OVER ()) - 1   AS file_row_idx,
            {sid}                        AS session_id_hash,
            server_timestamp_epoch_ms,
            {products}                   AS product_skus,
            {clicked}                    AS clicked_skus
        FROM {src}
    """.format(
        sid=_nn("session_id_hash"),
        products=_parse_sku_list("product_skus_hash"),
        clicked=_parse_sku_list("clicked_skus_hash"),
        src=src,
    ))

    if with_vectors:
        con.execute("DROP TABLE IF EXISTS raw_search_vectors")
        _timed(con, "raw_search_vectors", """
            CREATE TABLE raw_search_vectors AS
            SELECT
                (row_number() OVER ()) - 1 AS file_row_idx,
                {sid}                      AS session_id_hash,
                server_timestamp_epoch_ms,
                {qv}                       AS query_vector
            FROM {src}
        """.format(sid=_nn("session_id_hash"),
                   qv=_parse_vector("query_vector"),
                   src=src))


def ingest_catalog(con, with_vectors: bool) -> None:
    print("sku_to_content.csv (71 MB)")
    src = _csv(config.CATALOG_CSV, CATALOG_COLUMNS)

    con.execute("DROP TABLE IF EXISTS raw_catalog")
    # category_hash is a '/'-separated 3-level hierarchy; split it once here so
    # every downstream category question is a column lookup, not a string parse.
    _timed(con, "raw_catalog", """
        CREATE TABLE raw_catalog AS
        WITH src AS (
            SELECT
                {sku}                             AS product_sku_hash,
                {cat}                             AS category_hash,
                try_cast({price} AS DOUBLE)       AS price_bucket
            FROM {src}
        )
        SELECT
            product_sku_hash,
            category_hash,
            str_split(category_hash, '/')                       AS category_path,
            try_cast(list_extract(str_split(category_hash,'/'),1) AS VARCHAR) AS category_lvl1,
            try_cast(list_extract(str_split(category_hash,'/'),2) AS VARCHAR) AS category_lvl2,
            try_cast(list_extract(str_split(category_hash,'/'),3) AS VARCHAR) AS category_lvl3,
            len(str_split(category_hash, '/'))                  AS category_depth,
            price_bucket
        FROM src
    """.format(sku=_nn("product_sku_hash"),
               cat=_nn("category_hash"),
               price=_nn("price_bucket"),
               src=src))

    if with_vectors:
        con.execute("DROP TABLE IF EXISTS raw_catalog_vectors")
        _timed(con, "raw_catalog_vectors", """
            CREATE TABLE raw_catalog_vectors AS
            SELECT
                {sku}   AS product_sku_hash,
                {dv}    AS description_vector,
                {iv}    AS image_vector
            FROM {src}
        """.format(sku=_nn("product_sku_hash"),
                   dv=_parse_vector("description_vector"),
                   iv=_parse_vector("image_vector"),
                   src=src))


def main() -> None:
    ap = argparse.ArgumentParser(description="Ingest the Coveo CSVs into DuckDB.")
    ap.add_argument("--with-vectors", action="store_true",
                    help="Also ingest query/description/image vectors (ML layer). Slower + larger.")
    ap.add_argument("--only", choices=["browsing", "search", "catalog"],
                    help="Ingest a single source.")
    args = ap.parse_args()

    config.verify_raw_data()
    con = connect()
    print("DuckDB -> {0}\n".format(config.DUCKDB_PATH))

    t0 = time.time()
    if args.only in (None, "catalog"):
        ingest_catalog(con, args.with_vectors)
    if args.only in (None, "search"):
        ingest_search(con, args.with_vectors)
    if args.only in (None, "browsing"):
        ingest_browsing(con)

    print("\nTotal {0:,.1f}s\n".format(time.time() - t0))
    print("Tables:")
    for name, n in con.execute(
        "SELECT table_name, estimated_size FROM duckdb_tables() ORDER BY table_name"
    ).fetchall():
        print("  {0:<24} ~{1:>14,} rows".format(name, int(n)))
    con.close()


if __name__ == "__main__":
    main()
