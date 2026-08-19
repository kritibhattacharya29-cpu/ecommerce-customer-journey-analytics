"""Execute the DuckDB transformation SQL in order.

The transformation logic lives in .sql files rather than embedded strings so it
stays readable, reviewable and runnable by hand in a DuckDB shell. This module
is only the runner.

Usage:
    python -m src.transform.build_model              # run all stages
    python -m src.transform.build_model --stage 01   # run one stage
"""
from __future__ import annotations

import argparse
import re
import time
from pathlib import Path

from src import config
from src.ingest.build_duckdb import connect

SQL_DIR = config.REPO_ROOT / "sql" / "duckdb"


def split_statements(sql: str) -> list[str]:
    """Split on semicolons that end a statement, ignoring those in comments.

    Deliberately simple: the project's SQL contains no semicolons inside string
    literals or dollar-quoted blocks, so stripping comments first is sufficient.
    """
    without_line_comments = re.sub(r"--[^\n]*", "", sql)
    return [s.strip() for s in without_line_comments.split(";") if s.strip()]


def run_file(con, path: Path) -> None:
    print(f"\n{path.name}")
    statements = split_statements(path.read_text(encoding="utf-8"))
    for stmt in statements:
        # name the target table for progress output
        m = re.search(r"CREATE\s+(?:OR\s+REPLACE\s+)?TABLE\s+(\w+)", stmt, re.I)
        label = m.group(1) if m else stmt.split()[0].lower()
        print(f"  [{label}] ...", end="", flush=True)
        t0 = time.time()
        con.execute(stmt)
        elapsed = time.time() - t0
        if m:
            n = con.execute(f"SELECT count(*) FROM {m.group(1)}").fetchone()[0]
            print(f" {n:>12,} rows  {elapsed:>7,.1f}s")
        else:
            print(f" {elapsed:>7,.1f}s")


def main() -> None:
    ap = argparse.ArgumentParser(description="Build the DuckDB analytical model.")
    ap.add_argument("--stage", help="Run only files starting with this prefix, e.g. 01")
    args = ap.parse_args()

    files = sorted(SQL_DIR.glob("*.sql"))
    if args.stage:
        files = [f for f in files if f.name.startswith(args.stage)]
    if not files:
        raise SystemExit(f"No SQL files matched in {SQL_DIR}")

    con = connect()
    t0 = time.time()
    for f in files:
        run_file(con, f)
    print(f"\nTotal {time.time() - t0:,.1f}s")

    print("\nModel tables:")
    rows = con.execute("""
        SELECT table_name, estimated_size
        FROM duckdb_tables()
        WHERE table_name NOT LIKE 'raw_%'
        ORDER BY table_name
    """).fetchall()
    for name, n in rows:
        print(f"  {name:<28} ~{int(n):>13,} rows")
    con.close()


if __name__ == "__main__":
    main()
