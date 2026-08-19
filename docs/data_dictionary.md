# Data dictionary

Two parts: the **source fields** as Coveo document them, and the **derived
fields** this project adds. Where the two disagree — where a documented
guarantee doesn't hold in the data — that is noted explicitly, because those are
the places analysis goes wrong silently.

---

## Source: `browsing_train.csv` → `raw_browsing`

| Field | Type | Description |
|---|---|---|
| `session_id_hash` | string | Hashed session identifier. A session groups events **at most 30 minutes apart**; a return after 31 minutes gets a new ID. |
| `event_type` | enum | Google Protocol event type: `pageview` or `event`. An `add` can arrive on page load *or* as a standalone event. |
| `product_action` | enum | `detail`, `add`, `purchase`, `remove`. **NULL means a non-product pageview** (e.g. an FAQ page), not missing data. |
| `product_sku_hash` | string | Hashed product identifier, when the event involves a product. |
| `server_timestamp_epoch_ms` | int | Epoch milliseconds, **shifted by an undisclosed number of weeks** for anonymisation. Intra-week patterns are preserved. |
| `hashed_url` | string | Hashed URL of the current page. |

### Caveats Coveo state directly

- Row order is **not** chronological. Sequence must be rebuilt from
  `session_id_hash` + `server_timestamp_epoch_ms`.
- A product detail page may emit **both** a `detail` and a `pageview` event.
- Removing an item may emit **several consecutive `remove` events**.
- Click events are **not** here — they live in `search_train.csv`.

### Caveats found by measurement, not documented

Quantified in [`reports/data_quality_report.md`](../reports/data_quality_report.md):

- `server_timestamp_epoch_ms` is **not a total order** — multiple events in one
  session share a single millisecond, so a deterministic tie-break is required.
- Some sessions contain internal gaps **exceeding 30 minutes**, contradicting
  the stated session rule.
- Some sessions contain a `purchase` with **no `add`** anywhere in the session.

---

## Source: `search_train.csv` → `raw_search`

| Field | Type | Description |
|---|---|---|
| `session_id_hash` | string | Joins to `raw_browsing`. Not every search session has browsing events. |
| `server_timestamp_epoch_ms` | int | As above. |
| `query_vector` | vector | Dense representation of the query text. Compatible with `description_vector`. Ingested only with `--with-vectors`. |
| `product_skus_hash` | list | SKUs **returned** by the search — the impression set. |
| `clicked_skus_hash` | list | SKUs **clicked** from that result set, if any. |

The impression set is what makes this dataset unusual: recording products *seen
but not clicked* supplies genuine negative feedback, which most public
e-commerce datasets lack entirely.

Both list fields arrive as Python reprs (`['abc', 'def']`) and are parsed once at
ingest into `VARCHAR[]`.

---

## Source: `sku_to_content.csv` → `raw_catalog`

| Field | Type | Description |
|---|---|---|
| `product_sku_hash` | string | Hashed SKU. Primary key. |
| `category_hash` | string | Hashed category hierarchy, `/`-separated, up to 3 levels. |
| `price_bucket` | float | Price as a **10-quantile bucket** (deciles), observed range 1–10. Not a currency amount — only ordinal comparisons are valid. |
| `description_vector` | vector | Dense text metadata representation. `--with-vectors` only. |
| `image_vector` | vector | Dense image metadata representation. `--with-vectors` only. |

Coveo note metadata is present "when the information is available". In practice
**roughly half the catalog rows are entirely empty** — SKU present, everything
else NULL. Any category or price segmentation must model this explicitly.

---

## Derived fields

### `raw_browsing`

| Field | Type | Rationale |
|---|---|---|
| `file_row_idx` | bigint | Position in the original file, captured at ingest. Preserves the physical order so file-vs-timestamp disagreement can be *measured*, and serves as the deterministic tie-break when timestamps collide. Sorting on ingest would destroy this information permanently. |

### `raw_catalog`

| Field | Type | Rationale |
|---|---|---|
| `category_path` | varchar[] | `category_hash` split on `/`. |
| `category_lvl1/2/3` | varchar | Individual hierarchy levels, so category rollups are column lookups rather than repeated string parsing. |
| `category_depth` | bigint | Number of levels present. Not all SKUs have the full 3 — depth itself is a data-quality signal. |

### `raw_search`

| Field | Type | Rationale |
|---|---|---|
| `product_skus` | varchar[] | Parsed impression set. |
| `clicked_skus` | varchar[] | Parsed click set. |
| `file_row_idx` | bigint | As above. |

---

## Conventions used throughout

**Timestamps are converted in UTC, explicitly.** Absolute dates are meaningless
(week-shifted), so only day-of-week and hour-of-day carry information — which
makes *how* the epoch milliseconds are converted load-bearing.

The pipeline uses DuckDB's `epoch_ms()`, which returns a naive `TIMESTAMP`
interpreted as UTC. It deliberately does **not** use `to_timestamp()`, which
returns `TIMESTAMP WITH TIME ZONE` and from which `hour()` and `dayofweek()`
extract in the *session's* time zone. That would make the derived columns depend
on the machine running the pipeline: the same code on the same data gives
different answers on a laptop set to Asia/Calcutta than on a UTC server.

This was not hypothetical. The original implementation used `to_timestamp()`,
and switching to `epoch_ms()` changed `hour_of_day` for **100% of the 26.4M
staged events** and `day_of_week` for **34.6%** of them. The bug was caught by
`src/transform/verify_postgres.py`, which compares DuckDB against PostgreSQL and
flagged the two timestamp columns as disagreeing.

**What UTC hours do and do not mean.** The retailer's own time zone is not
disclosed, and the timestamps are week-shifted. So a UTC hour-of-day figure is
*not* the shopper's local clock time — "traffic peaks at 14:00 UTC" says nothing
about whether people shop at lunchtime. What survives is the **shape**: the
relative distribution across the day, and the contrast between hours, which is
unaffected by a constant offset. Any claim in this project about hour-of-day is
a claim about shape, never about local behaviour.

**Price.** `price_bucket` is ordinal. "Bucket 8 costs twice bucket 4" is not a
valid statement; "bucket 8 converts better than bucket 4" is.

**Unknown members.** SKUs missing from the catalog, and catalog rows missing
category or price, are assigned explicit `(unknown)` members rather than dropped.
Dropping them would bias every segmented metric toward well-maintained products.

**Two funnel definitions.** *Step-attained* (did the session ever reach this
stage?) and *strict-sequence* (did the stages occur in order?) are always
reported together. The gap between them measures cross-session behaviour rather
than hiding it.
