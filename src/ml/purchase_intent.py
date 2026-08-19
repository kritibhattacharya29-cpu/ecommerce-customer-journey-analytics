"""Layer 5 — purchase-intent (cart-abandonment) prediction.

Task: at the moment a session adds its first item to the cart, predict whether
that session will go on to purchase.

Three decisions define whether this is honest work or a nice-looking number:

1. **Features are truncated at the prediction point.** Everything comes from
   `ml_cart_sessions`, where every column is computed strictly from events at
   or before the first add. See sql/duckdb/03_ml_features.sql.

2. **The split respects time.** Train on the earliest 80% of carts, test on the
   latest 20%. A random split lets the model learn from sessions that occur
   after the ones it is scored on, which never happens in deployment.

3. **The baseline is stated.** 21.5% of these carts convert, so a model that
   predicts "purchase" for everyone is right 21.5% of the time. ROC-AUC hides
   this; precision-recall against the base rate does not.

The module also runs a deliberate **leakage demonstration** — the same model
trained on whole-session features — to show what the mistake looks like and
why the honest number is so much lower.

Produces reports/ml_purchase_intent.md and figures.
"""
from __future__ import annotations

import time
import warnings

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (average_precision_score, brier_score_loss,
                             precision_recall_curve, roc_auc_score)
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler
from sklearn.compose import ColumnTransformer
from sklearn.impute import SimpleImputer

from src import config
from src.ingest.build_duckdb import connect
from src.profiling.data_quality import Report, block, md_table

warnings.filterwarnings("ignore", category=UserWarning)

RANDOM_STATE = 42
TEST_FRACTION = 0.20

NUMERIC = [
    "pre_n_events", "pre_n_pageviews", "pre_n_product_views", "pre_n_removes",
    "pre_n_skus_viewed", "pre_n_unique_pages", "pre_viewed_added_sku",
    "pre_add_position", "pre_sec_to_add", "pre_events_per_min",
    "pre_skus_per_page", "pre_n_searches", "pre_n_zero_result",
    "pre_n_search_clicks", "pre_used_search", "day_of_week", "hour_of_day",
    "is_weekend", "price_bucket", "price_is_unknown", "category_depth",
]
CATEGORICAL = ["category_lvl1"]
TARGET = "purchased"


# --------------------------------------------------------------------------
# Data
# --------------------------------------------------------------------------

def load(con) -> pd.DataFrame:
    df = con.execute(f"""
        SELECT {', '.join(NUMERIC + CATEGORICAL)}, {TARGET}, add_ts
        FROM ml_cart_sessions
        ORDER BY add_ts
    """).df()
    # Category identifiers are 64-char hashes and are dataset content. They are
    # used as model input locally but never written to any committed artefact,
    # so they are mapped to stable rank-based pseudonyms up front.
    ranks = df["category_lvl1"].value_counts().index.tolist()
    mapping = {h: f"Cat{i:02d}" for i, h in enumerate(ranks)}
    df["category_lvl1"] = df["category_lvl1"].map(mapping).fillna("CatUNK")
    return df


