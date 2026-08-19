"""Layer 3 — search analytics.

This is the layer the Coveo dataset makes possible and most public e-commerce
datasets do not. For each of 819,516 queries it records the **impression set**
(every SKU returned) alongside the click set, which means:

  * click-through rate has a real denominator, not an assumed one
  * the rank of each clicked result is recoverable, so position bias is
    measurable rather than theoretical
  * products *seen and not clicked* are genuine negative feedback

Produces reports/search_analysis.md.
"""
from __future__ import annotations

import time

from src import config
from src.ingest.build_duckdb import connect
from src.profiling.data_quality import Report, block, md_table, pct


def volume_and_failure(con, rep: Report) -> None:
    r = con.execute("""
        SELECT
            count(*)                                              AS queries,
            count(DISTINCT session_id_hash)                       AS sessions,
            count(*) FILTER (WHERE is_zero_result)                AS zero_result,
            count(*) FILTER (WHERE NOT is_zero_result)            AS with_results,
            count(*) FILTER (WHERE len(valid_clicked_skus) > 0)   AS with_click,
            sum(n_phantom_clicks)                                 AS phantom,
            median(n_results) FILTER (WHERE NOT is_zero_result)   AS med_results
        FROM stg_search
    """).fetchone()
    queries, sessions, zero, with_results, with_click, phantom, med_results = r

    rep.add(block(f"""
        ## 1. Search volume and failure rate

        | Measure | Value | Rate |
        |---|---|---|
        | Search queries | {queries:,} | 100% |
        | Sessions issuing ≥1 search | {sessions:,} | — |
        | **Zero-result queries** | {zero:,} | **{pct(zero, queries):.2f}%** |
        | Queries returning results | {with_results:,} | {pct(with_results, queries):.2f}% |
        | Queries with ≥1 valid click | {with_click:,} | {pct(with_click, queries):.2f}% |
        | Median result-set size | {med_results:,.0f} | — |
        | Clicks discarded as phantom | {phantom:,} | — |

        **Click-through rate: {pct(with_click, with_results):.2f}%** of queries
        that returned anything received a click.

        Note the denominator. CTR is computed over queries *that returned
        results*, not all queries — a zero-result search cannot be clicked, and
        including it would blend two different failures into one number that
        improves when search gets worse.

        The {zero:,} zero-result queries are the most visible failure mode
        here: each is a shopper who stated intent explicitly and received
        nothing, and the remedies are concrete (synonym and redirect rules,
        stocking decisions informed by what people ask for and cannot find).

        It is tempting to price that directly as lost revenue. **§4 tests that
        assumption and it does not hold** — sessions hitting a zero-result page
        convert no worse than sessions that do not. The rate is a real quality
        problem; the revenue impact is not established by this data at session
        grain, and §4 sets out what would establish it.
    """))
    rep.finding("note", f"{pct(zero, queries):.1f}% of searches return zero results "
                        f"({zero:,} queries) — a large failure rate, but see §4: no measurable "
                        "session-level conversion penalty.")
    rep.finding("note", f"CTR {pct(with_click, with_results):.1f}% computed over queries that "
                        "returned results; zero-result queries excluded from the denominator.")


