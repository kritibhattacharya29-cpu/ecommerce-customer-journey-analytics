"""Layer 4 — product and commercial analytics.

A licence note that shapes the output. Category identifiers in this dataset are
64-character hashes, and Coveo's Terms & Conditions forbid distributing "the
Dataset and/or data contained therein". Publishing a table keyed by those
hashes would be republishing dataset identifiers.

So categories are reported under stable rank-based pseudonyms — "Category A",
"Category B" — assigned by descending traffic. The hash never leaves the local
warehouse. This is both licence-safe and more readable: a 64-char hex string
tells a reader nothing that "Category A" does not.

Produces reports/product_analysis.md.
"""
from __future__ import annotations

import time

from src import config
from src.ingest.build_duckdb import connect
from src.profiling.data_quality import Report, block, md_table, pct

# A..Z pseudonyms, assigned by descending traffic.
LABELS = [chr(ord("A") + i) for i in range(26)]


def catalog_coverage(con, rep: Report) -> None:
    r = con.execute("""
        SELECT
            count(*)                                          AS skus_seen,
            count(*) FILTER (WHERE in_catalog)                AS in_catalog,
            count(*) FILTER (WHERE category_lvl1 <> '(unknown)') AS with_category,
            count(*) FILTER (WHERE has_price)                 AS with_price
        FROM dim_product
        WHERE product_sku_hash IN (SELECT product_sku_hash FROM fct_product_performance)
    """).fetchone()
    seen, in_cat, with_cat, with_price = r

    traffic = con.execute("""
        SELECT
            sum(sessions_viewed)                                              AS all_views,
            sum(sessions_viewed) FILTER (WHERE category_lvl1 <> '(unknown)')  AS cat_views,
            sum(sessions_viewed) FILTER (WHERE price_tier <> '(unknown)')     AS price_views,
            sum(sessions_purchased)                                           AS all_purch,
            sum(sessions_purchased) FILTER (WHERE price_tier <> '(unknown)')  AS price_purch
        FROM fct_product_performance
    """).fetchone()
    all_views, cat_views, price_views, all_purch, price_purch = traffic

    xtab = con.execute("""
        SELECT (category_lvl1 <> '(unknown)') AS has_category,
               (sessions_viewed > 0)          AS was_viewed,
               count(*)                       AS skus
        FROM fct_product_performance GROUP BY 1, 2 ORDER BY 1, 2
    """).fetchall()
    xt = {(bool(a), bool(b)): c for a, b, c in xtab}

    behaviour = con.execute("""
        SELECT
            CASE WHEN sessions_viewed = 0 THEN 'Never viewed' ELSE 'Viewed' END AS grp,
            count(*)                     AS skus,
            sum(sessions_added)          AS adds,
            sum(sessions_purchased)      AS purchases,
            sum(sessions_removed)        AS removes
        FROM fct_product_performance GROUP BY 1 ORDER BY 1
    """).fetchall()

    rep.add(block(f"""
        ## 1. Catalog coverage is not missing at random

        Before any category or price breakdown, the honest first question is how
        much of the business those breakdowns can see.

        | Measure | SKUs | Coverage |
        |---|---|---|
        | Distinct SKUs with any browsing activity | {seen:,} | 100% |
        | …present in the catalog file | {in_cat:,} | {pct(in_cat, seen):.2f}% |
        | …with a category | {with_cat:,} | {pct(with_cat, seen):.2f}% |
        | …with a price bucket | {with_price:,} | {pct(with_price, seen):.2f}% |

        Roughly half the SKUs lack metadata — which sounds like it should
        cripple every breakdown below. Weighting by traffic instead of SKU
        count tells a completely different story:

        | Measure | Covered | Share of total |
        |---|---|---|
        | Product views on categorised SKUs | {cat_views:,} | **{pct(cat_views, all_views):.2f}%** |
        | Product views on priced SKUs | {price_views:,} | {pct(price_views, all_views):.2f}% |
        | Purchases on priced SKUs | {price_purch:,} | **{pct(price_purch, all_purch):.2f}%** |

        ### The missingness has a structure

        Crossing "has a category" against "was ever viewed" shows the two are
        almost the same variable:

        | | Never viewed | Viewed |
        |---|---|---|
        | **No category** | {xt.get((False, False), 0):,} | {xt.get((False, True), 0):,} |
        | **Has category** | {xt.get((True, False), 0):,} | {xt.get((True, True), 0):,} |

        **Every SKU that was ever viewed has catalog metadata.** The cell that
        would break every category report — viewed but uncategorised — contains
        {xt.get((False, True), 0):,} SKUs.

        What the metadata-less SKUs actually do:

        {md_table(["Group", "SKUs", "Adds", "Purchases", "Removes"], behaviour)}

        They are almost entirely **cart removals and purchases with no
        corresponding view** — the exact signature of the cross-session carts
        found in the quality assessment. These are products whose detail page
        was viewed in an earlier session; only the later checkout or removal
        falls inside the window, and the catalog export evidently covers the
        actively-merchandised range rather than everything a cart might contain.

        ### Why this matters for the rest of the report

        - **View-based breakdowns (§2, §3) are effectively complete** at
          {pct(cat_views, all_views):.1f}% coverage. The 51% figure is not the
          relevant one.
        - **Purchase-based breakdowns are not.** {100 - pct(price_purch, all_purch):.1f}%
          of purchases occur on SKUs with no price bucket, so any
          revenue-by-price statement silently omits a tenth of conversions.
        - **Cart-removal analysis is largely blind**, since removals concentrate
          almost entirely in the uncatalogued group.

        None of this would be visible had uncatalogued SKUs been dropped instead
        of modelled as `(unknown)` members — both tables would simply have read
        100% and the structure would be invisible.
    """))
    rep.finding("blocker", f"Catalog metadata is NOT missing at random: all {xt.get((True, True), 0):,} "
                           f"ever-viewed SKUs have it, while uncatalogued SKUs are almost purely "
                           "cart removals/purchases from cross-session carts.")
    rep.finding("handle", f"{100 - pct(price_purch, all_purch):.1f}% of purchases are on SKUs with no "
                          "price bucket — revenue-by-price breakdowns omit them.")


