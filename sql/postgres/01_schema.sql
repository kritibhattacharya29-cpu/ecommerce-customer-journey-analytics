-- ===========================================================================
-- PostgreSQL SERVING LAYER
--
-- Holds the modelled marts only -- never the 36M raw events. DuckDB does the
-- out-of-core ETL; Postgres serves the result to SQL clients and Power BI.
--
-- The split is deliberate. Pushing the full event stream through Postgres on a
-- laptop would take hours per iteration and consume ~20 GB, and none of the
-- questions this project answers need row-level events at query time. What
-- they need is a small, well-indexed dimensional model -- which is exactly
-- what Postgres is good at.
--
--   psql -U postgres -c "CREATE DATABASE coveo_analytics;"
--   psql -U postgres -d coveo_analytics -f sql/postgres/01_schema.sql
-- ===========================================================================

CREATE SCHEMA IF NOT EXISTS coveo;
SET search_path TO coveo, public;


-- ---------------------------------------------------------------------------
-- dim_product
-- ---------------------------------------------------------------------------
DROP TABLE IF EXISTS fct_product_performance CASCADE;
DROP TABLE IF EXISTS fct_session CASCADE;
DROP TABLE IF EXISTS dim_product CASCADE;

CREATE TABLE dim_product (
    product_sku_hash    CHAR(64)     PRIMARY KEY,
    in_catalog          BOOLEAN      NOT NULL,
    category_lvl1       TEXT         NOT NULL DEFAULT '(unknown)',
    category_lvl2       TEXT         NOT NULL DEFAULT '(unknown)',
    category_lvl3       TEXT         NOT NULL DEFAULT '(unknown)',
    category_hash       TEXT,
    category_depth      SMALLINT     NOT NULL DEFAULT 0,
    price_bucket        DOUBLE PRECISION,
    has_price           BOOLEAN      NOT NULL,
    -- '(unknown)' is a real member, not a NULL. Enforced so a future join or
    -- GROUP BY cannot silently drop half the catalog.
    price_tier          TEXT         NOT NULL
);

CREATE INDEX idx_dim_product_cat1  ON dim_product (category_lvl1);
CREATE INDEX idx_dim_product_tier  ON dim_product (price_tier);


-- ---------------------------------------------------------------------------
-- fct_session -- the spine of the analysis, one row per shopping session
-- ---------------------------------------------------------------------------
CREATE TABLE fct_session (
    session_id_hash         CHAR(64)     PRIMARY KEY,
    session_start           TIMESTAMP    NOT NULL,
    session_end             TIMESTAMP    NOT NULL,
    day_of_week             SMALLINT     NOT NULL,
    hour_of_day             SMALLINT     NOT NULL,

    -- Both the raw and capped duration are stored so the outlier treatment is
    -- visible and auditable rather than silently baked into one number.
    duration_sec_raw        DOUBLE PRECISION,
    duration_sec_capped     DOUBLE PRECISION,

    n_events                INTEGER      NOT NULL,
    n_pageviews             INTEGER      NOT NULL,
    n_product_views         INTEGER      NOT NULL,
    n_adds                  INTEGER      NOT NULL,
    n_removes               INTEGER      NOT NULL,
    n_purchases             INTEGER      NOT NULL,
    n_skus_viewed           INTEGER      NOT NULL,
    n_skus_added            INTEGER      NOT NULL,
    n_skus_purchased        INTEGER      NOT NULL,
    n_unique_pages          INTEGER      NOT NULL,

    -- step-attained funnel
    reached_session         BOOLEAN      NOT NULL,
    reached_detail          BOOLEAN      NOT NULL,
    reached_add             BOOLEAN      NOT NULL,
    reached_purchase        BOOLEAN      NOT NULL,

    -- strict-sequence funnel
    strict_detail           BOOLEAN,
    strict_add              BOOLEAN,
    strict_purchase         BOOLEAN,

    purchase_without_add    BOOLEAN      NOT NULL,

    is_bounce               BOOLEAN      NOT NULL,
    cart_abandoned          BOOLEAN      NOT NULL,
    cart_emptied            BOOLEAN      NOT NULL,

    sec_to_first_add        DOUBLE PRECISION,
    sec_to_purchase         DOUBLE PRECISION,
    sec_add_to_purchase     DOUBLE PRECISION,

    n_searches              INTEGER      NOT NULL,
    n_zero_result_searches  INTEGER      NOT NULL,
    n_search_clicks         INTEGER      NOT NULL,
    n_valid_search_clicks   INTEGER      NOT NULL,
    n_search_impressions    INTEGER      NOT NULL,
    used_search             BOOLEAN      NOT NULL,

    had_duplicate_events    BOOLEAN      NOT NULL,

    -- A strict-sequence purchase is by definition also a step-attained one.
    -- Encoding the relationship as a constraint means a future change to the
    -- funnel logic that breaks it fails loudly at load time.
    CONSTRAINT strict_implies_attained
        CHECK (strict_purchase IS NOT TRUE OR reached_purchase),
    CONSTRAINT abandoned_implies_add
        CHECK (NOT cart_abandoned OR reached_add)
);

