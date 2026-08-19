# E-commerce Customer Journey & Purchase Intent Analytics

End-to-end analytics on **36 million real e-commerce events** from the Coveo
SIGIR 2021 eCom Data Challenge — session reconstruction, funnel and search
conversion analysis, and purchase-intent modelling.

The dataset is genuine production telemetry from a live retailer, not a teaching
table. Coveo document it as imperfect: events are *not stored in chronological
order*, tracking pixels double-fire, and half the product catalog has no
metadata. Handling that honestly is the point of this project.

> **Data:** © Coveo Solutions Inc. Not included in this repository — see
> [`docs/DATA_ACCESS.md`](docs/DATA_ACCESS.md) to obtain your own copy under
> Coveo's research/educational Terms & Conditions.

---

## Why this dataset

Most funnel portfolio projects use a clean, pre-aggregated table where every
session already has a tidy `view → cart → purchase` sequence. Nothing is learned
from that, because every hard decision has already been made by whoever built
the table.

This dataset makes you make those decisions. Every number below is measured by
[`src/profiling/data_quality.py`](src/profiling/data_quality.py) against all
36,079,307 events, and written to
[`reports/data_quality_report.md`](reports/data_quality_report.md) *before* any
analysis depends on it.

| What the data does | Measured | Why it matters |
|---|---|---|
| **Timestamps are not a total order** | 9,763,612 `(session, ms)` collisions — **99.4%** are the documented PDP `detail`+`pageview` double-fire | Sorting by timestamp alone is non-deterministic: the same query returns a different funnel each run. Physical file position is captured at ingest as the stable tie-break. |
| **Purchases with no add-to-cart** | 7,071 of 53,209 purchasing sessions — **13.3%** | The 30-min session rule splits cross-session carts. A strict sequential funnel deletes 13% of conversions. Both funnel definitions are reported side by side. |
| **Searches returning nothing** | 216,762 of 819,516 — **26.5%** | A quarter of all searches are a dead end. This is the most directly actionable commercial finding in the dataset. |
| **Catalog is half empty** | 34,348 of 66,386 SKUs (**51.7%**) have no price bucket | Category and price segments silently drop these unless `(unknown)` is an explicit member. |
| **Clicks on products never returned** | 17,622 | Result sets get re-ranked between impression and click. These clicks have no denominator and are excluded from CTR. |
| **File order *is* nearly chronological** | only 269 sessions (**0.005%**) truly out of order | Coveo's warning is real but minor — and that is exactly what makes it dangerous: rare enough to survive spot-checking, frequent enough to corrupt results. |

The last row is the point. Testing a documented warning and finding it
*quantitatively different from what it implies* is worth more than repeating it.

---

## Architecture

```
  Raw CSVs (immutable, outside the repo)
  browsing_train.csv 6.0 GB · search_train.csv 1.7 GB · sku_to_content.csv 71 MB
                              │
                              ▼
  ┌──────────────────────────────────────────────────────────────┐
  │  DuckDB — ETL engine                                         │
  │  out-of-core columnar processing on a laptop                 │
  │                                                              │
  │   raw_*        exact copy + file_row_idx (order preserved)   │
  │   stg_*        typed, deduplicated, sequenced                │
  │   dim_/fct_    conformed dimensional model                   │
  └──────────────────────────────────────────────────────────────┘
                              │  compact marts only
                              ▼
  ┌──────────────────────────────────────────────────────────────┐
  │  PostgreSQL — serving layer                                  │
  │  aggregated marts for SQL analysis + Power BI                │
  └──────────────────────────────────────────────────────────────┘
                              │
                    ┌─────────┴─────────┐
                    ▼                   ▼
              Power BI            scikit-learn
              dashboard        purchase-intent model
```

**Why two engines.** DuckDB scans and joins 36 M rows on a laptop in seconds
without a server, which makes iterating on the transformation logic practical.
But a columnar file is not a serving layer — PostgreSQL holds the modelled marts
so the analysis is queryable by standard SQL tooling and Power BI. Using each
for what it is good at is the actual engineering decision here; running the full
event stream through Postgres would take hours per iteration and consume ~20 GB.

**Why vectors are ingested separately.** `query_vector`, `description_vector`
and `image_vector` are ~1.6 GB of the 1.7 GB search file, and no funnel or
conversion question needs them. They are loaded only behind `--with-vectors`,
for the ML layer, keeping every analytical scan cheap.

