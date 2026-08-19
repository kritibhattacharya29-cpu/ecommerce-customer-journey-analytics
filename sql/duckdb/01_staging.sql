-- ===========================================================================
-- STAGING LAYER
--
-- Turns the raw event dump into a clean, sequenced event stream. Every
-- transformation here is a decision that the data-quality assessment forced,
-- and each one is reversible because the raw layer is never modified.
--
-- Run after: src/ingest/build_duckdb.py
-- ===========================================================================


-- ---------------------------------------------------------------------------
-- stg_browsing — deduplicated, chronologically sequenced events
--
-- Three problems are resolved here, in this order:
--
--   1. EXACT DUPLICATES. A tracking pixel firing twice produces byte-identical
--      rows. Collapsed to one. Raw layer keeps the evidence.
--
--   2. PDP DOUBLE-FIRE. Coveo warn a product detail page "may generate both a
--      detail and a pageview event". Both land on the same
--      (session, timestamp, url). Counting them separately would inflate
--      product views against page views. The `detail` is kept as the more
--      specific of the two; the paired `pageview` is dropped.
--
--   3. ORDERING. File order is not chronological, and timestamps collide, so
--      neither alone is a total order. Sequence is
--          (server_timestamp_epoch_ms, file_row_idx)
--      which is deterministic and stable across runs — important, because a
--      funnel built on a non-deterministic sequence gives different numbers
--      every time you run it.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE TABLE stg_browsing AS
WITH fingerprinted AS (
    -- (1a) Reduce each row to a single 64-bit fingerprint.
    --
    -- Grouping directly on the six source columns means a ~200-byte key (three
    -- 64-char hashes) across 36M rows -- roughly 7 GB of grouping keys, which
    -- on an 8 GB machine spills catastrophically. Hashing to a BIGINT first
    -- makes the grouping key 8 bytes.
    --
    -- A sentinel is used rather than bare concat_ws because concat_ws skips
    -- NULLs, which would let ('a', NULL, 'b') collide with ('a', 'b', NULL).
    SELECT
        *,
        hash(concat_ws('|',
             coalesce(session_id_hash,                    '<N>'),
             coalesce(event_type,                         '<N>'),
             coalesce(product_action,                     '<N>'),
             coalesce(product_sku_hash,                   '<N>'),
             coalesce(server_timestamp_epoch_ms::VARCHAR, '<N>'),
             coalesce(hashed_url,                         '<N>'))) AS row_fp
    FROM raw_browsing
    WHERE session_id_hash IS NOT NULL
      AND server_timestamp_epoch_ms IS NOT NULL
),
colliding AS (
    -- (1b) Only ~1,800 rows in 36M are true duplicates, so grouping the whole
    -- table to find them is enormous work for a tiny result. Group on the
    -- BIGINT fingerprint instead: cheap, and it narrows the candidates to a
    -- handful of rows.
    SELECT row_fp FROM fingerprinted GROUP BY row_fp HAVING count(*) > 1
),
unique_rows AS (
    -- A unique fingerprint proves a unique row -- pass straight through.
    SELECT
        session_id_hash, event_type, product_action, product_sku_hash,
        server_timestamp_epoch_ms, hashed_url, file_row_idx,
        1 AS raw_row_count
    FROM fingerprinted
    WHERE row_fp NOT IN (SELECT row_fp FROM colliding)
),
resolved_rows AS (
    -- Only the tiny colliding subset pays for exact full-column grouping,
    -- which also guards against a (vanishingly unlikely) hash collision
    -- between two genuinely different rows.
    SELECT
        session_id_hash, event_type, product_action, product_sku_hash,
        server_timestamp_epoch_ms, hashed_url,
        min(file_row_idx) AS file_row_idx,
        count(*)          AS raw_row_count   -- audit trail: how many collapsed
    FROM fingerprinted
    WHERE row_fp IN (SELECT row_fp FROM colliding)
    GROUP BY ALL
),
deduped AS (
    SELECT * FROM unique_rows
    UNION ALL
    SELECT * FROM resolved_rows
),
pdp_marked AS (
    -- (2) flag the pageview half of a detail+pageview pair on one timestamp.
    --
    -- Expressed as a window function rather than a correlated EXISTS: at 36M
    -- rows a per-row lookup back into the same CTE is the difference between
    -- one partitioned pass and a self-join over the whole event stream.
    SELECT
        d.*,
        max(CASE WHEN d.product_action = 'detail' THEN 1 ELSE 0 END) OVER (
            PARTITION BY d.session_id_hash,
                         d.server_timestamp_epoch_ms,
                         d.hashed_url
        ) AS detail_at_same_instant
    FROM deduped d
)
SELECT
    session_id_hash,
    event_type,
    product_action,
    product_sku_hash,
    hashed_url,
    server_timestamp_epoch_ms,
    to_timestamp(server_timestamp_epoch_ms / 1000)          AS event_ts,

    -- (3) deterministic within-session sequence
    row_number() OVER (
        PARTITION BY session_id_hash
        ORDER BY server_timestamp_epoch_ms, file_row_idx
    )                                                        AS event_seq,

    -- Only intra-week seasonality is interpretable: Coveo shifted all
    -- timestamps by an undisclosed number of weeks, preserving weekly pattern.
    dayofweek(to_timestamp(server_timestamp_epoch_ms / 1000)) AS day_of_week,
    hour(to_timestamp(server_timestamp_epoch_ms / 1000))      AS hour_of_day,

    -- Canonical funnel stage. 'pageview' is the catch-all for non-product
    -- browsing; NULL product_action means no product was involved.
    CASE
        WHEN product_action = 'purchase' THEN 'purchase'
        WHEN product_action = 'add'      THEN 'add_to_cart'
        WHEN product_action = 'remove'   THEN 'remove_from_cart'
        WHEN product_action = 'detail'   THEN 'product_detail'
        ELSE 'pageview'
    END                                                       AS funnel_stage,

    raw_row_count,
    file_row_idx