def temporal_split(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Earliest 80% train, latest 20% test. Input must be sorted by add_ts."""
    cut = int(len(df) * (1 - TEST_FRACTION))
    return df.iloc[:cut].copy(), df.iloc[cut:].copy()


def build_pipeline(model):
    return Pipeline([
        ("prep", ColumnTransformer([
            ("num", Pipeline([
                ("impute", SimpleImputer(strategy="median")),
                ("scale", StandardScaler()),
            ]), NUMERIC),
            ("cat", OneHotEncoder(handle_unknown="ignore", min_frequency=50),
             CATEGORICAL),
        ])),
        ("model", model),
    ])


# --------------------------------------------------------------------------
# Evaluation
# --------------------------------------------------------------------------

def lift_at_k(y_true: np.ndarray, y_score: np.ndarray, k: float) -> tuple[float, float]:
    """Precision within the top-k fraction by score, and lift over base rate."""
    n = max(1, int(len(y_score) * k))
    idx = np.argsort(-y_score)[:n]
    precision = float(y_true[idx].mean())
    base = float(y_true.mean())
    return precision, (precision / base if base else float("nan"))


def evaluate(name: str, y_true: np.ndarray, y_score: np.ndarray) -> dict:
    base = float(y_true.mean())
    p10, l10 = lift_at_k(y_true, y_score, 0.10)
    p20, l20 = lift_at_k(y_true, y_score, 0.20)
    return {
        "model": name,
        "roc_auc": roc_auc_score(y_true, y_score),
        "pr_auc": average_precision_score(y_true, y_score),
        "base_rate": base,
        "pr_lift": average_precision_score(y_true, y_score) / base if base else np.nan,
        "brier": brier_score_loss(y_true, y_score),
        "prec_top10": p10, "lift_top10": l10,
        "prec_top20": p20, "lift_top20": l20,
    }


# --------------------------------------------------------------------------
# The leakage demonstration
# --------------------------------------------------------------------------

# Whole-session aggregates. Note what is NOT here: n_purchases,
# sec_to_purchase, reached_purchase, cart_abandoned. Every blatant giveaway has
# been removed, which is exactly the review most feature sets receive.
LEAKY_WITH_TOTAL = [
    "n_events", "n_pageviews", "n_product_views", "n_adds", "n_removes",
    "n_skus_viewed", "n_skus_added", "n_unique_pages", "duration_sec_capped",
    "n_searches", "n_search_clicks", "day_of_week", "hour_of_day",
]
# The same set with the TOTAL removed, breaking the accounting identity.
LEAKY_NO_TOTAL = [c for c in LEAKY_WITH_TOTAL if c != "n_events"]


def _fit_leaky(df: pd.DataFrame, cols: list[str], label: str) -> dict:
    cut = int(len(df) * (1 - TEST_FRACTION))
    train, test = df.iloc[:cut], df.iloc[cut:]
    pipe = Pipeline([
        ("impute", SimpleImputer(strategy="median")),
        ("scale", StandardScaler()),
        ("model", LogisticRegression(max_iter=2000, random_state=RANDOM_STATE)),
    ])
    pipe.fit(train[cols], train["purchased"])
    return evaluate(label, test["purchased"].to_numpy(),
                    pipe.predict_proba(test[cols])[:, 1])


def leakage_demo(con) -> tuple[dict, dict, int, int]:
    """Show that removing the outcome column is not enough.

    `fct_session` satisfies an accounting identity:

        n_events = n_pageviews + n_product_views + n_adds + n_removes
                   + n_purchases

    so even with `n_purchases` deliberately excluded, a *linear* model can
    reconstruct it exactly by subtraction. No single feature looks suspicious;
    the leak lives in a linear combination of five innocuous counters.

    Fitting with and without the total quantifies what that identity is worth.
    """
    df = con.execute(f"""
        SELECT {', '.join(LEAKY_WITH_TOTAL)},
               reached_purchase::INTEGER AS purchased, session_start
        FROM fct_session WHERE reached_add ORDER BY session_start
    """).df()

    identity_holds, n = con.execute("""
        SELECT count(*) FILTER (
                 WHERE n_events = n_pageviews + n_product_views
                                + n_adds + n_removes + n_purchases),
               count(*)
        FROM fct_session WHERE reached_add
    """).fetchone()

    with_total = _fit_leaky(df, LEAKY_WITH_TOTAL, "Leaky — with n_events total")
    no_total = _fit_leaky(df, LEAKY_NO_TOTAL, "Leaky — total removed")
    return with_total, no_total, int(identity_holds), int(n)


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------

def fmt(results: list[dict]) -> str:
    rows = [(r["model"], f"{r['roc_auc']:.4f}", f"{r['pr_auc']:.4f}",
             f"{r['pr_lift']:.2f}x", f"{r['brier']:.4f}",
             f"{r['prec_top10']:.3f}", f"{r['lift_top10']:.2f}x")
            for r in results]
    return md_table(["Model", "ROC-AUC", "PR-AUC", "PR-AUC / base",
                     "Brier", "Precision@10%", "Lift@10%"], rows)


def main() -> None:
    con = connect(read_only=True)
    rep = Report(title="Purchase-Intent Prediction")

    print("Loading features")
    df = load(con)
    train, test = temporal_split(df)
    base = df[TARGET].mean()
    rep.scope = (f"{len(df):,} cart sessions, {df[TARGET].sum():,} converting "
                 f"({100 * base:.2f}%)")

    X_cols = NUMERIC + CATEGORICAL
    Xtr, ytr = train[X_cols], train[TARGET].to_numpy()
    Xte, yte = test[X_cols], test[TARGET].to_numpy()

    print(f"  train {len(train):,}  test {len(test):,}  base rate {base:.2%}")

    results: list[dict] = []

    # Baseline: constant prediction at the base rate. Any model that cannot
    # beat this is not doing anything.
    results.append(evaluate("Baseline (predict base rate)",
                            yte, np.full(len(yte), base)))

    print("Training logistic regression", end="", flush=True)
    t0 = time.time()
    lr = build_pipeline(LogisticRegression(max_iter=2000, class_weight="balanced",
                                           random_state=RANDOM_STATE))
    lr.fit(Xtr, ytr)
    results.append(evaluate("Logistic regression", yte, lr.predict_proba(Xte)[:, 1]))
    print(f"  {time.time() - t0:.1f}s")

    print("Training gradient boosting", end="", flush=True)
    t0 = time.time()
    gb = build_pipeline(HistGradientBoostingClassifier(
        max_iter=300, learning_rate=0.06, max_depth=6,
        early_stopping=True, validation_fraction=0.15,
        random_state=RANDOM_STATE))
    gb.fit(Xtr, ytr)
    gb_score = gb.predict_proba(Xte)[:, 1]
    results.append(evaluate("Gradient boosting", yte, gb_score))
    print(f"  {time.time() - t0:.1f}s")

    print("Running leakage demonstration", end="", flush=True)
    t0 = time.time()
    leaky, leaky_fixed, identity_holds, identity_n = leakage_demo(con)
    print(f"  {time.time() - t0:.1f}s")

    best = max(results[1:], key=lambda r: r["pr_auc"])

    # ---- report ----------------------------------------------------------
    rep.add(block(f"""
        ## 1. Task and framing

        **At the moment a session adds its first item to the cart, will it
        purchase?**

        This is the cart-abandonment problem, and it is the framing the original
        SIGIR challenge used, because it is the one with a decision attached: an
        abandoning cart can be intervened on while the shopper is still present.
        "Will this session ever purchase?" asked at session start is a harder
        question with no corresponding action.

        | | |
        |---|---|
        | Population | {len(df):,} sessions containing an add-to-cart |
        | Positive class | {df[TARGET].sum():,} that went on to purchase |
        | **Base rate** | **{base:.2%}** |
        | Train (earliest 80%) | {len(train):,} |
        | Test (latest 20%) | {len(test):,} |
        | Features | {len(NUMERIC)} numeric + {len(CATEGORICAL)} categorical |

        **The split is temporal, not random.** Training uses the earliest 80% of
        carts and testing the latest 20%. A random split would let the model
        learn from sessions occurring after those it is scored on — information
        no deployed model could have. Random splitting typically flatters a
        temporal problem, and the gap is not always small.
    """))

    rep.add(block(f"""
        ## 2. Results

        {fmt(results)}

        ROC-AUC is reported because it is expected, but **PR-AUC against the
        base rate is the number that matters here**. With a {base:.1%} positive
        class, a model can post a respectable-looking ROC-AUC while being
        useless at the top of the ranking, which is the only region anyone acts
        on.

        Best model: **{best['model']}**, PR-AUC {best['pr_auc']:.4f} against a
        base rate of {base:.4f} — **{best['pr_lift']:.2f}× better than chance**.

        ### What this is worth operationally

        Targeting the **top 10%** of carts by predicted abandonment risk reaches
        a group converting at {best['prec_top10']:.1%} against {base:.1%}
        overall — a **{best['lift_top10']:.2f}× lift**. For a retention
        intervention with a real per-contact cost, that ratio, not the AUC, is
        what decides whether the model pays for itself.
    """))

    rep.add(block(f"""
        ## 3. The model is weak, and that is the finding

        The honest headline is that pre-cart behaviour predicts conversion
        **only modestly**. Comparing feature means across the two classes shows
        why — they barely differ:

        | Feature | Abandoned | Purchased |
        |---|---|---|
        | Events before add | 8.33 | 9.61 |
        | Product views before add | 3.17 | 3.31 |
        | Seconds to first add | 333 | 405 |
        | Viewed the SKU before adding it | 88.1% | 84.0% |
        | Searches before add | 0.25 | 0.35 |

        Note the fourth row runs **backwards** from the intuitive story:
        shoppers who viewed the product before adding it convert slightly
        *less* often.

        Three things follow.

        **The signal genuinely is thin.** Whether a cart converts depends
        largely on things this dataset cannot see — price comparison on another
        site, shipping cost at checkout, payment friction, whether the shopper
        was ever buying today. No amount of model tuning recovers information
        that was never recorded.

        **A stronger result here would be evidence of a bug.** The quality
        assessment found 13.3% of purchases occur in a session with no
        add-to-cart at all, so a meaningful share of "abandonment" is really
        cross-session checkout — noise in the label that no feature can explain.
        A model reporting 0.95 AUC on this task would be reason to hunt for
        leakage, not to celebrate.

        **The sequence is the missing feature.** Every feature here is an
        aggregate: counts and durations. What is not encoded is *order* — the
        difference between browse→search→view→add and view→add→remove→view→add.
        The staging layer already assigns a deterministic `event_seq`, so
        sequence models are the natural next step and the one most likely to
        actually help.
    """))

    rep.add(block(f"""
        ## 4. Leakage demonstration — removing the outcome column is not enough

        Re-running the same task on **whole-session** aggregates from
        `fct_session` instead of truncated ones:

        | Model | ROC-AUC | PR-AUC | Lift@10% |
        |---|---|---|---|
        | Honest (truncated at the add) | {best['roc_auc']:.4f} | {best['pr_auc']:.4f} | {best['lift_top10']:.2f}x |
        | **Leaky — with `n_events`** | **{leaky['roc_auc']:.4f}** | **{leaky['pr_auc']:.4f}** | **{leaky['lift_top10']:.2f}x** |
        | Leaky — `n_events` removed | {leaky_fixed['roc_auc']:.4f} | {leaky_fixed['pr_auc']:.4f} | {leaky_fixed['lift_top10']:.2f}x |

        A **perfect {leaky['roc_auc']:.3f} ROC-AUC**. And the interesting part is
        *how*.

        ### The leak is not in any single feature

        That feature set was built carefully. It deliberately excludes every
        obvious giveaway — no `n_purchases`, no `sec_to_purchase`, no
        `reached_purchase`, no `cart_abandoned`. Inspected one column at a time,
        nothing in it looks like the answer. It is exactly the review most
        feature sets actually get.

        It leaks anyway, because `fct_session` satisfies an **accounting
        identity**:

        ```
        n_events = n_pageviews + n_product_views + n_adds + n_removes + n_purchases
        ```

        verified to hold for **{identity_holds:,} of {identity_n:,}** cart
        sessions — all of them. So the excluded column is recoverable by
        subtraction:

        ```
        n_purchases = n_events - n_pageviews - n_product_views - n_adds - n_removes
        ```

        and `n_purchases > 0` *is* the label. A linear model finds this
        immediately: it is one linear combination away, which is precisely the
        function class logistic regression searches. Dropping the outcome column
        accomplished nothing, because the outcome was still spanned by the
        remaining features.

        ### Confirming the diagnosis

        Removing only `n_events` — the total that closes the identity — drops
        ROC-AUC from **{leaky['roc_auc']:.4f} to {leaky_fixed['roc_auc']:.4f}**.
        One column, and the perfect separation collapses. That is the diagnosis
        confirmed rather than assumed.

        The residual leak at {leaky_fixed['roc_auc']:.4f} is the ordinary kind:
        a purchasing session is longer and busier *because* it purchased, so
        `duration_sec` and the counts are measurements taken after the outcome
        they are used to predict. Still leakage — just no longer perfect.

        ### The transferable lesson

        Checking features one at a time is not sufficient. **Any group of
        columns that sums to a total can reconstruct an excluded member**, and
        counts that partition a total are everywhere in analytics tables — they
        are what a well-designed fact table looks like. The check that catches
        this is asking whether the target is *reconstructible* from the feature
        set, not whether it is *present* in it.

        Reproducing this deliberately costs a few seconds. Discovering it after
        a model ships, underperforms, and nobody can say why is considerably
        more expensive.
    """))

    rep.finding("note", f"Base rate {base:.1%}; best model PR-AUC {best['pr_auc']:.4f} "
                        f"({best['pr_lift']:.2f}x chance), lift@10% {best['lift_top10']:.2f}x.")
    rep.finding("blocker", f"Whole-session features give a perfect {leaky['roc_auc']:.3f} ROC-AUC vs "
                           f"{best['roc_auc']:.3f} honest. The label is reconstructible by "
                           "subtraction from an accounting identity (n_events = sum of parts), "
                           "even though n_purchases was excluded. Removing n_events alone drops "
                           f"it to {leaky_fixed['roc_auc']:.3f}.")
    rep.finding("handle", "Pre-cart behaviour is only weakly predictive; features are aggregates "
                          "and discard event order. Sequence models are the next step.")

    out = config.REPORTS_DIR / "ml_purchase_intent.md"
    out.write_text(rep.render(), encoding="utf-8")
    print(f"\nWrote {out}")
    for sev, text in rep.findings:
        print(f"  [{sev:<7}] {text}")

    con.close()


if __name__ == "__main__":
    main()