---

## Project layers

| Layer | What it does | Status |
|---|---|---|
| **1 — Data engineering** | Raw ingest, data-quality assessment, session reconstruction, dimensional model | ✅ |
| **2 — Customer journey** | Funnel (both definitions), cart abandonment, session depth, time-to-purchase | ✅ [report](reports/funnel_analysis.md) |
| **3 — Search analytics** | Zero-result rate, CTR, position bias, click → purchase attribution | ✅ [report](reports/search_analysis.md) |
| **4 — Product & commercial** | Catalog coverage, category performance, price sensitivity, demand concentration | ✅ [report](reports/product_analysis.md) |
| **5 — Data science** | Purchase-intent / cart-abandonment prediction, leakage analysis | ✅ [report](reports/ml_purchase_intent.md) |

---

## Selected results

**The funnel, both ways** — over 4,934,699 sessions:

| Stage | Step-attained | Strict-sequence |
|---|---|---|
| Session | 4,934,699 (100%) | 4,934,699 (100%) |
| Product detail | 3,260,353 (66.07%) | 3,260,353 (66.07%) |
| Add to cart | 214,684 (4.35%) | 194,882 (3.95%) |
| Purchase | **53,209 (1.08%)** | **40,291 (0.82%)** |

The two definitions disagree on **12,918 purchasing sessions — 24.3% of all
conversions**. A textbook sequential funnel would report 0.82% and quietly omit
a quarter of the revenue.

Cart abandonment is **78.5%**, reported explicitly as an *upper bound*: 7,071 of
those "abandoned" carts were purchased in a later session, under a different
session ID.

**A null result worth more than a positive one.** 26.5% of searches return zero
results, which looks like an obvious revenue leak. Tested directly, it isn't —
sessions hitting a zero-result page convert at 3.57% against 3.56% for those
that never do. A **+0.00 pp** difference across 145,065 sessions.
[`search_analysis.md` §4](reports/search_analysis.md) works through why the
session is the wrong unit of analysis for the question, and what would actually
answer it. Reporting the 26.5% as lost revenue would have been the easy,
confident, wrong answer.

**Position bias.** 21.7% of search clicks land on result #1 and 44.3% in the top
three — recoverable only because Coveo log the full impression set. Any
relevance model trained on raw clicks would mostly learn to reproduce the
existing ranker.

**Missing catalog data is not missing at random.** 51% of SKUs have no category
— which sounds fatal for category reporting. Crossing "has metadata" against
"was ever viewed" shows they are almost the same variable:

| | Never viewed | Viewed |
|---|---|---|
| **No category** | 27,910 | **0** |
| **Has category** | 8 | 29,565 |

Every SKU that was ever viewed has metadata, so view-based breakdowns are
100% covered. The uncatalogued SKUs are almost purely cart *removals* and
purchases with no view — the same cross-session carts the funnel analysis
found. The practical consequence is specific rather than general: 10.1% of
purchases sit on unpriced SKUs, so revenue-by-price omits a tenth of
conversions, while category-by-views omits nothing. Dropping those rows instead
of modelling them as `(unknown)` would have hidden the structure entirely.

**Purchase-intent prediction, and a leakage trap worth seeing.** Predicting
whether a cart converts, using only events up to the add-to-cart:

| Model | ROC-AUC | PR-AUC | Lift@10% |
|---|---|---|---|
| Baseline (21.5% base rate) | 0.500 | 0.215 | 1.00× |
| Honest model (features truncated at the add) | 0.657 | 0.313 | 1.92× |
| **Same task, whole-session features** | **1.000** | — | — |

That 1.000 is a *perfect* score, and the reason is the interesting part. The
leaky feature set deliberately excludes every obvious giveaway — no
`n_purchases`, no `sec_to_purchase`, no `reached_purchase`. Inspected one column
at a time, nothing looks wrong. But `fct_session` satisfies an accounting
identity that holds for all 214,684 cart sessions:

```
n_events = n_pageviews + n_product_views + n_adds + n_removes + n_purchases
```

so the excluded label is recoverable by subtraction, and logistic regression
finds it instantly — it is exactly one linear combination away. Removing
`n_events` alone drops ROC-AUC from **1.000 to 0.793**, confirming the diagnosis
rather than assuming it.