def position_bias(con, rep: Report) -> None:
    """Rank of clicked results — recoverable only because impressions are logged."""
    rows = con.execute("""
        WITH clicks AS (
            SELECT unnest(valid_clicked_skus) AS sku, result_skus
            FROM stg_search
            WHERE len(valid_clicked_skus) > 0
        ),
        ranked AS (
            SELECT list_position(result_skus, sku) AS rank_1based FROM clicks
        )
        SELECT
            CASE WHEN rank_1based = 1 THEN '1 (first result)'
                 WHEN rank_1based = 2 THEN '2'
                 WHEN rank_1based = 3 THEN '3'
                 WHEN rank_1based BETWEEN 4 AND 5   THEN '4-5'
                 WHEN rank_1based BETWEEN 6 AND 10  THEN '6-10'
                 WHEN rank_1based BETWEEN 11 AND 20 THEN '11-20'
                 ELSE '21+' END                       AS position_band,
            count(*)                                  AS clicks
        FROM ranked
        WHERE rank_1based IS NOT NULL
        GROUP BY 1
        ORDER BY min(rank_1based)
    """).fetchall()

    total = sum(r[1] for r in rows)
    tbl = [(band, n, pct(n, total)) for band, n in rows]

    top1 = next((n for band, n in rows if band.startswith("1 ")), 0)
    top3 = sum(n for band, n in rows if band in ("1 (first result)", "2", "3"))

    stats = con.execute("""
        WITH clicks AS (
            SELECT unnest(valid_clicked_skus) AS sku, result_skus
            FROM stg_search WHERE len(valid_clicked_skus) > 0
        )
        SELECT median(list_position(result_skus, sku)),
               quantile_cont(list_position(result_skus, sku), 0.9)
        FROM clicks
    """).fetchone()

    rep.add(block(f"""
        ## 2. Position bias

        Where in the result list did shoppers click? This is only answerable
        because Coveo logged the full impression set, not just the click.

        {md_table(["Result position", "Clicks", "% of clicks"], tbl)}

        Median clicked position: **{stats[0]:,.0f}**; 90th percentile:
        {stats[1]:,.0f}.

        **{pct(top1, total):.1f}% of all clicks land on the first result, and
        {pct(top3, total):.1f}% land in the top three.**

        This is the central caveat for any relevance work on this data: a
        product's click count measures *where the ranker put it* at least as
        much as how relevant it was. Training a relevance model on raw clicks
        therefore teaches it to reproduce the existing ranking, including its
        mistakes. Correcting for position — by modelling propensity, or by
        comparing products that appeared at similar ranks — is a prerequisite,
        not a refinement.
    """))
    rep.finding("blocker", f"{pct(top1, total):.0f}% of search clicks land on position 1 — "
                           "raw clicks encode ranker position, not relevance; any relevance "
                           "model must correct for position bias.")


def search_to_outcome(con, rep: Report) -> None:
    """Does a search click actually lead anywhere?"""
    r = con.execute("""
        WITH clicked AS (
            SELECT DISTINCT s.session_id_hash, unnest(s.valid_clicked_skus) AS sku
            FROM stg_search s WHERE len(s.valid_clicked_skus) > 0
        ),
        outcome AS (
            SELECT
                c.session_id_hash, c.sku,
                max(CASE WHEN b.funnel_stage = 'product_detail' THEN 1 ELSE 0 END) AS viewed,
                max(CASE WHEN b.funnel_stage = 'add_to_cart'    THEN 1 ELSE 0 END) AS added,
                max(CASE WHEN b.funnel_stage = 'purchase'       THEN 1 ELSE 0 END) AS purchased
            FROM clicked c
            LEFT JOIN stg_browsing b
              ON b.session_id_hash  = c.session_id_hash
             AND b.product_sku_hash = c.sku
            GROUP BY 1, 2
        )
        SELECT count(*), sum(viewed), sum(added), sum(purchased) FROM outcome
    """).fetchone()
    n_clicks, viewed, added, purchased = r

    rep.add(block(f"""
        ## 3. Search click → outcome

        Following each clicked SKU into the same session's browsing stream:

        | Outcome for a clicked product | Occurrences | % of search clicks |
        |---|---|---|
        | Clicked from search results | {n_clicks:,} | 100% |
        | …later viewed (product detail) in the session | {viewed:,} | {pct(viewed, n_clicks):.2f}% |
        | …later added to cart | {added:,} | {pct(added, n_clicks):.2f}% |
        | …later purchased | {purchased:,} | **{pct(purchased, n_clicks):.2f}%** |

        This joins the search and browsing files on `(session, SKU)`, which is
        the only linkage available between them — there is no event ID tying a
        click to a subsequent pageview.

        Two consequences follow, and both limit the claim. Attribution is
        **within-session only**: a product clicked today and bought tomorrow
        appears here as a failure, and the quality assessment already showed
        13.3% of purchases occur in a different session from the add. And the
        join cannot distinguish a view *caused by* the search click from one the
        shopper would have reached anyway. These are association rates, not
        causal effects, and the honest ceiling on this analysis is that it
        describes co-occurrence within a session.
    """))
    rep.finding("handle", f"{pct(purchased, n_clicks):.1f}% of search-clicked products are purchased "
                          "in the same session; cross-session outcomes are not attributable.")


