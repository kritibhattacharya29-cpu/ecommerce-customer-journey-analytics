-- ===========================================================================
-- MART LAYER — session grain
--
-- Run after: sql/duckdb/01_staging.sql
-- ===========================================================================


-- ---------------------------------------------------------------------------
-- fct_session — one row per shopping session
--
-- This is the spine of the whole project. Funnel, abandonment, time-to-purchase
-- and the purchase-intent model all read from here.
--
-- The critical design choice is that BOTH funnel definitions are materialised
-- side by side:
--
--   reached_*  — STEP-ATTAINED. Did the session ever reach this stage, in any
--                order? Counts cross-session carts, so it does not understate
--                revenue.
--
--   strict_*   — STRICT-SEQUENCE. Did the stages occur in the canonical order
--                detail -> add -> purchase? The textbook definition.
--
-- The gap between them is not noise to be reconciled away — it is the measured
-- rate of cross-session and non-linear shopping behaviour, and it is a finding
-- in its own right. Reporting only one of these numbers is the mistake this
-- model exists to prevent.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE TABLE fct_session AS
WITH ev AS (
    SELECT
        session_id_hash,
        min(server_timestamp_epoch_ms)                              AS first_ts,
        max(server_timestamp_epoch_ms)                              AS last_ts,
        min(event_ts)                                               AS session_start,
        max(event_ts)                                               AS session_end,
        count(*)                                                    AS n_events,

        count(*) FILTER (WHERE funnel_stage = 'pageview')           AS n_pageviews,
        count(*) FILTER (WHERE funnel_stage = 'product_detail')     AS n_product_views,
        count(*) FILTER (WHERE funnel_stage = 'add_to_cart')        AS n_adds,
        count(*) FILTER (WHERE funnel_stage = 'remove_from_cart')   AS n_removes,
        count(*) FILTER (WHERE funnel_stage = 'purchase')           AS n_purchases,

        count(DISTINCT product_sku_hash)
            FILTER (WHERE funnel_stage = 'product_detail')          AS n_skus_viewed,
        count(DISTINCT product_sku_hash)
            FILTER (WHERE funnel_stage = 'add_to_cart')             AS n_skus_added,
        count(DISTINCT product_sku_hash)
            FILTER (WHERE funnel_stage = 'purchase')                AS n_skus_purchased,
        count(DISTINCT hashed_url)                                  AS n_unique_pages,

        -- first arrival at each stage, for strict-sequence evaluation
        min(event_seq) FILTER (WHERE funnel_stage = 'product_detail') AS first_detail_seq,
        min(event_seq) FILTER (WHERE funnel_stage = 'add_to_cart')    AS first_add_seq,
        min(event_seq) FILTER (WHERE funnel_stage = 'purchase')       AS first_purchase_seq,

        -- elapsed-time milestones
        min(server_timestamp_epoch_ms)
            FILTER (WHERE funnel_stage = 'add_to_cart')             AS first_add_ts,
        min(server_timestamp_epoch_ms)
            FILTER (WHERE funnel_stage = 'purchase')                AS first_purchase_ts,

        -- entry context: only intra-week seasonality is interpretable
        min(day_of_week)                                            AS day_of_week,
        min(hour_of_day)                                            AS hour_of_day,

        max(raw_row_count) > 1                                      AS had_duplicate_events
    FROM stg_browsing
    GROUP BY session_id_hash
),
srch AS (
    -- The ::BIGINT casts are load-bearing, not cosmetic. DuckDB's sum() on an
    -- integer column returns HUGEINT (int128) to be overflow-safe. pandas has
    -- no int128, so a HUGEINT column silently arrives as float64, and
    -- to_csv then writes "0.0" -- which PostgreSQL rejects for an INTEGER
    -- column. These counts are small by construction, so the width is pure
    -- overhead; casting here fixes the type at the source rather than papering
    -- over it in the loader.
    SELECT
        session_id_hash,
        count(*)                                        AS n_searches,
        count(*) FILTER (WHERE is_zero_result)          AS n_zero_result_searches,
        sum(n_clicks)::BIGINT                           AS n_search_clicks,
        sum(len(valid_clicked_skus))::BIGINT            AS n_valid_search_clicks,
        sum(n_results)::BIGINT                          AS n_search_impressions
    FROM stg_search
    GROUP BY session_id_hash
)
SELECT
    e.session_id_hash,
    e.session_start,
    e.session_end,
    e.day_of_week,
    e.hour_of_day,

    -- Duration is capped for reporting because some sessions contain gaps that
    -- violate Coveo's own 30-minute session rule (clock skew, background tabs,
    -- server-side stitching). The uncapped value is kept alongside so the cap
    -- is visible and auditable rather than baked in silently.
    (e.last_ts - e.first_ts) / 1000.0                       AS duration_sec_raw,
    least((e.last_ts - e.first_ts) / 1000.0, 1800.0)        AS duration_sec_capped,

    e.n_events,
    e.n_pageviews,
    e.n_product_views,
    e.n_adds,
    e.n_removes,
    e.n_purchases,
    e.n_skus_viewed,
    e.n_skus_added,
    e.n_skus_purchased,
    e.n_unique_pages,

    -- ---- funnel: step-attained -------------------------------------------
    TRUE                                    AS reached_session,
    e.n_product_views > 0                   AS reached_detail,
    e.n_adds > 0                            AS reached_add,
    e.n_purchases > 0                       AS reached_purchase,

    -- ---- funnel: strict sequence -----------------------------------------
    e.first_detail_seq IS NOT NULL          AS strict_detail,
    (e.first_detail_seq IS NOT NULL
     AND e.first_add_seq > e.first_detail_seq)              AS strict_add,
    (e.first_detail_seq IS NOT NULL
     AND e.first_add_seq > e.first_detail_seq
     AND e.first_purchase_seq > e.first_add_seq)            AS strict_purchase,

    -- the discrepancy itself, flagged per session
    (e.n_purchases > 0 AND e.n_adds = 0)    AS purchase_without_add,

    -- ---- behavioural outcomes --------------------------------------------
    e.n_events = 1                          AS is_bounce,
    (e.n_adds > 0 AND e.n_purchases = 0)    AS cart_abandoned,
    (e.n_adds > 0 AND e.n_removes >= e.n_adds
                  AND e.n_purchases = 0)    AS cart_emptied,

    -- ---- timing ----------------------------------------------------------
    (e.first_add_ts - e.first_ts) / 1000.0              AS sec_to_first_add,
    (e.first_purchase_ts - e.first_ts) / 1000.0         AS sec_to_purchase,
    (e.first_purchase_ts - e.first_add_ts) / 1000.0     AS sec_add_to_purchase,

    -- ---- search ----------------------------------------------------------
    coalesce(s.n_searches, 0)               AS n_searches,
    coalesce(s.n_zero_result_searches, 0)   AS n_zero_result_searches,
    coalesce(s.n_search_clicks, 0)          AS n_search_clicks,
    coalesce(s.n_valid_search_clicks, 0)    AS n_valid_search_clicks,
    coalesce(s.n_search_impressions, 0)     AS n_search_impressions,
    coalesce(s.n_searches, 0) > 0           AS used_search,

    e.had_duplicate_events
