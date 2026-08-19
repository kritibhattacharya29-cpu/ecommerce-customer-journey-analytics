"""Layer 2 — customer journey and funnel analytics.

Reads fct_session and produces reports/funnel_analysis.md.

The organising idea: report the **step-attained** and **strict-sequence**
funnels side by side. The data-quality assessment found that 13.3% of
purchasing sessions contain no add-to-cart event at all, because the
30-minute session rule splits a cart built in one session from the checkout
that happens in the next. A strict sequential funnel silently deletes those
conversions.

Neither definition is "correct". The gap between them is the interesting
quantity, because it measures how much of the business's conversion is
cross-session — something a single funnel number cannot express.
"""
from __future__ import annotations

import time

from src import config
from src.ingest.build_duckdb import connect
from src.profiling.data_quality import Report, block, md_table, pct


def funnel_overview(con, rep: Report) -> None:
    r = con.execute("""
        SELECT
            count(*)                                        AS sessions,
            count(*) FILTER (WHERE reached_detail)          AS att_detail,
            count(*) FILTER (WHERE reached_add)             AS att_add,
            count(*) FILTER (WHERE reached_purchase)        AS att_purchase,
            count(*) FILTER (WHERE strict_detail)           AS str_detail,
            count(*) FILTER (WHERE strict_add)              AS str_add,
            count(*) FILTER (WHERE strict_purchase)         AS str_purchase,
            count(*) FILTER (WHERE is_bounce)               AS bounces
        FROM fct_session
    """).fetchone()
    (n, ad, aa, ap, sd, sa, sp, bounce) = r

    rows = [
        ("Session started",   n,  100.0,          n,  100.0),
        ("Product detail view", ad, pct(ad, n),   sd, pct(sd, n)),
        ("Add to cart",       aa, pct(aa, n),     sa, pct(sa, n)),
        ("Purchase",          ap, pct(ap, n),     sp, pct(sp, n)),
    ]
    tbl = md_table(
        ["Stage", "Step-attained", "% of sessions", "Strict-sequence", "% of sessions"],
        rows)

    hidden = ap - sp
    rep.add(block(f"""
        ## 1. The funnel, both ways

        {tbl}

        **Step-attained** asks *did the session ever reach this stage?*
        **Strict-sequence** asks *did the stages happen in canonical order?*

        The two definitions disagree on **{hidden:,} purchasing sessions**
        ({pct(hidden, ap):.1f}% of all conversions). Those are real purchases
        that a textbook sequential funnel would not count.

        Stage-to-stage conversion, on the step-attained definition:

        | Transition | Rate |
        |---|---|
        | Session → product detail | {pct(ad, n):.2f}% |
        | Product detail → add to cart | {pct(aa, ad):.2f}% |
        | Add to cart → purchase | {pct(ap, aa):.2f}% |
        | **Session → purchase (overall)** | **{pct(ap, n):.2f}%** |

        {bounce:,} sessions ({pct(bounce, n):.1f}%) consist of a single event
        and never enter the funnel at all. They are retained rather than
        filtered, because excluding them would inflate every conversion rate
        below — a common way funnel dashboards end up disagreeing with revenue.
    """))
    rep.finding("note", f"Overall session→purchase conversion {pct(ap, n):.2f}%; "
                        f"strict-sequence definition would report {pct(sp, n):.2f}%, "
                        f"missing {hidden:,} real conversions.")


