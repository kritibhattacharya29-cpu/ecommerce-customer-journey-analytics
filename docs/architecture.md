# Architecture & engineering decisions

Each section records a decision, the constraint that forced it, and what the
alternative would have cost. The point of writing it down is that most of these
choices are invisible in the finished numbers but entirely determine whether
those numbers are right.

---

## 1. Two engines, not one

**Decision.** DuckDB performs the raw→staging→mart transformation. PostgreSQL
holds only the finished marts.

**Why.** The raw event stream is 36,079,307 rows / 7.8 GB of CSV. Loading that
into PostgreSQL on a laptop means an hours-long `COPY`, ~20 GB on disk with
indexes, and a multi-minute wait for every iteration on the transformation
logic. DuckDB scans the same data column-wise in seconds and required no server
to install.

But a columnar embedded database is not a serving layer. PostgreSQL gets the
modelled output — a few million session rows and 66k product rows — where it is
genuinely good: indexed lookups, constraints, views, and a stable endpoint for
Power BI.

**Cost of the alternative.** Postgres-only would have made the iteration loop
unworkable. DuckDB-only would have meant no serving layer, no constraints, and
no honest claim to PostgreSQL on the project.

---

## 2. File order is captured, not discarded

**Decision.** `file_row_idx` is materialised at ingest, before any sort.

**Why.** Coveo warn the file is "not strictly chronological". Sorting on the way
in would resolve that warning and simultaneously destroy the only evidence that
could quantify it.

Keeping physical order let the assessment establish two things that changed the
design:

- File order is in fact **99.995% chronological** — only 269 sessions contain a
  genuine backwards step. The documented warning is real but far smaller than it
  sounds, which is exactly what makes it dangerous: rare enough to survive a
  spot-check, frequent enough to corrupt aggregates.
- **Timestamps are not a total order.** 9,763,612 `(session, millisecond)`
  collisions exist, 99.4% of them the documented PDP `detail`+`pageview`
  double-fire.

The second finding is the operationally important one. `ORDER BY timestamp`
alone is non-deterministic across runs, so `file_row_idx` became the stable
tie-break in every sequencing operation.

---

## 3. Vectors live in separate tables

**Decision.** `query_vector`, `description_vector` and `image_vector` are
ingested only behind `--with-vectors`.

**Why.** They are roughly 1.6 GB of the 1.7 GB search file and most of the
catalog file, and no funnel, abandonment, search-conversion or category question
touches them. Excluding them from the hot tables makes every analytical scan
cheaper by roughly an order of magnitude. They remain available for the ML
layer, where they are the entire point.

---

## 4. Unknowns are members, not NULLs

**Decision.** SKUs absent from the catalog, and catalog rows missing category or
price, get explicit `(unknown)` dimension members.

**Why.** 51.7% of catalog SKUs have no price bucket. A `GROUP BY category` with
an inner join would produce a clean-looking report covering half the business,
and nothing in the output would indicate the omission. Explicit unknown members
make the gap visible in every breakdown, and coverage percentages are reported
next to every price statistic.

---

## 5. Two funnel definitions, always reported together

**Decision.** `fct_session` materialises both `reached_*` (step-attained) and
`strict_*` (strict-sequence) flags.

**Why.** 13.3% of purchasing sessions contain no add-to-cart event. These are
not errors: Coveo's 30-minute session rule splits a cart built in one session
from the checkout that happens in the next, so the add and the purchase land
under different session IDs.

A strict sequential funnel discards those conversions and understates revenue by
13%. A step-attained funnel counts them but overstates within-session intent.
Neither is correct alone. Materialising both makes the discrepancy a reportable
quantity — the measured rate of cross-session shopping — rather than an artefact
hidden inside whichever definition was chosen.

---

## 6. Two-stage deduplication

**Decision.** Duplicate detection hashes each row to a 64-bit fingerprint,
groups on that, then re-checks only the colliding subset with full column
equality.

**Why.** Grouping directly on the six source columns means a ~200-byte key
(three 64-char hashes) over 36M rows — roughly 7 GB of grouping keys. On an 8 GB
machine that spills catastrophically; measured at **132 seconds** in the
profiler. The fingerprint approach shrinks the key to 8 bytes and ran in **32
seconds**, a 4.1× improvement, while the exact re-check on the colliding subset
preserves correctness against hash collisions.

The result being sought is 1,800 duplicate rows out of 36 million. Spending a
7 GB hash table to find them was the wrong shape of solution.

---

## 7. Memory limit set below available RAM

**Decision.** DuckDB's `memory_limit` defaults to ~30% of physical RAM
(2 GB on the 7.8 GB development machine), with spill directed to
`COVEO_WORK_DIR`.

**Why.** An over-generous limit is actively counterproductive. Claiming memory
the OS then has to swap makes the entire machine unresponsive and the query
*slower*, not faster — observed directly during development, with free RAM at
0.1 GB and the process's working set paged out. DuckDB spilling deliberately to
a known temp directory is orderly, bounded and observable.

