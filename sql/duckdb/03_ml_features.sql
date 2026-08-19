-- ===========================================================================
-- LAYER 5 — PURCHASE-INTENT FEATURES
--
-- Task: given a session that has just added something to the cart, will it go
-- on to purchase? This is the cart-abandonment problem, and it is the framing
-- the original SIGIR challenge used, because it is the one with a decision
-- attached — an abandoning cart can be intervened on in real time.
--
-- ---------------------------------------------------------------------------
-- THE LEAKAGE PROBLEM, AND WHY THIS TABLE EXISTS
-- ---------------------------------------------------------------------------
-- fct_session is the obvious place to get features from, and it is completely
-- unusable for this. Every one of its counters — n_adds, n_removes, n_events,
-- duration_sec, sec_to_purchase — is aggregated over the ENTIRE session,
-- including everything that happened AFTER the moment we are pretending to
-- predict from. A model trained on it would score ~0.99 AUC and be worthless,
-- because `n_purchases > 0` is sitting in the feature set in various disguises.
--
-- The subtle versions matter more than the obvious ones. `duration_sec` leaks:
-- purchasing sessions are longer, but that length is *caused* by the purchase
-- that has not happened yet at prediction time. `n_events` leaks for the same
-- reason. These are the ones that survive a careless review and quietly
-- inflate every metric.
--
-- So every feature here is computed strictly from events at or before
-- `add_seq` — the sequence position of the FIRST add-to-cart. That is the
-- prediction point. Nothing after it may inform a feature; it may only inform
-- the label.
--
-- Run after: sql/duckdb/02_marts.sql
-- ===========================================================================


CREATE OR REPLACE TABLE ml_cart_sessions AS

WITH first_add AS (
    -- The prediction point: the first add-to-cart in each session.
    SELECT
        session_id_hash,
        event_seq                 AS add_seq,
        server_timestamp_epoch_ms AS add_ts,
        product_sku_hash          AS added_sku
    FROM (
        SELECT
            session_id_hash, event_seq, server_timestamp_epoch_ms, product_sku_hash,
            row_number() OVER (PARTITION BY session_id_hash ORDER BY event_seq) AS rn
        FROM stg_browsing
        WHERE funnel_stage = 'add_to_cart'
    )
    WHERE rn = 1
),

-- ---------------------------------------------------------------------------
-- FEATURES: strictly events at or before the prediction point.
-- ---------------------------------------------------------------------------
pre AS (
    SELECT
        f.session_id_hash,
        f.add_seq,
        f.add_ts,
        f.added_sku,

        count(*)                                                   AS pre_n_events,
        count(*) FILTER (WHERE b.funnel_stage = 'pageview')        AS pre_n_pageviews,
        count(*) FILTER (WHERE b.funnel_stage = 'product_detail')  AS pre_n_product_views,
        count(*) FILTER (WHERE b.funnel_stage = 'remove_from_cart') AS pre_n_removes,

        count(DISTINCT b.product_sku_hash)
            FILTER (WHERE b.funnel_stage = 'product_detail')       AS pre_n_skus_viewed,
        count(DISTINCT b.hashed_url)                               AS pre_n_unique_pages,

        -- Did the shopper look at this specific product before adding it?
        -- An impulse add differs from a considered one.
        max(CASE WHEN b.funnel_stage = 'product_detail'
                  AND b.product_sku_hash = f.added_sku THEN 1 ELSE 0 END)
                                                                   AS pre_viewed_added_sku,

        -- Dwell: milliseconds from session start to the add.
        f.add_ts - min(b.server_timestamp_epoch_ms)                AS pre_ms_to_add
    FROM first_add f
    JOIN stg_browsing b
      ON b.session_id_hash = f.session_id_hash
     AND b.event_seq      <= f.add_seq          -- <<< the truncation
    GROUP BY f.session_id_hash, f.add_seq, f.add_ts, f.added_sku
),