def abandonment(con, rep: Report) -> None:
    r = con.execute("""
        SELECT
            count(*) FILTER (WHERE reached_add)                     AS with_cart,
            count(*) FILTER (WHERE cart_abandoned)                  AS abandoned,
            count(*) FILTER (WHERE cart_emptied)                    AS emptied,
            count(*) FILTER (WHERE purchase_without_add)            AS cross_session,
            median(n_skus_added) FILTER (WHERE reached_add)         AS med_skus_added
        FROM fct_session
    """).fetchone()
    with_cart, abandoned, emptied, cross_session, med_added = r

    by_depth = con.execute("""
        SELECT
            CASE WHEN n_skus_added = 1 THEN '1 item'
                 WHEN n_skus_added = 2 THEN '2 items'
                 WHEN n_skus_added BETWEEN 3 AND 5 THEN '3-5 items'
                 ELSE '6+ items' END                        AS cart_size,
            count(*)                                        AS carts,
            count(*) FILTER (WHERE reached_purchase)        AS converted,
            100.0 * count(*) FILTER (WHERE reached_purchase) / count(*) AS conv_rate
        FROM fct_session
        WHERE reached_add
        GROUP BY 1
        ORDER BY min(n_skus_added)
    """).fetchall()

    rep.add(block(f"""
        ## 2. Cart abandonment

        | Measure | Sessions | Rate |
        |---|---|---|
        | Sessions that added to cart | {with_cart:,} | — |
        | …abandoned (added, never purchased) | {abandoned:,} | **{pct(abandoned, with_cart):.2f}%** |
        | …explicitly emptied the cart (removes ≥ adds) | {emptied:,} | {pct(emptied, with_cart):.2f}% |
        | Purchases with no add-to-cart in session | {cross_session:,} | — |

        Median distinct SKUs added per cart-building session: {med_added:,.0f}.

        Conversion by cart size:

        {md_table(["Cart size", "Sessions", "Converted", "Conversion %"], by_depth)}

        Abandonment measured this way is an **upper bound**. Some of these
        sessions did convert — in a later session, under a different session ID,
        where the purchase appears with no preceding add. The
        {cross_session:,} cross-session purchases are the visible half of that
        effect; the abandoning half is counted here. Any abandonment figure from
        session-scoped data carries this caveat, and quoting one without it
        overstates the problem.
    """))
    rep.finding("handle", f"Cart abandonment {pct(abandoned, with_cart):.1f}% is an upper bound — "
                          f"{cross_session:,} purchases occur in a later session than the add.")


def session_depth(con, rep: Report) -> None:
    rows = con.execute("""
        SELECT
            CASE WHEN n_events = 1 THEN '1 (bounce)'
                 WHEN n_events BETWEEN 2 AND 4   THEN '2-4'
                 WHEN n_events BETWEEN 5 AND 9   THEN '5-9'
                 WHEN n_events BETWEEN 10 AND 24 THEN '10-24'
                 WHEN n_events BETWEEN 25 AND 49 THEN '25-49'
                 ELSE '50+' END                              AS depth_band,
            count(*)                                         AS sessions,
            100.0 * count(*) FILTER (WHERE reached_purchase) / count(*) AS conv_rate,
            median(duration_sec_capped)                      AS med_duration_s
        FROM fct_session
        GROUP BY 1
        ORDER BY min(n_events)
    """).fetchall()

    timing = con.execute("""
        SELECT
            median(sec_to_first_add)     FILTER (WHERE reached_add),
            median(sec_to_purchase)      FILTER (WHERE reached_purchase),
            median(sec_add_to_purchase)  FILTER (WHERE reached_purchase AND reached_add),
            median(duration_sec_capped),
            median(duration_sec_capped)  FILTER (WHERE reached_purchase)
        FROM fct_session
    """).fetchone()

    rep.add(block(f"""
        ## 3. Session depth and time-to-purchase

        {md_table(["Events in session", "Sessions", "Conversion %", "Median duration (s)"], rows)}

        Timing milestones (medians, on the 30-minute-capped duration):

        | Milestone | Median |
        |---|---|
        | Session start → first add to cart | {timing[0]:,.0f} s |
        | Session start → purchase | {timing[1]:,.0f} s |
        | First add → purchase | {timing[2]:,.0f} s |
        | Session duration, all sessions | {timing[3]:,.0f} s |
        | Session duration, converting sessions | {timing[4]:,.0f} s |

        Medians are used throughout rather than means. The quality assessment
        found sessions containing gaps that breach Coveo's own 30-minute rule —
        background tabs, clock skew, server-side stitching — and a handful of
        multi-hour sessions would drag any mean badly. The capped duration is
        reported alongside the raw one in `fct_session` so the adjustment is
        visible rather than baked in.
    """))