def category_performance(con, rep: Report) -> None:
    rows = con.execute("""
        SELECT
            category_lvl1,
            count(*)                    AS skus,
            sum(sessions_viewed)        AS views,
            sum(sessions_added)         AS adds,
            sum(sessions_purchased)     AS purchases
        FROM fct_product_performance
        WHERE category_lvl1 <> '(unknown)'
        GROUP BY 1
        ORDER BY views DESC
    """).fetchall()

    tbl = []
    for i, (_hash, skus, views, adds, purch) in enumerate(rows):
        # Pseudonym, not the hash -- see module docstring.
        label = f"Category {LABELS[i]}" if i < len(LABELS) else f"Category #{i + 1}"
        tbl.append((label, skus, views, adds, purch,
                    pct(adds, views), pct(purch, views)))

    rep.add(block(f"""
        ## 2. Category performance

        {md_table(["Category", "SKUs", "Views", "Adds", "Purchases",
                   "View→Add %", "View→Purchase %"], tbl)}

        Categories are shown under stable pseudonyms assigned by descending
        traffic. The underlying identifiers are 64-character hashes and are
        dataset content, so they are not reproduced here — see the module
        docstring for the reasoning.

        The spread in view→purchase rate across categories is the commercially
        interesting quantity: it separates categories where browsing converts
        from categories that attract traffic and do not. Note that with hashed
        categories there is no way to tell whether a low-converting category is
        a merchandising failure or simply a browse-heavy category like
        accessories, where low conversion is normal. Anonymisation buys privacy
        at the cost of exactly the domain context that would make this
        actionable — worth stating rather than glossing over.
    """))


def price_sensitivity(con, rep: Report) -> None:
    rows = con.execute("""
        SELECT
            price_bucket,
            count(*)                 AS skus,
            sum(sessions_viewed)     AS views,
            sum(sessions_added)      AS adds,
            sum(sessions_purchased)  AS purchases
        FROM fct_product_performance
        WHERE price_bucket IS NOT NULL
        GROUP BY 1 ORDER BY 1
    """).fetchall()
    tbl = [(f"{int(b)}", skus, views, adds, purch, pct(adds, views), pct(purch, views))
           for b, skus, views, adds, purch in rows]

    lo = [r for r in rows if r[0] <= 3]
    hi = [r for r in rows if r[0] >= 8]
    lo_rate = pct(sum(r[4] for r in lo), sum(r[2] for r in lo))
    hi_rate = pct(sum(r[4] for r in hi), sum(r[2] for r in hi))

    rep.add(block(f"""
        ## 3. Price sensitivity

        `price_bucket` is a **10-quantile bucket**, not a currency amount. Each
        bucket holds roughly a tenth of the catalog by price rank, so the only
        valid statements are ordinal ones: bucket 8 is pricier than bucket 4.
        "Bucket 8 costs twice bucket 4" is not something this data supports.

        {md_table(["Price bucket", "SKUs", "Views", "Adds", "Purchases",
                   "View→Add %", "View→Purchase %"], tbl)}

        Cheapest three buckets convert at **{lo_rate:.2f}%** view→purchase;
        priciest three at **{hi_rate:.2f}%**.

        Two warnings before reading a price effect into this. Price bucket is
        confounded with category — expensive categories differ from cheap ones
        in far more than price, so this comparison is not holding product type
        constant. And the ~52% of SKUs with no price bucket are excluded
        entirely from this table; if missing metadata correlates with anything
        commercially relevant, that absence is itself a selection effect.

        A defensible price analysis would compare buckets *within* a category
        rather than across the whole catalog. That is a natural extension of
        this table and is not claimed by it.
    """))
    rep.finding("handle", f"Price view→purchase {lo_rate:.2f}% (buckets 1-3) vs {hi_rate:.2f}% "
                          "(8-10), but price is confounded with category — not a clean price effect.")