**Known limitation.** Even so, building `stg_browsing` on the full 36M events
requires two sorts of ~7 GB each and takes tens of minutes on this hardware,
peaking around 12 GB of temp space. The next optimisation is surrogate integer
keys: replacing the three 64-char hash columns with `INTEGER` dimension keys
would cut the sort payload from ~200 bytes to ~30 bytes per row, bringing the
sort within RAM. That is also the textbook star-schema design, so it improves
the model and the performance together.

---

## 8. Heavy artefacts live outside the repo

**Decision.** Raw CSVs and the DuckDB warehouse live under `COVEO_WORK_DIR`,
outside the repository.

**Why.** Two independent reasons. Coveo's licence forbids redistributing the
data, so it must never be committable by accident. And the repository sits in a
OneDrive-synced folder, where a multi-gigabyte database would be re-uploaded on
every write and `.git` is exposed to sync-induced corruption.

---

## 9. Tests run on synthetic data

**Decision.** `tests/fixtures/make_synthetic.py` fabricates Coveo-shaped data
reproducing every measured pathology; no real row is ever committed.

**Why.** Clause 5 of the Terms & Conditions forbids distributing "the Dataset
and/or data contained therein", which rules out committing even a handful of
real rows as a fixture. Without synthetic fixtures the pipeline would be
untestable by anyone who does not already have the data.

The fixtures are not merely schema-compatible — they deliberately contain
out-of-order events, millisecond collisions, PDP double-fires, duplicate rows,
cross-session purchases, over-long session gaps, uncatalogued SKUs, phantom
search clicks and zero-result searches, so that all 19 assertions exercise real
handling rather than a happy path.

---

## 10. Timestamps are converted in UTC, explicitly

**Decision.** Epoch milliseconds are converted with DuckDB's `epoch_ms()`, never
`to_timestamp()`.

**Why.** `to_timestamp()` returns `TIMESTAMP WITH TIME ZONE`, and `hour()` /
`dayofweek()` extract from it in the *session's* time zone. That makes derived
columns a property of the machine running the pipeline rather than of the data.

This was not theoretical. The original implementation used `to_timestamp()` on a
laptop set to Asia/Calcutta. Switching to `epoch_ms()` — a naive `TIMESTAMP`
interpreted as UTC — changed `hour_of_day` for **100% of the 26.4M staged
events** and `day_of_week` for **34.6%** of them. The same code, on the same
data, produced different answers depending on where it ran.

**How it was caught.** Not by a test, and not by inspection. The cross-engine
verification in §11 flagged the two timestamp columns as disagreeing between
DuckDB and PostgreSQL, which led back to the conversion function.

**What survives.** The retailer's own time zone is not disclosed and the
timestamps are week-shifted, so a UTC hour is not the shopper's local clock.
What is interpretable is the *shape* — the relative distribution across the day —
which a constant offset does not change. Every hour-of-day claim in this project
is a claim about shape.

---

## 11. The serving layer is verified, not assumed

**Decision.** `src/transform/verify_postgres.py` runs 19 aggregates against both
DuckDB and PostgreSQL and compares them.

**Why.** A load that completes without error is not a load that is correct. Row
counts can match while values are silently mangled, and every mangling mode is
invisible from a `count(*)`.

It has already paid for itself twice:

- **HUGEINT → float64.** DuckDB's `sum()` over an integer returns int128 so it
  cannot overflow. pandas has no int128 dtype, so the column arrived as float64
  and `to_csv` wrote `"0.0"`, which PostgreSQL rejects for an `INTEGER` column.
  Fixed with `::BIGINT` casts at the source, plus a guard in the loader that
  names the offending column instead of failing inside `COPY`.
- **The timezone bug in §10**, which nothing else would have surfaced.

---

## 12. Bulk loading drops indexes first

**Decision.** For tables above ~1M rows, the loader captures index and
constraint DDL, drops them, runs `COPY`, then rebuilds.

**Why, and the wrong turn on the way.** Loading `fct_session` (4.9M rows)
originally took **518.5s** using pandas DataFrames and `LIMIT/OFFSET` paging.

Profiling attributed 58% of that to `pandas.to_csv`, so the obvious fix was to
bypass pandas and let DuckDB write the CSV natively. That produced **507.1s** —
a 2% improvement.

The profile was wrong in an instructive way: it measured `COPY` into a `TEMP`
table, which has no indexes, making `COPY` look nearly free. On the real table,
maintaining a **587 MB primary-key index over a 64-char hash** one row at a time
was the actual cost.

| Approach | Time |
|---|---|
| pandas + `LIMIT/OFFSET` | 518.5s |
| Native DuckDB CSV export | 507.1s |
| …plus drop/rebuild indexes | **156.7s** |

CHECK constraints are deliberately *not* dropped. They cost almost nothing per
row, and waving through a load that violates the funnel invariants to save a few
seconds would defeat the purpose of having them.

**The transferable lesson:** a benchmark that omits the indexes is not a
benchmark of the load.
