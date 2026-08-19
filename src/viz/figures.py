"""Generate the analytical figures embedded in the reports.

Every chart plots aggregates only -- counts, rates and curves. Nothing here
labels or plots an individual session, SKU or URL, which is what keeps the
committed images compatible with Coveo's no-redistribution terms.

Usage:
    python -m src.viz.figures
"""
from __future__ import annotations

import numpy as np
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter, PercentFormatter

from src import config
from src.ingest.build_duckdb import connect
from src.viz import style
from src.viz.style import BLUE, ORANGE, GREY, DARK_GREY, RED, save

FIGDIR = config.FIGURES_DIR


def thousands(x, _pos=None) -> str:
    if x >= 1_000_000:
        return f"{x / 1_000_000:.1f}M"
    if x >= 1_000:
        return f"{x / 1_000:.0f}k"
    return f"{x:.0f}"


# --------------------------------------------------------------------------

def fig_funnel(con) -> None:
    """The headline chart: the two funnel definitions diverging."""
    r = con.execute("""
        SELECT
            count(*)                                 AS n,
            count(*) FILTER (WHERE reached_detail)   AS ad,
            count(*) FILTER (WHERE reached_add)      AS aa,
            count(*) FILTER (WHERE reached_purchase) AS ap,
            count(*) FILTER (WHERE strict_detail)    AS sd,
            count(*) FILTER (WHERE strict_add)       AS sa,
            count(*) FILTER (WHERE strict_purchase)  AS sp
        FROM fct_session
    """).fetchone()
    n, ad, aa, ap, sd, sa, sp = r

    stages = ["Session", "Product\ndetail", "Add to\ncart", "Purchase"]
    attained = [n, ad, aa, ap]
    strict = [n, sd, sa, sp]

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(11.5, 4.6),
                                   gridspec_kw={"width_ratios": [1.4, 1]})

    # Linear % of sessions, not a log count. The collapse from 66% to 4% IS the
    # funnel; a log axis would flatten it into four near-equal bars and hide
    # exactly what the chart exists to show.
    att_pct = [100.0 * v / n for v in attained]
    str_pct = [100.0 * v / n for v in strict]

    y = np.arange(len(stages))[::-1]
    h = 0.38
    ax1.barh(y + h / 2, att_pct, height=h, color=BLUE, label="Step-attained")
    ax1.barh(y - h / 2, str_pct, height=h, color=ORANGE, label="Strict-sequence")
    ax1.set_yticks(y, stages)
    ax1.set_xlim(0, 118)
    ax1.xaxis.set_major_formatter(PercentFormatter(decimals=0))
    ax1.set_xlabel("Share of all sessions")
    ax1.set_title("The funnel, measured two ways")
    ax1.legend(loc="lower right")
    style.despine(ax1)
    for yy, p, v in zip(y + h / 2, att_pct, attained):
        ax1.text(p + 1.5, yy, f"{p:.2f}%  ({thousands(v)})",
                 va="center", fontsize=8, color=BLUE)
    for yy, p, v in zip(y - h / 2, str_pct, strict):
        ax1.text(p + 1.5, yy, f"{p:.2f}%  ({thousands(v)})",
                 va="center", fontsize=8, color=ORANGE)

    # Right panel: where the two definitions disagree.
    gap = [a - s for a, s in zip(attained, strict)]
    bars = ax2.bar(range(len(stages)), gap, color=RED, width=0.55)
    ax2.set_xticks(range(len(stages)), stages)
    ax2.yaxis.set_major_formatter(FuncFormatter(thousands))
    ax2.set_ylabel("Sessions counted by one definition only")
    ax2.set_title("Sessions a strict funnel discards")
    style.despine(ax2)
    ax2.set_ylim(0, max(gap) * 1.30)
    for i, (b, g) in enumerate(zip(bars, gap)):
        if g == 0:
            continue
        ax2.text(b.get_x() + b.get_width() / 2, g + max(gap) * 0.03,
                 f"{g:,}", ha="center", va="bottom", fontsize=9,
                 color=RED, fontweight="bold")
    # Park the callout over the two empty stages, where nothing can collide.
    ax2.annotate(f"{100 * gap[3] / ap:.1f}% of all\nconversions",
                 xy=(2.72, gap[3] * 0.92), xytext=(0.5, max(gap) * 0.66),
                 fontsize=10, color=RED, fontweight="bold", ha="center",
                 arrowprops=dict(arrowstyle="->", color=RED, lw=1.5,
                                 connectionstyle="arc3,rad=-0.15"))

    fig.suptitle("A strict sequential funnel omits a quarter of all conversions",
                 fontsize=13, fontweight="bold", y=1.02)
    save(fig, FIGDIR / "funnel.png")


