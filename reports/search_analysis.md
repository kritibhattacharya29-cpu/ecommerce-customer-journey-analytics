# Search Analytics — Coveo SIGIR 2021 eCommerce Dataset

Generated 2026-08-19 10:39 UTC from 819,516 events across 550,100 sessions.

> Dataset © Coveo Solutions Inc., released for the SIGIR 2021 eCom Data
> Challenge and used here under their research/educational Terms &
> Conditions. No row-level data is reproduced in this report — only
> aggregate counts and rates.

---

## Findings summary

| | Finding |
|---|---|
| 🔵 | 26.5% of searches return zero results (216,762 queries) — a large failure rate, but see §4: no measurable session-level conversion penalty. |
| 🔵 | CTR 27.9% computed over queries that returned results; zero-result queries excluded from the denominator. |
| 🔴 | 22% of search clicks land on position 1 — raw clicks encode ranker position, not relevance; any relevance model must correct for position bias. |
| 🟠 | 3.2% of search-clicked products are purchased in the same session; cross-session outcomes are not attributable. |
| 🔵 | Zero-result searches show NO session-level conversion penalty (3.57% vs 3.56%, +0.00 pp). Session is too coarse a grain; query-level next-action analysis is required. |

🔴 blocks analysis · 🟠 must be handled in modelling · 🔵 worth knowing

---

## 1. Search volume and failure rate

| Measure | Value | Rate |
|---|---|---|
| Search queries | 819,516 | 100% |
| Sessions issuing ≥1 search | 550,100 | — |
| **Zero-result queries** | 216,762 | **26.45%** |
| Queries returning results | 602,754 | 73.55% |
| Queries with ≥1 valid click | 168,453 | 20.56% |
| Median result-set size | 25 | — |
| Clicks discarded as phantom | 33,147 | — |

**Click-through rate: 27.95%** of queries
that returned anything received a click.

Note the denominator. CTR is computed over queries *that returned
results*, not all queries — a zero-result search cannot be clicked, and
including it would blend two different failures into one number that
improves when search gets worse.

The 216,762 zero-result queries are the most visible failure mode
here: each is a shopper who stated intent explicitly and received
nothing, and the remedies are concrete (synonym and redirect rules,
stocking decisions informed by what people ask for and cannot find).

It is tempting to price that directly as lost revenue. **§4 tests that
assumption and it does not hold** — sessions hitting a zero-result page
convert no worse than sessions that do not. The rate is a real quality
problem; the revenue impact is not established by this data at session
grain, and §4 sets out what would establish it.

## 2. Position bias

Where in the result list did shoppers click? This is only answerable
because Coveo logged the full impression set, not just the click.

| Result position | Clicks | % of clicks |
|---|---|---|
| 1 (first result) | 78,051 | 21.74 |
| 2 | 48,904 | 13.62 |
| 3 | 31,964 | 8.90 |
| 4-5 | 46,506 | 12.95 |
| 6-10 | 68,276 | 19.02 |
| 11-20 | 67,003 | 18.66 |
| 21+ | 18,336 | 5.11 |

Median clicked position: **4**; 90th percentile:
17.

**21.7% of all clicks land on the first result, and
44.3% land in the top three.**

This is the central caveat for any relevance work on this data: a
product's click count measures *where the ranker put it* at least as
much as how relevant it was. Training a relevance model on raw clicks
therefore teaches it to reproduce the existing ranking, including its
mistakes. Correcting for position — by modelling propensity, or by
comparing products that appeared at similar ranks — is a prerequisite,
not a refinement.

## 3. Search click → outcome

Following each clicked SKU into the same session's browsing stream:

| Outcome for a clicked product | Occurrences | % of search clicks |
|---|---|---|
| Clicked from search results | 230,140 | 100% |
| …later viewed (product detail) in the session | 32,098 | 13.95% |
| …later added to cart | 22,786 | 9.90% |
| …later purchased | 7,254 | **3.15%** |

This joins the search and browsing files on `(session, SKU)`, which is
the only linkage available between them — there is no event ID tying a
click to a subsequent pageview.

Two consequences follow, and both limit the claim. Attribution is
**within-session only**: a product clicked today and bought tomorrow
appears here as a failure, and the quality assessment already showed
13.3% of purchases occur in a different session from the add. And the
join cannot distinguish a view *caused by* the search click from one the
shopper would have reached anyway. These are association rates, not
causal effects, and the honest ceiling on this analysis is that it
describes co-occurrence within a session.

## 4. What does a zero-result search cost? — a null result

| Segment | Sessions | Conversion |
|---|---|---|
| All searching sessions | 474,117 | 3.56% |
| …that never hit a zero-result page | 329,052 | **3.56%** |
| …that hit ≥1 zero-result page | 145,065 | **3.57%** |

**Difference: +0.00 percentage points.** On 145,065 sessions
that is indistinguishable from zero.

This is not the result §1 leads you to expect, and it is worth stating
plainly rather than burying: **at session level, hitting a zero-result
search has no measurable effect on whether the session converts.**

### Why the obvious story fails

Three explanations are consistent with this, and they are not mutually
exclusive:

1. **Shoppers recover.** A zero-result page is a minor speed bump —
   they reformulate the query, or navigate by category instead, and
   carry on. Search is one route to a product, not the only one.
2. **Selection cancels the damage.** Hitting a zero-result page is
   mostly a function of *searching a lot*, and sessions that search a
   lot are higher-intent to begin with. Higher baseline intent and the
   cost of the failure push in opposite directions.
3. **The unit of analysis is wrong.** A session is far too coarse. One
   empty result among fifteen searches is diluted to invisibility by
   the fourteen that worked.

### What would actually answer the question

The session is the wrong grain. The right test is at **query level**:
for each query, what happens in the next few events — does the shopper
reformulate, switch to category browsing, or stop? Comparing the
immediate next action after a zero-result query against the same after
a successful one isolates the effect without diluting it across a whole
session.

That requires interleaving search events into the browsing timeline on
`(session, timestamp)` — which the staging layer already makes possible,
since both tables carry a deterministic sequence. It is the natural next
piece of work, and it is the honest answer to "how much does this cost",
which the table above does **not** establish.
