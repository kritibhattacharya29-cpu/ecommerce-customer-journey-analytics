# Notebooks

Exploratory analysis. Two rules, both driven by Coveo's Terms & Conditions:

1. **Clear all outputs before committing.** Cell output can contain row-level
   data — `session_id_hash` values, SKU hashes, sample rows — and clause 5
   forbids redistributing "the Dataset and/or data contained therein". A
   committed notebook with live output is a redistribution.

2. **Aggregate results only.** Charts, counts, rates and model coefficients are
   fine. `df.head()` is not.

Reproducible analysis belongs in `src/` and `sql/`; notebooks are for
exploration, and anything worth keeping should graduate into a script.