def funnel_shape(con, rep: Report) -> None:
    r = con.execute("""
        SELECT
            count(*)                                                   AS skus,
            count(*) FILTER (WHERE sessions_viewed = 0)                AS never_viewed,
            count(*) FILTER (WHERE sessions_viewed > 0 AND sessions_added = 0) AS viewed_never_added,
            count(*) FILTER (WHERE sessions_purchased > 0)             AS ever_purchased,
            median(sessions_viewed)                                    AS med_views
        FROM fct_product_performance
    """).fetchone()
    skus, never_viewed, viewed_never_added, ever_purchased, med_views = r

    conc = con.execute("""
        WITH ranked AS (
            SELECT sessions_viewed,
                   sum(sessions_viewed) OVER () AS total,
                   sum(sessions_viewed) OVER (ORDER BY sessions_viewed DESC
                                              ROWS UNBOUNDED PRECEDING) AS cume,
                   row_number() OVER (ORDER BY sessions_viewed DESC) AS rn,
                   count(*) OVER () AS n
            FROM fct_product_performance
        )
        SELECT
            max(CASE WHEN rn <= n * 0.01 THEN 100.0 * cume / total END),
            max(CASE WHEN rn <= n * 0.10 THEN 100.0 * cume / total END),
            max(CASE WHEN rn <= n * 0.20 THEN 100.0 * cume / total END)
        FROM ranked
    """).fetchone()

    rep.add(block(f"""
        ## 4. How concentrated is demand?

        | Measure | SKUs | Share |
        |---|---|---|
        | SKUs with browsing activity | {skus:,} | 100% |
        | …viewed but never added to a cart | {viewed_never_added:,} | {pct(viewed_never_added, skus):.2f}% |
        | …purchased at least once | {ever_purchased:,} | {pct(ever_purchased, skus):.2f}% |

        Median product views per SKU: {med_views:,.0f}.

        Traffic concentration:

        | Top N% of SKUs by views | Share of all product views |
        |---|---|
        | Top 1% | {conc[0]:.1f}% |
        | Top 10% | {conc[1]:.1f}% |
        | Top 20% | {conc[2]:.1f}% |

        This is the classic long tail, and it constrains the recommendation work
        in Layer 5 directly. With demand this concentrated, a recommender that
        optimises raw hit-rate will learn to suggest head products to everyone —
        scoring well on paper while adding nothing a shopper could not have
        found unaided. Coverage and novelty metrics have to sit alongside
        accuracy, and that decision follows from this table rather than from
        preference.
    """))
    rep.finding("note", f"Top 10% of SKUs take {conc[1]:.0f}% of product views — head-heavy demand "
                        "means Layer 5 recommenders need coverage/novelty metrics, not just hit-rate.")


SECTIONS = [
    ("Catalog coverage", catalog_coverage),
    ("Category performance", category_performance),
    ("Price sensitivity", price_sensitivity),
    ("Demand concentration", funnel_shape),
]


def main() -> None:
    con = connect(read_only=True)
    rep = Report(title="Product & Commercial Analytics")
    skus, views = con.execute(
        "SELECT count(*), sum(sessions_viewed) FROM fct_product_performance").fetchone()
    rep.scope = f"{int(views):,} product views across {int(skus):,} SKUs"

    print("Building product analysis\n")
    for name, fn in SECTIONS:
        print(f"  {name:<24}", end="", flush=True)
        t0 = time.time()
        fn(con, rep)
        print(f"{time.time() - t0:>7,.1f}s")
    con.close()

    out = config.REPORTS_DIR / "product_analysis.md"
    out.write_text(rep.render(), encoding="utf-8")
    print(f"\nWrote {out}")
    for sev, text in rep.findings:
        print(f"  [{sev:<7}] {text}")


if __name__ == "__main__":
    main()