def fig_session_depth(con) -> None:
    rows = con.execute("""
        SELECT
            CASE WHEN n_events = 1 THEN '1'
                 WHEN n_events BETWEEN 2 AND 4   THEN '2-4'
                 WHEN n_events BETWEEN 5 AND 9   THEN '5-9'
                 WHEN n_events BETWEEN 10 AND 24 THEN '10-24'
                 WHEN n_events BETWEEN 25 AND 49 THEN '25-49'
                 ELSE '50+' END AS band,
            count(*) AS sessions,
            100.0 * count(*) FILTER (WHERE reached_purchase) / count(*) AS conv
        FROM fct_session GROUP BY 1 ORDER BY min(n_events)
    """).fetchall()
    bands = [r[0] for r in rows]
    sess = [r[1] for r in rows]
    conv = [r[2] for r in rows]

    fig, ax = plt.subplots(figsize=(8, 4.4))
    ax.bar(bands, sess, color=GREY, width=0.6, label="Sessions")
    ax.set_ylabel("Sessions", color=DARK_GREY)
    ax.yaxis.set_major_formatter(FuncFormatter(thousands))
    ax.set_xlabel("Events in session")
    style.despine(ax)

    ax2 = ax.twinx()
    ax2.plot(bands, conv, color=ORANGE, marker="o", linewidth=2.4,
             markersize=7, label="Conversion rate")
    ax2.set_ylabel("Conversion rate", color=ORANGE)
    ax2.yaxis.set_major_formatter(PercentFormatter(decimals=0))
    ax2.tick_params(axis="y", colors=ORANGE)
    ax2.grid(False)
    for side in ("top", "right", "left", "bottom"):
        ax2.spines[side].set_visible(False)
    for x, v in zip(bands, conv):
        ax2.annotate(f"{v:.1f}%", (x, v), textcoords="offset points",
                     xytext=(0, 9), ha="center", fontsize=8, color=ORANGE)

    ax.set_title("Engagement predicts conversion — and 43% of sessions are single-event")
    save(fig, FIGDIR / "session_depth.png")


def fig_position_bias(con) -> None:
    rows = con.execute("""
        WITH clicks AS (
            SELECT unnest(valid_clicked_skus) AS sku, result_skus
            FROM stg_search WHERE len(valid_clicked_skus) > 0
        )
        SELECT list_position(result_skus, sku) AS pos, count(*) AS clicks
        FROM clicks
        WHERE list_position(result_skus, sku) IS NOT NULL
          AND list_position(result_skus, sku) <= 20
        GROUP BY 1 ORDER BY 1
    """).fetchall()
    pos = [r[0] for r in rows]
    clicks = [r[1] for r in rows]
    total = con.execute("""
        SELECT sum(len(valid_clicked_skus)) FROM stg_search
    """).fetchone()[0]
    share = [100.0 * c / total for c in clicks]
    cume = np.cumsum(share)

    fig, ax = plt.subplots(figsize=(8.5, 4.4))
    ax.bar(pos, share, color=BLUE, width=0.7, label="Share of clicks")
    ax.set_xlabel("Position in search results")
    ax.set_ylabel("Share of all clicks")
    ax.yaxis.set_major_formatter(PercentFormatter(decimals=0))
    ax.set_xticks(pos)
    style.despine(ax)

    ax2 = ax.twinx()
    ax2.plot(pos, cume, color=ORANGE, linewidth=2.2, marker=".", label="Cumulative")
    ax2.set_ylabel("Cumulative share", color=ORANGE)
    ax2.yaxis.set_major_formatter(PercentFormatter(decimals=0))
    ax2.tick_params(axis="y", colors=ORANGE)
    ax2.set_ylim(0, 100)
    ax2.grid(False)
    for side in ("top", "right", "left", "bottom"):
        ax2.spines[side].set_visible(False)

    ax.annotate(f"{share[0]:.1f}% of clicks\nland on result #1",
                xy=(1, share[0]), xytext=(4.2, share[0] * 0.92),
                fontsize=9, color=RED, fontweight="bold",
                arrowprops=dict(arrowstyle="->", color=RED, lw=1.4))
    ax.set_title("Position bias: clicks measure ranking as much as relevance")
    save(fig, FIGDIR / "position_bias.png")