-- Search behaviour before the add. Joined on timestamp because stg_search has
-- no event_seq in the browsing sequence -- the two files are separate streams.
pre_search AS (
    SELECT
        p.session_id_hash,
        count(s.session_id_hash)                                AS pre_n_searches,
        count(s.session_id_hash) FILTER (WHERE s.is_zero_result) AS pre_n_zero_result,
        coalesce(sum(len(s.valid_clicked_skus)), 0)             AS pre_n_search_clicks
    FROM pre p
    LEFT JOIN stg_search s
      ON s.session_id_hash            = p.session_id_hash
     AND s.server_timestamp_epoch_ms <= p.add_ts               -- <<< truncation
    GROUP BY p.session_id_hash
),

-- ---------------------------------------------------------------------------
-- LABEL: did a purchase happen AFTER the prediction point?
--
-- Strictly after (event_seq > add_seq). A purchase at or before the first add
-- is a cross-session cart checking out, which is a different phenomenon and
-- must not be counted as this cart converting.
-- ---------------------------------------------------------------------------
label AS (
    SELECT
        f.session_id_hash,
        max(CASE WHEN b.funnel_stage = 'purchase'
                  AND b.event_seq > f.add_seq THEN 1 ELSE 0 END)  AS purchased,
        max(CASE WHEN b.funnel_stage = 'purchase'
                  AND b.product_sku_hash = f.added_sku
                  AND b.event_seq > f.add_seq THEN 1 ELSE 0 END)  AS purchased_added_sku
    FROM first_add f
    JOIN stg_browsing b ON b.session_id_hash = f.session_id_hash
    GROUP BY f.session_id_hash
)

SELECT
    p.session_id_hash,

    -- ---- temporal key, for the train/test split ----------------------------
    -- A random split would let the model learn from sessions that happen after
    -- the ones it is tested on. In any deployed setting you train on the past
    -- and predict the future, so the split must respect time or the reported
    -- score is optimistic.
    to_timestamp(p.add_ts / 1000)                       AS add_time,
    p.add_ts,

    -- ---- behavioural features (all strictly pre-add) -----------------------
    p.pre_n_events,
    p.pre_n_pageviews,
    p.pre_n_product_views,
    p.pre_n_removes,
    p.pre_n_skus_viewed,
    p.pre_n_unique_pages,
    p.pre_viewed_added_sku,
    p.add_seq                                           AS pre_add_position,
    p.pre_ms_to_add / 1000.0                            AS pre_sec_to_add,

    -- Velocity: events per minute up to the add. Distinguishes a decisive
    -- shopper from someone idling with a tab open.
    CASE WHEN p.pre_ms_to_add > 0
         THEN p.pre_n_events / (p.pre_ms_to_add / 60000.0) END AS pre_events_per_min,

    -- Browsing breadth: how many products considered per page seen.
    CASE WHEN p.pre_n_unique_pages > 0
         THEN p.pre_n_skus_viewed::DOUBLE / p.pre_n_unique_pages END AS pre_skus_per_page,

    coalesce(s.pre_n_searches, 0)                       AS pre_n_searches,
    coalesce(s.pre_n_zero_result, 0)                    AS pre_n_zero_result,
    coalesce(s.pre_n_search_clicks, 0)                  AS pre_n_search_clicks,
    (coalesce(s.pre_n_searches, 0) > 0)::INTEGER        AS pre_used_search,

    -- ---- context -----------------------------------------------------------
    dayofweek(to_timestamp(p.add_ts / 1000))            AS day_of_week,
    hour(to_timestamp(p.add_ts / 1000))                 AS hour_of_day,
    (dayofweek(to_timestamp(p.add_ts / 1000)) IN (0, 6))::INTEGER AS is_weekend,

    -- ---- product attributes of the added SKU -------------------------------
    -- price_bucket is an ordinal decile, never a currency amount.
    d.price_bucket,
    (d.price_bucket IS NULL)::INTEGER                   AS price_is_unknown,
    coalesce(d.category_lvl1, '(unknown)')              AS category_lvl1,
    coalesce(d.category_depth, 0)                       AS category_depth,

    -- ---- label -------------------------------------------------------------
    l.purchased,
    l.purchased_added_sku
FROM pre p
LEFT JOIN pre_search s USING (session_id_hash)
LEFT JOIN label      l USING (session_id_hash)
LEFT JOIN dim_product d ON d.product_sku_hash = p.added_sku;
