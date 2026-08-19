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
| **2 — Customer journey** | Funnel conversion, cart abandonment, session depth, time-to-purchase | 🚧 |
| **3 — Search analytics** | Query → impression → click → purchase conversion, zero-result searches | 🚧 |
| **4 — Product & commercial** | Category performance, price-bucket sensitivity, product affinity | 🚧 |
| **5 — Data science** | Purchase-intent classification, session-based recommendation | 🚧 |

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

Add `--with-vectors` to the ingest only when working on the ML layer.

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