CREATE INDEX idx_session_purchase  ON fct_session (reached_purchase);
CREATE INDEX idx_session_dow_hour  ON fct_session (day_of_week, hour_of_day);
CREATE INDEX idx_session_search    ON fct_session (used_search);
CREATE INDEX idx_session_abandoned ON fct_session (cart_abandoned) WHERE cart_abandoned;


-- ---------------------------------------------------------------------------
-- fct_product_performance
-- ---------------------------------------------------------------------------
CREATE TABLE fct_product_performance (
    product_sku_hash        CHAR(64)  PRIMARY KEY REFERENCES dim_product,
    sessions_viewed         INTEGER   NOT NULL,
    sessions_added          INTEGER   NOT NULL,
    sessions_purchased      INTEGER   NOT NULL,
    sessions_removed        INTEGER   NOT NULL,
    n_views                 INTEGER   NOT NULL,
    n_adds                  INTEGER   NOT NULL,
    n_purchases             INTEGER   NOT NULL,
    in_catalog              BOOLEAN,
    category_lvl1           TEXT,
    category_lvl2           TEXT,
    price_bucket            DOUBLE PRECISION,
    price_tier              TEXT,
    -- Rates are NULL, never 0, when the denominator is 0: a product nobody
    -- viewed has an undefined conversion rate, and reporting 0% would rank it
    -- as a failing product rather than an unseen one.
    view_to_add_rate        DOUBLE PRECISION,
    add_to_purchase_rate    DOUBLE PRECISION,
    view_to_purchase_rate   DOUBLE PRECISION
);

CREATE INDEX idx_prodperf_cat1 ON fct_product_performance (category_lvl1);
CREATE INDEX idx_prodperf_conv ON fct_product_performance (view_to_purchase_rate DESC NULLS LAST);


-- ---------------------------------------------------------------------------
-- Convenience views for BI tools
-- ---------------------------------------------------------------------------

-- Both funnel definitions in one shape Power BI can chart directly.
CREATE OR REPLACE VIEW v_funnel AS
WITH totals AS (SELECT count(*)::NUMERIC AS n FROM fct_session)
SELECT stage, step_order, definition, sessions,
       ROUND(100.0 * sessions / (SELECT n FROM totals), 4) AS pct_of_sessions
FROM (
    SELECT 'Session'        AS stage, 1 AS step_order, 'step_attained' AS definition,
           count(*) AS sessions FROM fct_session
    UNION ALL SELECT 'Product detail', 2, 'step_attained', count(*) FROM fct_session WHERE reached_detail
    UNION ALL SELECT 'Add to cart',    3, 'step_attained', count(*) FROM fct_session WHERE reached_add
    UNION ALL SELECT 'Purchase',       4, 'step_attained', count(*) FROM fct_session WHERE reached_purchase
    UNION ALL SELECT 'Session',        1, 'strict_sequence', count(*) FROM fct_session
    UNION ALL SELECT 'Product detail', 2, 'strict_sequence', count(*) FROM fct_session WHERE strict_detail
    UNION ALL SELECT 'Add to cart',    3, 'strict_sequence', count(*) FROM fct_session WHERE strict_add
    UNION ALL SELECT 'Purchase',       4, 'strict_sequence', count(*) FROM fct_session WHERE strict_purchase
) f
ORDER BY definition, step_order;


CREATE OR REPLACE VIEW v_category_performance AS
SELECT
    d.category_lvl1,
    count(*)                                        AS skus,
    sum(p.sessions_viewed)                          AS sessions_viewed,
    sum(p.sessions_added)                           AS sessions_added,
    sum(p.sessions_purchased)                       AS sessions_purchased,
    -- Coverage is reported next to every price statistic, because 51.7% of
    -- SKUs have no price bucket and a mean over the other half is misleading
    -- without it.
    ROUND(100.0 * count(*) FILTER (WHERE d.has_price) / count(*), 2) AS pct_with_price,
    ROUND(AVG(d.price_bucket) FILTER (WHERE d.has_price)::NUMERIC, 2) AS avg_price_bucket,
    ROUND(100.0 * sum(p.sessions_purchased)::NUMERIC
          / NULLIF(sum(p.sessions_viewed), 0), 4)   AS view_to_purchase_pct
FROM fct_product_performance p
JOIN dim_product d USING (product_sku_hash)
GROUP BY d.category_lvl1
ORDER BY sessions_viewed DESC;