FROM ev e
LEFT JOIN srch s USING (session_id_hash);


-- ---------------------------------------------------------------------------
-- fct_product_performance — per-SKU funnel
--
-- Joined through dim_product so SKUs with no catalog row still appear, under
-- their '(unknown)' category and price members.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE TABLE fct_product_performance AS
WITH per_sku AS (
    SELECT
        product_sku_hash,
        count(DISTINCT session_id_hash)
            FILTER (WHERE funnel_stage = 'product_detail')  AS sessions_viewed,
        count(DISTINCT session_id_hash)
            FILTER (WHERE funnel_stage = 'add_to_cart')     AS sessions_added,
        count(DISTINCT session_id_hash)
            FILTER (WHERE funnel_stage = 'purchase')        AS sessions_purchased,
        count(DISTINCT session_id_hash)
            FILTER (WHERE funnel_stage = 'remove_from_cart') AS sessions_removed,
        count(*) FILTER (WHERE funnel_stage = 'product_detail') AS n_views,
        count(*) FILTER (WHERE funnel_stage = 'add_to_cart')    AS n_adds,
        count(*) FILTER (WHERE funnel_stage = 'purchase')       AS n_purchases
    FROM stg_browsing
    WHERE product_sku_hash IS NOT NULL
    GROUP BY product_sku_hash
)
SELECT
    p.*,
    d.in_catalog,
    d.category_lvl1,
    d.category_lvl2,
    d.price_bucket,
    d.price_tier,
    -- Rates are NULL, not 0, when the denominator is 0. A SKU nobody viewed
    -- has an undefined view->add rate; reporting 0% would make it look like a
    -- failing product rather than an unseen one.
    CASE WHEN p.sessions_viewed > 0
         THEN p.sessions_added::DOUBLE / p.sessions_viewed END     AS view_to_add_rate,
    CASE WHEN p.sessions_added > 0
         THEN p.sessions_purchased::DOUBLE / p.sessions_added END  AS add_to_purchase_rate,
    CASE WHEN p.sessions_viewed > 0
         THEN p.sessions_purchased::DOUBLE / p.sessions_viewed END AS view_to_purchase_rate
FROM per_sku p
LEFT JOIN dim_product d USING (product_sku_hash);
