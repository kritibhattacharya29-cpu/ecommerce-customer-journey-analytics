# PostgreSQL serving layer — setup

The serving layer runs PostgreSQL **without an installer, without administrator
rights, and without a Windows service**. That is a deliberate constraint: this
project was built on a locked-down machine, and a portfolio project that only
reproduces if you happen to be a local admin is not reproducible.

The cluster listens on **localhost only**, on **port 5433**, so it cannot
collide with any PostgreSQL you already have.

---

## 1. Get the binaries

Download the **binaries-only ZIP** — not the installer — from
<https://www.enterprisedb.com/download-postgresql-binaries>.

This project was built against `postgresql-17.2-1-windows-x64-binaries.zip`
(297 MB). Extract it so that the layout is:

```
<COVEO_WORK_DIR parent>/
├── pgsql/            <- from the ZIP
│   └── bin/
│       ├── initdb.exe
│       ├── pg_ctl.exe
│       ├── postgres.exe
│       └── psql.exe
├── pgdata/           <- created by `pg.ps1 setup`
└── work/             <- the DuckDB warehouse
```

With the default `COVEO_WORK_DIR=D:/coveo-sigir/work`, that means extracting to
`D:\coveo-sigir\`.

Extraction takes a few minutes — the archive contains ~30,000 files, and
Windows' built-in `Expand-Archive` is slow with many small files. Any third-party
unzip tool is considerably faster.

---

## 2. Create and start the cluster

```powershell
.\scripts\pg.ps1 setup
```

This runs `initdb`, sets `listen_addresses = 'localhost'` and `port = 5433`,
switches TCP authentication to `scram-sha-256`, and records a generated password
in `.env` (which is gitignored — no credential is ever committed).

```powershell
.\scripts\pg.ps1 start
```

Also creates the `coveo_analytics` database if it does not exist.

Other commands: `stop`, `status`, `psql`.

---

## 3. Apply the schema

```bash
psql -h localhost -p 5433 -U postgres -d coveo_analytics -f sql/postgres/01_schema.sql
```

Creates `coveo.dim_product`, `coveo.fct_session`,
`coveo.fct_product_performance`, their indexes and CHECK constraints, and the
`v_funnel` / `v_category_performance` views.

---

## 4. Load and verify

```bash
python -m src.transform.load_postgres
```

```bash
python -m src.transform.verify_postgres
```

The verification step is the point. A load that finishes without error is not a
load that is correct — row counts can match while values are silently mangled.
`verify_postgres.py` runs 19 aggregates against **both** engines and compares
them, so the claim "the PostgreSQL layer works" rests on evidence.

It has already earned its place twice:

- It caught a **HUGEINT → float64** downcast, where DuckDB's `sum()` returns
  int128, pandas has no such dtype, and `COPY` was handed `"0.0"` for an
  `INTEGER` column.
- It caught a **timezone bug** that made `hour_of_day` depend on the machine's
  local time zone — wrong for 100% of events and, worse, not reproducible
  between machines.

Neither was visible from row counts alone.

---

## Performance notes

Loading `fct_session` (4.9M rows) takes about **157 seconds**, of which ~27s is
DuckDB writing CSV, ~59s is `COPY`, and ~41s is rebuilding indexes.

Getting there took two attempts and one wrong turn, recorded in
`src/transform/load_postgres.py`:

| Approach | Time |
|---|---|
| pandas DataFrames + `LIMIT/OFFSET` paging | 518.5s |
| Native DuckDB CSV export (pandas removed) | 507.1s |
| …plus dropping and rebuilding indexes around the load | **156.7s** |

The middle row is the instructive one. Profiling had attributed 58% of the time
to pandas, so removing pandas looked like the obvious fix — and it changed
almost nothing. The profile was measuring a `TEMP` table, which has no indexes,
so `COPY` appeared nearly free. On the real table, maintaining a 587 MB
primary-key index over a 64-char hash row-by-row was the actual cost.

A benchmark that omits the indexes is not a benchmark of the load.