def fig_demand_concentration(con) -> None:
    rows = con.execute("""
        WITH r AS (
            SELECT sessions_viewed,
                   row_number() OVER (ORDER BY sessions_viewed DESC) AS rn,
                   sum(sessions_viewed) OVER (ORDER BY sessions_viewed DESC
                                              ROWS UNBOUNDED PRECEDING) AS cume,
                   sum(sessions_viewed) OVER () AS total,
                   count(*) OVER () AS n
            FROM fct_product_performance
        )
        SELECT 100.0 * rn / n AS pct_skus, 100.0 * cume / total AS pct_views
        FROM r WHERE rn % 200 = 0 OR rn <= 50
        ORDER BY rn
    """).fetchall()
    x = [r[0] for r in rows]
    y = [r[1] for r in rows]

    fig, ax = plt.subplots(figsize=(7.2, 4.6))
    ax.plot(x, y, color=BLUE, linewidth=2.6)
    ax.fill_between(x, y, color=BLUE, alpha=0.12)
    ax.plot([0, 100], [0, 100], color=GREY, linestyle="--", linewidth=1.4,
            label="Perfectly even demand")
    ax.set_xlabel("Share of SKUs (ranked by views)")
    ax.set_ylabel("Share of all product views")
    ax.xaxis.set_major_formatter(PercentFormatter(decimals=0))
    ax.yaxis.set_major_formatter(PercentFormatter(decimals=0))
    ax.set_xlim(0, 100)
    ax.set_ylim(0, 101)
    ax.legend(loc="lower right")
    style.despine(ax)

    y10 = np.interp(10, x, y)
    ax.plot([10, 10], [0, y10], color=ORANGE, linestyle=":", linewidth=1.6)
    ax.plot([0, 10], [y10, y10], color=ORANGE, linestyle=":", linewidth=1.6)
    ax.annotate(f"Top 10% of SKUs\ntake {y10:.0f}% of views",
                xy=(10, y10), xytext=(24, y10 - 26), fontsize=9,
                color=ORANGE, fontweight="bold",
                arrowprops=dict(arrowstyle="->", color=ORANGE, lw=1.4))
    ax.set_title("Demand is head-heavy — which constrains recommendation")
    save(fig, FIGDIR / "demand_concentration.png")