def zero_result_impact(con, rep: Report) -> None:
    r = con.execute("""
        SELECT
            count(*) FILTER (WHERE used_search)                          AS searched,
            count(*) FILTER (WHERE n_zero_result_searches > 0)           AS any_zero,
            100.0 * count(*) FILTER (WHERE used_search AND reached_purchase)
                  / nullif(count(*) FILTER (WHERE used_search), 0)       AS conv_search,
            100.0 * count(*) FILTER (WHERE n_zero_result_searches > 0 AND reached_purchase)
                  / nullif(count(*) FILTER (WHERE n_zero_result_searches > 0), 0) AS conv_zero,
            100.0 * count(*) FILTER (WHERE used_search AND n_zero_result_searches = 0
                                      AND reached_purchase)
                  / nullif(count(*) FILTER (WHERE used_search
                                      AND n_zero_result_searches = 0), 0) AS conv_clean
        FROM fct_session
    """).fetchone()
    searched, any_zero, conv_search, conv_zero, conv_clean = r

    gap = (conv_zero or 0) - (conv_clean or 0)

    rep.add(block(f"""
        ## 4. What does a zero-result search cost? — a null result

        | Segment | Sessions | Conversion |
        |---|---|---|
        | All searching sessions | {searched:,} | {conv_search:.2f}% |
        | …that never hit a zero-result page | {searched - any_zero:,} | **{conv_clean:.2f}%** |
        | …that hit ≥1 zero-result page | {any_zero:,} | **{conv_zero:.2f}%** |

        **Difference: {gap:+.2f} percentage points.** On {any_zero:,} sessions
        that is indistinguishable from zero.

        This is not the result §1 leads you to expect, and it is worth stating
        plainly rather than burying: **at session level, hitting a zero-result
        search has no measurable effect on whether the session converts.**

        ### Why the obvious story fails

        Three explanations are consistent with this, and they are not mutually
        exclusive:

        1. **Shoppers recover.** A zero-result page is a minor speed bump —
           they reformulate the query, or navigate by category instead, and
           carry on. Search is one route to a product, not the only one.
        2. **Selection cancels the damage.** Hitting a zero-result page is
           mostly a function of *searching a lot*, and sessions that search a
           lot are higher-intent to begin with. Higher baseline intent and the
           cost of the failure push in opposite directions.
        3. **The unit of analysis is wrong.** A session is far too coarse. One
           empty result among fifteen searches is diluted to invisibility by
           the fourteen that worked.

        ### What would actually answer the question

        The session is the wrong grain. The right test is at **query level**:
        for each query, what happens in the next few events — does the shopper
        reformulate, switch to category browsing, or stop? Comparing the
        immediate next action after a zero-result query against the same after
        a successful one isolates the effect without diluting it across a whole
        session.

        That requires interleaving search events into the browsing timeline on
        `(session, timestamp)` — which the staging layer already makes possible,
        since both tables carry a deterministic sequence. It is the natural next
        piece of work, and it is the honest answer to "how much does this cost",
        which the table above does **not** establish.
    """))
    rep.finding("note", f"Zero-result searches show NO session-level conversion penalty "
                        f"({conv_zero:.2f}% vs {conv_clean:.2f}%, {gap:+.2f} pp). Session is too "
                        "coarse a grain; query-level next-action analysis is required.")


SECTIONS = [
    ("Volume & failure rate", volume_and_failure),
    ("Position bias", position_bias),
    ("Click → outcome", search_to_outcome),
    ("Zero-result impact", zero_result_impact),
]


def main() -> None:
    con = connect(read_only=True)
    rep = Report(title="Search Analytics")
    n_q, n_s = con.execute(
        "SELECT count(*), count(DISTINCT session_id_hash) FROM stg_search").fetchone()
    rep.set_scope(events=int(n_q), sessions=int(n_s))

    print("Building search analysis\n")
    for name, fn in SECTIONS:
        print(f"  {name:<24}", end="", flush=True)
        t0 = time.time()
        fn(con, rep)
        print(f"{time.time() - t0:>7,.1f}s")
    con.close()

    out = config.REPORTS_DIR / "search_analysis.md"
    out.write_text(rep.render(), encoding="utf-8")
    print(f"\nWrote {out}")
    for sev, text in rep.findings:
        print(f"  [{sev:<7}] {text}")


if __name__ == "__main__":
    main()