FROM pdp_marked
WHERE NOT (event_type = 'pageview'
           AND hashed_url IS NOT NULL
           AND detail_at_same_instant = 1);


-- ---------------------------------------------------------------------------
-- stg_search — one row per query, with impression/click sets intact
--
-- `n_results = 0` is the zero-result search: a direct, addressable revenue
-- leak and one of the few metrics here that maps to an obvious merchandising
-- action.
--
-- `clicked_not_in_results` is a pure telemetry artefact — a click logged
-- against a SKU absent from the recorded result set, which happens when
-- results are re-ranked or paginated between impression and click. Such clicks
-- have no denominator to belong to and are excluded from CTR.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE TABLE stg_search AS
SELECT
    session_id_hash,
    server_timestamp_epoch_ms,
    to_timestamp(server_timestamp_epoch_ms / 1000)  AS event_ts,
    coalesce(product_skus, [])                      AS result_skus,
    coalesce(clicked_skus, [])                      AS clicked_skus,
    len(coalesce(product_skus, []))                 AS n_results,
    len(coalesce(clicked_skus, []))                 AS n_clicks,
    len(coalesce(product_skus, [])) = 0             AS is_zero_result,

    -- clicks that reference a SKU never returned
    len(list_filter(coalesce(clicked_skus, []),
                    x -> NOT list_contains(coalesce(product_skus, []), x)))
                                                    AS n_phantom_clicks,

    -- clicks that are genuinely attributable to this result set
    list_filter(coalesce(clicked_skus, []),
                x -> list_contains(coalesce(product_skus, []), x))
                                                    AS valid_clicked_skus,
    file_row_idx
FROM raw_search
WHERE session_id_hash IS NOT NULL
  AND server_timestamp_epoch_ms IS NOT NULL;


-- ---------------------------------------------------------------------------
-- dim_product — catalog with EXPLICIT unknown members
--
-- Roughly half of `sku_to_content.csv` is SKU-only with no category or price,
-- and some browsed SKUs have no catalog row at all. Dropping either would bias
-- every segmented metric toward well-maintained products — the classic silent
-- error where a category report looks fine and is simply missing half the
-- business. Unknowns are therefore first-class members.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE TABLE dim_product AS
WITH browsed AS (
    SELECT DISTINCT product_sku_hash FROM stg_browsing
    WHERE product_sku_hash IS NOT NULL
),
all_skus AS (
    SELECT product_sku_hash FROM browsed
    UNION
    SELECT product_sku_hash FROM raw_catalog
)
SELECT
    s.product_sku_hash,
    c.product_sku_hash IS NOT NULL                       AS in_catalog,
    coalesce(c.category_lvl1, '(unknown)')               AS category_lvl1,
    coalesce(c.category_lvl2, '(unknown)')               AS category_lvl2,
    coalesce(c.category_lvl3, '(unknown)')               AS category_lvl3,
    c.category_hash,
    coalesce(c.category_depth, 0)                        AS category_depth,
    c.price_bucket,
    c.price_bucket IS NOT NULL                           AS has_price,
    -- ordinal grouping for readable reporting; buckets are deciles 1-10
    CASE
        WHEN c.price_bucket IS NULL      THEN '(unknown)'
        WHEN c.price_bucket <= 3         THEN 'low (1-3)'
        WHEN c.price_bucket <= 7         THEN 'mid (4-7)'
        ELSE                                  'high (8-10)'
    END                                                  AS price_tier
FROM all_skus s
LEFT JOIN raw_catalog c USING (product_sku_hash);