def fig_catalog_coverage(con) -> None:
    """The 'not missing at random' cross-tab, as a chart."""
    rows = con.execute("""
        SELECT (category_lvl1 <> '(unknown)') AS has_cat,
               (sessions_viewed > 0)          AS viewed,
               count(*)                       AS skus
        FROM fct_product_performance GROUP BY 1, 2
    """).fetchall()
    m = {(bool(a), bool(b)): c for a, b, c in rows}
    grid = np.array([[m.get((True, False), 0), m.get((True, True), 0)],
                     [m.get((False, False), 0), m.get((False, True), 0)]])

    fig, ax = plt.subplots(figsize=(6.4, 4.2))
    im = ax.imshow(grid, cmap="Blues", aspect="auto")
    ax.set_xticks([0, 1], ["Never viewed", "Viewed"])
    ax.set_yticks([0, 1], ["Has category", "No category"])
    ax.grid(False)
    for i in range(2):
        for j in range(2):
            v = grid[i, j]
            ax.text(j, i, f"{v:,}", ha="center", va="center", fontsize=14,
                    fontweight="bold",
                    color="white" if v > grid.max() * 0.5 else "#1a202c")
    for side in ("top", "right", "left", "bottom"):
        ax.spines[side].set_visible(False)
    ax.set_title("Catalog metadata is not missing at random")
    fig.text(0.5, -0.04,
             "Every SKU that was ever viewed has metadata. The cell that would break\n"
             "category reporting — viewed but uncategorised — is empty.",
             ha="center", fontsize=9, color=DARK_GREY)
    save(fig, FIGDIR / "catalog_coverage.png")


def fig_hourly_pattern(con) -> None:
    """Traffic and conversion by UTC hour — evidence the conversion is sane."""
    rows = con.execute("""
        SELECT hour_of_day,
               count(*)                                                    AS sessions,
               100.0 * count(*) FILTER (WHERE reached_purchase) / count(*) AS conv
        FROM fct_session GROUP BY 1 ORDER BY 1
    """).fetchall()
    hrs = [r[0] for r in rows]
    sess = [r[1] for r in rows]
    conv = [r[2] for r in rows]

    fig, ax = plt.subplots(figsize=(9.5, 4.4))
    ax.bar(hrs, sess, color=GREY, width=0.72, label="Sessions")
    ax.set_xlabel("Hour of day (UTC)")
    ax.set_ylabel("Sessions", color=DARK_GREY)
    ax.yaxis.set_major_formatter(FuncFormatter(thousands))
    ax.set_xticks(range(0, 24))
    ax.set_xlim(-0.7, 23.7)
    style.despine(ax)

    ax2 = ax.twinx()
    ax2.plot(hrs, conv, color=ORANGE, linewidth=2.4, marker="o", markersize=4.5)
    ax2.set_ylabel("Conversion rate", color=ORANGE)
    ax2.yaxis.set_major_formatter(PercentFormatter(decimals=1))
    ax2.tick_params(axis="y", colors=ORANGE)
    ax2.grid(False)
    for side in ("top", "right", "left", "bottom"):
        ax2.spines[side].set_visible(False)

    peak = max(rows, key=lambda r: r[1])
    trough = min(rows, key=lambda r: r[1])
    ax.annotate(f"peak {peak[0]:02d}:00 UTC\n≈ evening local",
                xy=(peak[0], peak[1]), xytext=(peak[0] + 3.2, peak[1] * 0.93),
                fontsize=9, color=BLUE, fontweight="bold",
                arrowprops=dict(arrowstyle="->", color=BLUE, lw=1.4))
    ax.annotate(f"trough {trough[0]:02d}:00 UTC\n≈ small hours local",
                xy=(trough[0], trough[1]), xytext=(trough[0] + 1.4, peak[1] * 0.42),
                fontsize=9, color=DARK_GREY,
                arrowprops=dict(arrowstyle="->", color=DARK_GREY, lw=1.4))

    ax.set_title("A normal retail day — in UTC, which is not the shopper's clock")
    save(fig, FIGDIR / "hourly_pattern.png")


FIGURES = [
    ("funnel", fig_funnel),
    ("hourly pattern", fig_hourly_pattern),
    ("session depth", fig_session_depth),
    ("position bias", fig_position_bias),
    ("demand concentration", fig_demand_concentration),
    ("catalog coverage", fig_catalog_coverage),
]


def main() -> None:
    style.apply()
    con = connect(read_only=True)
    print(f"Writing figures to {FIGDIR}\n")
    for name, fn in FIGURES:
        print(f"  {name}")
        fn(con)
    con.close()
    print("\nAll figures written.")


if __name__ == "__main__":
    main()
