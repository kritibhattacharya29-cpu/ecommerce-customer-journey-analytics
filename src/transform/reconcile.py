"""Row-count reconciliation between the raw and staging layers.

Every row that leaves the pipeline must be *accounted for*, not merely gone.
This is the cheapest and most effective guard against the most common silent
failure in analytics engineering: a join or filter that quietly drops rows,
producing a report that is internally consistent and simply wrong.

The identity checked here is:

    raw_browsing
      - rows with a NULL session or timestamp   (unusable)
      - redundant copies of exact duplicates    (tracking double-fire)
      - PDP pageviews paired with a detail      (documented double-fire)
    = stg_browsing

If those terms do not sum exactly, the transformation is dropping rows for a
reason nobody has named, and the build should not be trusted.

Usage:
    python -m src.transform.reconcile
"""
from __future__ import annotations

import sys

from src.ingest.build_duckdb import connect


def main() -> int:
    con = connect(read_only=True)

    raw = con.execute("SELECT count(*) FROM raw_browsing").fetchone()[0]

    unusable = con.execute("""
        SELECT count(*) FROM raw_browsing
        WHERE session_id_hash IS NULL OR server_timestamp_epoch_ms IS NULL
    """).fetchone()[0]

    # Redundant copies (not distinct fingerprints): sum(count - 1).
    dupes = con.execute("""
        WITH fp AS (
            SELECT hash(concat_ws('|',
                   coalesce(session_id_hash,                    '<N>'),
                   coalesce(event_type,                         '<N>'),
                   coalesce(product_action,                     '<N>'),
                   coalesce(product_sku_hash,                   '<N>'),
                   coalesce(server_timestamp_epoch_ms::VARCHAR, '<N>'),
                   coalesce(hashed_url,                         '<N>'))) AS row_fp
            FROM raw_browsing
            WHERE session_id_hash IS NOT NULL
              AND server_timestamp_epoch_ms IS NOT NULL
        )
        SELECT coalesce(sum(c - 1), 0) FROM (
            SELECT count(*) AS c FROM fp GROUP BY row_fp HAVING count(*) > 1
        )
    """).fetchone()[0]

    # PDP pageviews removed: distinct (session, ts, url) groups that contain
    # both a detail and a pageview, counted after deduplication.
    # Mirrors the staging logic exactly -- a windowed check over
    # (session, timestamp, url) rather than a correlated EXISTS, which at this
    # row count is the difference between one partitioned pass and a self-join
    # across the whole event stream.
    pdp = con.execute("""
        WITH deduped AS (
            SELECT DISTINCT session_id_hash, event_type, product_action,
                            product_sku_hash, server_timestamp_epoch_ms, hashed_url
            FROM raw_browsing
            WHERE session_id_hash IS NOT NULL
              AND server_timestamp_epoch_ms IS NOT NULL
        ),
        marked AS (
            SELECT
                event_type, hashed_url,
                max(CASE WHEN product_action = 'detail' THEN 1 ELSE 0 END) OVER (
                    PARTITION BY session_id_hash, server_timestamp_epoch_ms, hashed_url
                ) AS detail_at_same_instant
            FROM deduped
        )
        SELECT count(*) FROM marked
        WHERE event_type = 'pageview'
          AND hashed_url IS NOT NULL
          AND detail_at_same_instant = 1
    """).fetchone()[0]

    staged = con.execute("SELECT count(*) FROM stg_browsing").fetchone()[0]

    expected = raw - unusable - dupes - pdp
    delta = staged - expected

    print("Row-count reconciliation: raw_browsing -> stg_browsing\n")
    rows = [
        ("raw_browsing", raw, ""),
        ("- NULL session or timestamp", -unusable, "unusable"),
        ("- redundant duplicate copies", -dupes, "tracking double-fire"),
        ("- PDP pageviews paired with detail", -pdp, "documented double-fire"),
        ("= expected stg_browsing", expected, ""),
        ("  actual stg_browsing", staged, ""),
    ]
    for label, n, note in rows:
        print(f"  {label:<38} {n:>14,}  {note}")

    print()
    if delta == 0:
        print(f"  RECONCILED exactly ({staged:,} rows, "
              f"{100.0 * staged / raw:.2f}% of raw retained)")
        con.close()
        return 0

    print(f"  MISMATCH: {delta:+,} rows unaccounted for")
    print("  The transformation is dropping or duplicating rows for an unnamed "
          "reason. Do not trust downstream numbers until this is explained.")
    con.close()
    return 1


if __name__ == "__main__":
    sys.exit(main())
