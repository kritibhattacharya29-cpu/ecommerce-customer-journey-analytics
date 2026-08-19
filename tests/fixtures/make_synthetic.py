"""Generate synthetic Coveo-shaped CSVs for testing.

Coveo's Terms & Conditions forbid redistributing "the Dataset and/or data
contained therein", which rules out committing even a handful of real rows as a
test fixture. So the fixtures are fabricated from scratch.

They are not merely schema-compatible — they deliberately reproduce every
pathology measured in the real data, so the pipeline's handling of each one is
actually exercised:

  * events written to the file out of chronological order
  * multiple events colliding on a single millisecond
  * a PDP emitting both `detail` and `pageview` at one timestamp
  * consecutive duplicate `remove` events
  * a purchase with no add-to-cart (simulated cross-session cart)
  * intra-session gaps exceeding the nominal 30-minute session rule
  * browsed SKUs absent from the catalog
  * catalog rows present but entirely empty
  * a search click on a SKU that was never in the result set
  * searches returning nothing

Usage:
    python tests/fixtures/make_synthetic.py --out tests/fixtures/data
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import random
from pathlib import Path

BASE_TS = 1_550_000_000_000  # arbitrary epoch-ms anchor
MINUTE = 60_000


def h(prefix: str, i: int) -> str:
    """Deterministic 64-char hex, same shape as Coveo's hashes."""
    return hashlib.sha256(f"{prefix}-{i}".encode()).hexdigest()


def make_catalog(n_skus: int, rng: random.Random) -> list[dict]:
    """~half the rows fully populated, ~half entirely empty (as in the real data)."""
    rows = []
    for i in range(n_skus):
        sku = h("sku", i)
        if rng.random() < 0.5:
            cat = "/".join(h("cat", rng.randrange(3)) for _ in range(3))
            rows.append({
                "product_sku_hash": sku,
                "description_vector": "[{}]".format(
                    ", ".join(f"{rng.gauss(0, 1):.6f}" for _ in range(8))),
                "category_hash": cat,
                "image_vector": "[{}]".format(
                    ", ".join(f"{rng.gauss(0, 100):.6f}" for _ in range(8))),
                "price_bucket": f"{rng.randint(1, 10)}.0",
            })
        else:
            rows.append({"product_sku_hash": sku, "description_vector": "",
                         "category_hash": "", "image_vector": "", "price_bucket": ""})
    return rows


def make_browsing(n_sessions: int, n_skus: int, rng: random.Random) -> list[dict]:
    rows: list[dict] = []

    for s in range(n_sessions):
        sid = h("session", s)
        t = BASE_TS + s * 3_600_000
        session_rows: list[dict] = []

        def emit(event_type, action, sku, ts, url):
            session_rows.append({
                "session_id_hash": sid,
                "event_type": event_type,
                "product_action": action or "",
                "product_sku_hash": sku or "",
                "server_timestamp_epoch_ms": str(ts),
                "hashed_url": url,
            })

        # landing pageview
        emit("pageview", "", "", t, h("url", rng.randrange(20)))
        t += rng.randint(2_000, 20_000)

        viewed = []
        for _ in range(rng.randint(1, 4)):
            sku = h("sku", rng.randrange(n_skus))
            url = h("url", rng.randrange(20))
            viewed.append(sku)
            # PDP double-fire: detail + pageview share one millisecond
            emit("event_product", "detail", sku, t, url)
            emit("pageview", "", "", t, url)
            t += rng.randint(3_000, 60_000)

        added = []
        for sku in viewed:
            if rng.random() < 0.35:
                emit("event_product", "add", sku, t, h("url", rng.randrange(20)))
                added.append(sku)
                t += rng.randint(1_000, 30_000)

        # consecutive duplicate remove events
        if added and rng.random() < 0.15:
            sku = rng.choice(added)
            for _ in range(rng.randint(2, 3)):
                emit("event_product", "remove", sku, t, h("url", rng.randrange(20)))
            added.remove(sku)
            t += rng.randint(1_000, 10_000)

        # a gap that violates the nominal 30-minute session rule
        if rng.random() < 0.05:
            t += 45 * MINUTE
            emit("pageview", "", "", t, h("url", rng.randrange(20)))
            t += 5_000

        if added and rng.random() < 0.4:
            for sku in added:
                emit("event_product", "purchase", sku, t, h("url", rng.randrange(20)))
                t += 500

        # cross-session cart: purchase with no add anywhere in this session
        elif rng.random() < 0.04:
            emit("event_product", "purchase", h("sku", rng.randrange(n_skus)),
                 t, h("url", rng.randrange(20)))

        # SKUs that exist in browsing but never in the catalog
        if rng.random() < 0.03:
            emit("event_product", "detail", h("orphan-sku", rng.randrange(50)),
                 t, h("url", rng.randrange(20)))

        # exact duplicate row (double-fired tracking pixel)
        if session_rows and rng.random() < 0.05:
            session_rows.append(dict(rng.choice(session_rows)))

        # THE headline pathology: shuffle so file order != chronological order
        if rng.random() < 0.3:
            rng.shuffle(session_rows)

        rows.extend(session_rows)

    # interleave sessions so the file isn't conveniently grouped either
    rng.shuffle(rows)
    return rows


def make_search(n_queries: int, n_sessions: int, n_skus: int,
                rng: random.Random) -> list[dict]:
    rows = []
    for q in range(n_queries):
        sid = h("session", rng.randrange(n_sessions))
        ts = BASE_TS + rng.randrange(n_sessions) * 3_600_000 + rng.randint(0, 600_000)
        qv = "[{}]".format(", ".join(f"{rng.gauss(0, 0.1):.6f}" for _ in range(8)))

        if rng.random() < 0.08:                       # zero-result search
            products, clicked = [], []
        else:
            products = [h("sku", rng.randrange(n_skus))
                        for _ in range(rng.randint(1, 12))]
            if rng.random() < 0.25:
                clicked = [rng.choice(products)]
                if rng.random() < 0.1:                # phantom click
                    clicked = [h("sku", rng.randrange(n_skus, n_skus + 20))]
            else:
                clicked = []

        rows.append({
            "session_id_hash": sid,
            "query_vector": qv,
            "clicked_skus_hash": repr(clicked) if clicked else "",
            "product_skus_hash": repr(products) if products else "",
            "server_timestamp_epoch_ms": str(ts),
        })
    return rows


def write(path: Path, rows: list[dict], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    print(f"  {path.name:<24} {len(rows):>8,} rows")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="tests/fixtures/data")
    ap.add_argument("--sessions", type=int, default=500)
    ap.add_argument("--skus", type=int, default=200)
    ap.add_argument("--queries", type=int, default=300)
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    rng = random.Random(args.seed)
    out = Path(args.out)

    print(f"Generating synthetic fixtures -> {out}/")
    write(out / "sku_to_content.csv", make_catalog(args.skus, rng),
          ["product_sku_hash", "description_vector", "category_hash",
           "image_vector", "price_bucket"])
    write(out / "browsing_train.csv",
          make_browsing(args.sessions, args.skus, rng),
          ["session_id_hash", "event_type", "product_action",
           "product_sku_hash", "server_timestamp_epoch_ms", "hashed_url"])
    write(out / "search_train.csv",
          make_search(args.queries, args.sessions, args.skus, rng),
          ["session_id_hash", "query_vector", "clicked_skus_hash",
           "product_skus_hash", "server_timestamp_epoch_ms"])
    print("\nSynthetic only — contains no Coveo data.")


if __name__ == "__main__":
    main()