def search_influence(con, rep: Report) -> None:
    r = con.execute("""
        SELECT
            count(*) FILTER (WHERE used_search)                          AS searched,
            count(*) FILTER (WHERE NOT used_search)                      AS no_search,
            100.0 * count(*) FILTER (WHERE used_search AND reached_purchase)
                  / nullif(count(*) FILTER (WHERE used_search), 0)       AS conv_search,
            100.0 * count(*) FILTER (WHERE NOT used_search AND reached_purchase)
                  / nullif(count(*) FILTER (WHERE NOT used_search), 0)   AS conv_no_search,
            count(*) FILTER (WHERE n_zero_result_searches > 0)           AS had_zero_result,
            100.0 * count(*) FILTER (WHERE n_zero_result_searches > 0 AND reached_purchase)
                  / nullif(count(*) FILTER (WHERE n_zero_result_searches > 0), 0)
                                                                          AS conv_zero_result
        FROM fct_session
    """).fetchone()
    searched, no_search, cs, cns, zr, czr = r

    rep.add(block(f"""
        ## 4. Does search change the outcome?

        | Segment | Sessions | Conversion |
        |---|---|---|
        | Used search | {searched:,} | **{cs:.2f}%** |
        | Did not use search | {no_search:,} | {cns:.2f}% |
        | Hit ≥1 zero-result search | {zr:,} | {czr:.2f}% |

        Searching sessions convert at {cs / cns if cns else 0:,.1f}× the rate of
        non-searching ones.

        **This is a correlation, not a lever.** Shoppers who search are already
        further along in intent — searching is a symptom of wanting something
        specific, not a cause of buying it. Pushing more shoppers into the
        search box would not move the first two rows toward each other.

        The third row was intended as the controlled comparison, holding intent
        roughly constant by looking only within searching sessions. It returns
        **{czr:.2f}% against {cs:.2f}%** — no difference. Whatever a failed
        search costs, it is not visible at session grain. That null result is
        examined in `search_analysis.md` §4, which sets out why the session is
        the wrong unit of analysis for this question.
    """))
    if czr and cs:
        rep.finding("note", f"Zero-result searches show no session-level conversion penalty "
                            f"({czr:.2f}% vs {cs:.2f}% overall) — see search_analysis.md §4.")


def weekly_pattern(con, rep: Report) -> None:
    rows = con.execute("""
        SELECT day_of_week,
               count(*)                                                     AS sessions,
               100.0 * count(*) FILTER (WHERE reached_purchase) / count(*)  AS conv_rate
        FROM fct_session GROUP BY 1 ORDER BY 1
    """).fetchall()
    names = {0: "Sunday", 1: "Monday", 2: "Tuesday", 3: "Wednesday",
             4: "Thursday", 5: "Friday", 6: "Saturday"}
    rows = [(names.get(int(d), str(d)), s, c) for d, s, c in rows]

    hours = con.execute("""
        SELECT hour_of_day,
               count(*)                                                     AS sessions,
               100.0 * count(*) FILTER (WHERE reached_purchase) / count(*)  AS conv_rate
        FROM fct_session GROUP BY 1 ORDER BY 1
    """).fetchall()
    peak = max(hours, key=lambda r: r[1])
    best = max(hours, key=lambda r: r[2])

    rep.add(block(f"""
        ## 5. Intra-week pattern

        {md_table(["Day", "Sessions", "Conversion %"], rows)}

        Busiest hour: **{peak[0]:02d}:00** ({peak[1]:,} sessions).
        Highest-converting hour: **{best[0]:02d}:00** ({best[2]:.2f}%).

        Only day-of-week and hour-of-day are analysed. Coveo shifted every
        timestamp by an undisclosed number of weeks, so absolute dates carry no
        meaning — but the shift preserves intra-week structure, which is what
        makes this section valid and any calendar-based seasonality invalid.
        Hours are as recorded server-side and are not timezone-corrected.
    """))


SECTIONS = [
    ("Funnel overview", funnel_overview),
    ("Cart abandonment", abandonment),
    ("Session depth", session_depth),
    ("Search influence", search_influence),
    ("Weekly pattern", weekly_pattern),
]


def main() -> None:
    con = connect(read_only=True)
    rep = Report(title="Customer Journey & Funnel Analysis")
    n_sessions, n_events = con.execute(
        "SELECT count(*), sum(n_events) FROM fct_session").fetchone()
    rep.set_scope(events=int(n_events), sessions=int(n_sessions))
    print("Building funnel analysis\n")
    for name, fn in SECTIONS:
        print(f"  {name:<22}", end="", flush=True)
        t0 = time.time()
        fn(con, rep)
        print(f"{time.time() - t0:>7,.1f}s")
    con.close()

    out = config.REPORTS_DIR / "funnel_analysis.md"
    out.write_text(rep.render(), encoding="utf-8")
    print(f"\nWrote {out}")
    for sev, text in rep.findings:
        print(f"  [{sev:<7}] {text}")


if __name__ == "__main__":
    main()
