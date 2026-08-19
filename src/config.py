"""Central configuration, loaded from .env.

Every path in the project resolves through here so the repo stays portable:
the raw Coveo CSVs live outside the repo (and outside any cloud-sync folder),
and their location is the single thing a new user has to set.
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(REPO_ROOT / ".env")

# --- Raw data ---------------------------------------------------------------
RAW_DIR = Path(os.getenv("COVEO_RAW_DIR", "")).expanduser()

BROWSING_CSV = RAW_DIR / "browsing_train.csv"
SEARCH_CSV = RAW_DIR / "search_train.csv"
CATALOG_CSV = RAW_DIR / "sku_to_content.csv"

RAW_FILES = {
    "browsing": BROWSING_CSV,
    "search": SEARCH_CSV,
    "catalog": CATALOG_CSV,
}

# --- Derived / working data -------------------------------------------------
# The warehouse and DuckDB's spill files deliberately live OUTSIDE the repo.
# The repo sits in a OneDrive-synced folder; a multi-GB database and its
# temp files there would be uploaded to the cloud on every write, and .git
# inside a sync root is a known source of corruption. Keeping heavy artefacts
# in WORK_DIR keeps the repo small, fast and safe to sync.
WORK_DIR = Path(os.getenv("COVEO_WORK_DIR", "C:/data/coveo-sigir/work")).expanduser()
INTERIM_DIR = WORK_DIR / "tmp"          # DuckDB spill target for large sorts
PROCESSED_DIR = WORK_DIR                # the warehouse itself

# Small, publishable artefacts stay in the repo.
DATA_DIR = REPO_ROOT / "data"
REPORTS_DIR = REPO_ROOT / "reports"
FIGURES_DIR = REPORTS_DIR / "figures"

# An absolute DUCKDB_PATH in .env overrides the default; pathlib's `/` returns
# the right-hand operand unchanged when it is already absolute.
DUCKDB_PATH = WORK_DIR / os.getenv("DUCKDB_PATH", "coveo.duckdb")

# --- PostgreSQL serving layer ----------------------------------------------
PG = {
    "host": os.getenv("PGHOST", "localhost"),
    "port": int(os.getenv("PGPORT", "5432")),
    "dbname": os.getenv("PGDATABASE", "coveo_analytics"),
    "user": os.getenv("PGUSER", "postgres"),
    "password": os.getenv("PGPASSWORD", ""),
}


def pg_url() -> str:
    return (
        f"postgresql+psycopg2://{PG['user']}:{PG['password']}"
        f"@{PG['host']}:{PG['port']}/{PG['dbname']}"
    )


# --- Engine tuning ----------------------------------------------------------
def _default_memory_limit() -> str:
    """Pick a DuckDB memory limit that leaves the OS room to breathe.

    Deliberately conservative: DuckDB spilling to disk is orderly and
    predictable, whereas the OS swapping under memory pressure is not. On a
    small machine an over-generous limit makes the whole system unresponsive
    and the query slower, not faster.
    """
    try:
        import ctypes

        class MemStatus(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong),
                        ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong),
                        ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong),
                        ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong),
                        ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

        stat = MemStatus()
        stat.dwLength = ctypes.sizeof(MemStatus)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
        total_gb = stat.ullTotalPhys / (1024 ** 3)
    except Exception:
        try:
            total_gb = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / (1024 ** 3)
        except Exception:
            return "2GB"
    # ~30% of physical RAM, clamped to a sane band
    return "{0:.0f}GB".format(max(1, min(16, round(total_gb * 0.30))))


DUCKDB_MEMORY_LIMIT = os.getenv("DUCKDB_MEMORY_LIMIT") or _default_memory_limit()

# --- Sampling ---------------------------------------------------------------
SAMPLE_SESSION_FRACTION = float(os.getenv("SAMPLE_SESSION_FRACTION", "0.01"))

# Coveo's documented session rule: events >30 min apart start a new session.
SESSION_GAP_MINUTES = 30


def verify_raw_data() -> None:
    """Fail loudly and helpfully if the dataset isn't where .env says."""
    if not RAW_DIR or str(RAW_DIR) == ".":
        raise SystemExit(
            "COVEO_RAW_DIR is not set.\n"
            "Copy .env.example to .env and point COVEO_RAW_DIR at the folder\n"
            "containing the three unzipped Coveo CSVs. See docs/DATA_ACCESS.md."
        )
    missing = [name for name, path in RAW_FILES.items() if not path.exists()]
    if missing:
        raise SystemExit(
            f"Missing raw file(s): {', '.join(missing)}\n"
            f"Looked in: {RAW_DIR}\n"
            "The Coveo dataset must be obtained directly from Coveo under their\n"
            "Terms & Conditions -- see docs/DATA_ACCESS.md."
        )


def ensure_dirs() -> None:
    for d in (INTERIM_DIR, PROCESSED_DIR, FIGURES_DIR):
        d.mkdir(parents=True, exist_ok=True)
