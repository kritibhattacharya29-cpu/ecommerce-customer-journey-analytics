# Obtaining the dataset

**The dataset is not in this repository and will not be added to it.**

This project analyses the *Coveo SIGIR 2021 eCommerce Data Challenge* dataset.
Coveo release it for research and educational use under Terms & Conditions that
you must accept individually. You need to obtain your own copy.

---

## Why the data isn't here

Coveo's Terms & Conditions, clause 5:

> **Redistribution.** You will not distribute copies of the Dataset and/or data
> contained therein to any third party, which includes but is not limited to
> your research associates and colleagues.

That wording is broader than it first appears. "**and/or data contained
therein**" rules out not just the three CSVs but also:

- a "small sample" of real rows committed as a test fixture,
- a notebook with real `session_id_hash` values visible in cell output,
- a CSV export of row-level results,
- a Parquet extract of "just the purchases".

This repository therefore commits **only aggregates** — counts, rates,
distributions and model coefficients. Concretely:

| Practice | Status |
|---|---|
| `data/` directories | gitignored, contents never committed |
| `*.csv`, `*.parquet`, `*.duckdb` | gitignored globally |
| Test fixtures | **synthetically generated** (`tests/fixtures/`), never sampled from the real data |
| Notebook outputs | stripped of row-level identifiers before commit |
| Reported figures | aggregate statistics only |

Clause 2 additionally prohibits any attempt to de-anonymise the data. Nothing
in this project joins the dataset against any external source, which is the
only route by which a hashed identifier could become identifying.

---

## How to get it

1. Go to the official challenge repository:
   <https://github.com/coveooss/SIGIR-ecom-data-challenge>
2. Follow the link there to the data request form.
3. Complete the form with your details and institution, and accept the Terms &
   Conditions.
4. Coveo send a download link for `SIGIR-ecom-data-challenge.zip` (~1.9 GB).
5. Unzip it. You should have:

   ```
   SIGIR-ecom-data-challenge/
   ├── LICENSE                    <- the Terms & Conditions
   ├── README
   └── train/
       ├── browsing_train.csv     ~6.0 GB
       ├── search_train.csv       ~1.7 GB
       └── sku_to_content.csv     ~71 MB
   ```

6. Point the project at it — copy `.env.example` to `.env` and set:

   ```
   COVEO_RAW_DIR=/absolute/path/to/SIGIR-ecom-data-challenge/train
   ```

Do not rename, edit, or re-save the CSVs. The pipeline treats them as an
immutable raw layer and never writes to them; keeping them pristine means the
whole analysis can be reproduced from source at any time.

### Disk requirements

| Item | Size |
|---|---|
| Raw CSVs | ~7.8 GB |
| DuckDB warehouse (without vectors) | ~2–3 GB |
| DuckDB warehouse (with `--with-vectors`) | ~8 GB |
| Spill space during large sorts | up to ~4 GB transient |

Set `COVEO_WORK_DIR` in `.env` to a location with room, **outside any cloud-sync
folder** (OneDrive, Dropbox, iCloud). A multi-gigabyte database inside a sync
root gets re-uploaded on every write.

---

## Attribution

Clause 6 requires attribution to Coveo in any publication, project or
presentation based on the dataset. This project attributes Coveo in the README,
in the generated data-quality report, and by the citation below.

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

Attribution does not imply endorsement by Coveo, and clause 7 does not grant
permission to use Coveo's trade names or marks beyond describing the origin of
the data — which is the only use made of the name here.

This project is non-commercial and educational, consistent with clause 1.