The transferable lesson: checking features one at a time is not enough. Any
group of columns summing to a total can reconstruct an excluded member, and
counts that partition a total are what a well-designed fact table looks like.
The right question is whether the target is *reconstructible* from the feature
set, not whether it is *present* in it. Six standing assertions in
[`tests/test_pipeline.py`](tests/test_pipeline.py) guard the truncation.

The honest model is deliberately unimpressive at 0.657, and
[the report](reports/ml_purchase_intent.md) argues that is the correct result:
pre-cart behaviour barely separates the classes (8.33 vs 9.61 events; shoppers
who viewed the product before adding it convert *slightly less* often), because
what decides a checkout — price comparison elsewhere, shipping cost, payment
friction — was never recorded. A 0.95 here would be grounds to hunt for leakage,
not to celebrate.

**Row-count reconciliation.** `python -m src.transform.reconcile` proves every
dropped row is accounted for:

```
raw_browsing                               36,079,307
- NULL session or timestamp                         0
- redundant duplicate copies                   -1,800
- PDP pageviews paired with detail         -9,707,556
= expected stg_browsing                    26,369,951
  actual stg_browsing                      26,369,951   RECONCILED exactly
```

---

## Repository layout

```
├── src/
│   ├── config.py           single source of truth for paths (reads .env)
│   ├── ingest/             raw CSV → DuckDB
│   ├── profiling/          data-quality assessment
│   ├── transform/          staging + dimensional model
│   └── analysis/           funnel, search, product analytics
├── sql/
│   ├── duckdb/             transformation SQL
│   └── postgres/           serving-layer schema + marts
├── notebooks/              exploratory analysis
├── tests/fixtures/         SYNTHETIC data only — never real rows
├── docs/
│   ├── DATA_ACCESS.md      how to obtain the dataset + licence compliance
│   └── data_dictionary.md  field-level reference
├── reports/                generated analysis output
└── dashboard/              Power BI artefacts
```

---

## Reproducing

```bash
git clone <this-repo> && cd ecommerce-customer-journey-analytics
python -m venv .venv && .venv/Scripts/activate      # Windows
pip install -r requirements.txt
cp .env.example .env                                 # then set COVEO_RAW_DIR
```

Obtain the dataset ([`docs/DATA_ACCESS.md`](docs/DATA_ACCESS.md)), then:

```bash
python -m src.ingest.build_duckdb
```

```bash
python -m src.profiling.data_quality
```

```bash
python -m src.transform.build_model
```

```bash
python -m src.transform.reconcile
```

```bash
python -m src.analysis.funnel && python -m src.analysis.search && python -m src.analysis.product
```

```bash
python -m src.ml.purchase_intent
```

Add `--with-vectors` to the ingest only when working on the ML layer.

Verify the pipeline without the dataset — the tests run entirely on synthetic
fixtures:

```bash
python tests/test_pipeline.py
```

### Runtime and resources

Measured on a 4-core / 7.8 GB laptop:

| Step | Time | Notes |
|---|---|---|
| Ingest (all three CSVs) | 115 s | 7.8 GB CSV → 1.9 GB warehouse |
| Data-quality assessment | ~130 s | full 36M events |
| `build_model` | 737 s | `stg_browsing` alone is 609 s |
| Analyses | < 10 s | reads the marts |

`stg_browsing` needs two sorts of ~7 GB with a 2 GB memory budget, peaking near
12 GB of spill. On a machine with more RAM it is far quicker.
[`docs/architecture.md` §7](docs/architecture.md) explains the memory-limit
choice and the surrogate-key optimisation that would remove the bottleneck.

---

## Licence

Code in this repository: **MIT** (see [`LICENSE`](LICENSE)).

The dataset is **not** covered by that licence, is **not** redistributed here,
and remains governed by Coveo's Terms & Conditions — non-commercial research and
educational use, no redistribution, no de-anonymisation attempts.

Dataset citation:

```bibtex
@inproceedings{CoveoSIGIR2021,
  author    = {Tagliabue, Jacopo and Greco, Ciro and Roy, Jean-Francis and
               Bianchi, Federico and Cassani, Giovanni and Yu, Bingqing and
               Chia, Patrick John},
  title     = {SIGIR 2021 E-Commerce Workshop Data Challenge},
  year      = {2021},
  booktitle = {SIGIR eCom 2021}
}
```
