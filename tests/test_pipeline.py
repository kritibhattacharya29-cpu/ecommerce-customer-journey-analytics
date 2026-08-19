"""End-to-end pipeline test against synthetic fixtures.

Runs the real ingest and transform code — not a reimplementation of it — over
generated data that contains every pathology measured in the real dataset, and
asserts the pipeline handles each one correctly.

This is what makes the project testable at all: Coveo's licence forbids
committing real rows, so without synthetic fixtures there would be no way for
anyone else to verify the logic. Run with:

    python tests/test_pipeline.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PY = sys.executable


class Failure(Exception):
    pass


def check(label: str, actual, expected=None, predicate=None) -> None:
    if predicate is not None:
        ok = predicate(actual)
        detail = f"got {actual!r}"
    else:
        ok = actual == expected
        detail = f"got {actual!r}, expected {expected!r}"
    print(f"  {'PASS' if ok else 'FAIL'}  {label:<58} {detail}")
    if not ok:
        raise Failure(label)


def run(cmd: list[str], env: dict) -> None:
    result = subprocess.run(cmd, cwd=REPO, env=env,
                            capture_output=True, text=True)
    if result.returncode != 0:
        print(result.stdout[-3000:])
        print(result.stderr[-3000:])
        raise Failure(" ".join(cmd))


def main() -> int:
    import duckdb

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        raw, work = tmp / "raw", tmp / "work"

        env = dict(os.environ)
        env["COVEO_RAW_DIR"] = raw.as_posix()
        env["COVEO_WORK_DIR"] = work.as_posix()
        env["DUCKDB_PATH"] = "test.duckdb"
        env["PYTHONPATH"] = str(REPO)

        print("Generating synthetic fixtures")
        run([PY, "tests/fixtures/make_synthetic.py", "--out", str(raw)], env)

        print("\nRunning ingest")
        run([PY, "-m", "src.ingest.build_duckdb"], env)

        print("\nRunning transform")
        run([PY, "-m", "src.transform.build_model"], env)

        con = duckdb.connect(str(work / "test.duckdb"), read_only=True)
        q = lambda sql: con.execute(sql).fetchone()[0]  # noqa: E731

        print("\n--- Ordering: sequence is rebuilt, not trusted from file order ---")
        # Every session's event_seq must be monotonic in timestamp.
        check("no session has event_seq disagreeing with timestamp order",
              q("""SELECT count(*) FROM (
                     SELECT session_id_hash FROM (
                       SELECT session_id_hash, event_seq, server_timestamp_epoch_ms,
                              lag(server_timestamp_epoch_ms) OVER (
                                PARTITION BY session_id_hash ORDER BY event_seq) AS prev
                       FROM stg_browsing)
                     WHERE prev IS NOT NULL AND server_timestamp_epoch_ms < prev)"""), 0)

        check("event_seq is contiguous from 1 within each session",
              q("""SELECT count(*) FROM (
                     SELECT session_id_hash FROM stg_browsing
                     GROUP BY session_id_hash
                     HAVING min(event_seq) <> 1
                         OR max(event_seq) <> count(*))"""), 0)

        print("\n--- Deduplication ---")
        check("no exact duplicate rows survive staging",
              q("""SELECT count(*) FROM (
                     SELECT session_id_hash, event_type, product_action,
                            product_sku_hash, server_timestamp_epoch_ms, hashed_url
                     FROM stg_browsing
                     GROUP BY ALL HAVING count(*) > 1)"""), 0)

        check("duplicates were actually present in raw (fixture is exercising this)",
              q("""SELECT count(*) FROM (
                     SELECT 1 FROM raw_browsing
                     GROUP BY session_id_hash, event_type, product_action,
                              product_sku_hash, server_timestamp_epoch_ms, hashed_url
                     HAVING count(*) > 1)"""),
              predicate=lambda v: v > 0)

        print("\n--- PDP double-fire collapsed ---")
        check("no (session, ts, url) has both a detail and a pageview left",
              q("""SELECT count(*) FROM (
                     SELECT session_id_hash, server_timestamp_epoch_ms, hashed_url
                     FROM stg_browsing WHERE hashed_url IS NOT NULL
                     GROUP BY 1,2,3
                     HAVING count(*) FILTER (WHERE product_action='detail') > 0
                        AND count(*) FILTER (WHERE event_type='pageview') > 0)"""), 0)

        check("detail events were not themselves dropped",
              q("SELECT count(*) FROM stg_browsing WHERE product_action='detail'"),
              q("""SELECT count(*) FROM (
                     SELECT DISTINCT session_id_hash, product_sku_hash,
                            server_timestamp_epoch_ms, hashed_url
                     FROM raw_browsing WHERE product_action='detail')"""))

        print("\n--- Unknown members are modelled, not dropped ---")
        check("SKUs browsed but absent from catalog appear in dim_product",
              q("SELECT count(*) FROM dim_product WHERE NOT in_catalog"),
              predicate=lambda v: v > 0)

        check("every browsed SKU resolves in dim_product",
              q("""SELECT count(*) FROM (
                     SELECT DISTINCT product_sku_hash FROM stg_browsing
                     WHERE product_sku_hash IS NOT NULL) b
                   LEFT JOIN dim_product d USING (product_sku_hash)
                   WHERE d.product_sku_hash IS NULL"""), 0)

        check("unknown price SKUs get an explicit tier, never NULL",
              q("SELECT count(*) FROM dim_product WHERE price_tier IS NULL"), 0)

        print("\n--- Dual funnel definitions ---")
        check("cross-session purchases (no add) exist in the fixture",
              q("SELECT count(*) FROM fct_session WHERE purchase_without_add"),
              predicate=lambda v: v > 0)

        check("step-attained purchases >= strict-sequence purchases",
              q("""SELECT (count(*) FILTER (WHERE reached_purchase))
                        - (count(*) FILTER (WHERE strict_purchase))
                   FROM fct_session"""),
              predicate=lambda v: v >= 0)

        check("every strict_purchase session also reached_purchase",
              q("""SELECT count(*) FROM fct_session
                   WHERE strict_purchase AND NOT reached_purchase"""), 0)

        print("\n--- Session grain integrity ---")
        check("fct_session has one row per session in stg_browsing",
              q("SELECT count(*) FROM fct_session"),
              q("SELECT count(DISTINCT session_id_hash) FROM stg_browsing"))

        check("session_id is unique in fct_session",
              q("""SELECT count(*) FROM (SELECT session_id_hash FROM fct_session
                   GROUP BY 1 HAVING count(*) > 1)"""), 0)

        check("duration cap is applied",
              q("SELECT count(*) FROM fct_session WHERE duration_sec_capped > 1800"), 0)

        check("uncapped duration retained for audit",
              q("SELECT count(*) FROM fct_session WHERE duration_sec_raw > 1800"),
              predicate=lambda v: v > 0)

        print("\n--- ML features: leakage guards ---")
        # The whole point of ml_cart_sessions is that features stop at the add.
        # These assertions are the regression test for that guarantee.
        check("one row per session that added to cart",
              q("SELECT count(*) FROM ml_cart_sessions"),
              q("SELECT count(*) FROM fct_session WHERE reached_add"))

        check("pre-add event count never exceeds the add's own position",
              q("""SELECT count(*) FROM ml_cart_sessions
                   WHERE pre_n_events > pre_add_position"""), 0)

        check("no negative dwell time (add cannot precede session start)",
              q("SELECT count(*) FROM ml_cart_sessions WHERE pre_sec_to_add < 0"), 0)

        # If any pre_* feature counted post-add events, sessions with a purchase
        # would show inflated counts. Compare against a directly-computed
        # truncated count as ground truth.
        check("pre_n_product_views matches an independent truncated count",
              q("""WITH fa AS (
                     SELECT session_id_hash, min(event_seq) AS add_seq
                     FROM stg_browsing WHERE funnel_stage='add_to_cart' GROUP BY 1),
                   truth AS (
                     SELECT f.session_id_hash,
                            count(*) FILTER (WHERE b.funnel_stage='product_detail') AS n
                     FROM fa f JOIN stg_browsing b
                       ON b.session_id_hash=f.session_id_hash AND b.event_seq<=f.add_seq
                     GROUP BY 1)
                   SELECT count(*) FROM ml_cart_sessions m
                   JOIN truth t USING (session_id_hash)
                   WHERE m.pre_n_product_views <> t.n"""), 0)

        check("label counts only purchases strictly after the add",
              q("""WITH fa AS (
                     SELECT session_id_hash, min(event_seq) AS add_seq
                     FROM stg_browsing WHERE funnel_stage='add_to_cart' GROUP BY 1),
                   truth AS (
                     SELECT f.session_id_hash,
                            max(CASE WHEN b.funnel_stage='purchase'
                                      AND b.event_seq > f.add_seq THEN 1 ELSE 0 END) AS y
                     FROM fa f JOIN stg_browsing b
                       ON b.session_id_hash=f.session_id_hash
                     GROUP BY 1)
                   SELECT count(*) FROM ml_cart_sessions m
                   JOIN truth t USING (session_id_hash)
                   WHERE m.purchased <> t.y"""), 0)

        # The accounting-identity trap, as a standing check: no subset of the
        # feature columns may sum to a total that reconstructs the label.
        check("ml_cart_sessions exposes no session-total column",
              q("""SELECT count(*) FROM duckdb_columns()
                   WHERE table_name='ml_cart_sessions'
                     AND column_name IN ('n_events','n_purchases','duration_sec_raw',
                                         'duration_sec_capped','reached_purchase')"""), 0)

        print("\n--- Search ---")
        check("phantom clicks are excluded from valid_clicked_skus",
              q("""SELECT count(*) FROM stg_search
                   WHERE len(list_filter(valid_clicked_skus,
                             x -> NOT list_contains(result_skus, x))) > 0"""), 0)

        check("phantom clicks exist in the fixture",
              q("SELECT sum(n_phantom_clicks) FROM stg_search"),
              predicate=lambda v: v and v > 0)

        check("zero-result searches are flagged",
              q("SELECT count(*) FROM stg_search WHERE is_zero_result"),
              predicate=lambda v: v > 0)

        con.close()

    print("\nAll checks passed.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Failure as e:
        print(f"\nFAILED: {e}")
        sys.exit(1)
