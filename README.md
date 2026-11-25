# merger-arb-lab

Research, risk and manually approved execution tooling for merger arbitrage. The first case is **Marvell (MRVL) /
Lumentum (LITE)**: an all-stock deal at a fixed 4.5 MRVL per LITE share, announced 2026-11-02, expected to close in
1H-2028.

> **Verdict (2026-11-24 close): unattractive at a 21.5% spread, so WATCH.** The spread is real: the terms come from
> the merger agreement, and prices were checked against two independent sources. It is wider than every comparable
> completed deal at the same stage. But it roughly compensates for the risks: China's SAMR review, both shareholder
> votes, a crash regime, and above all the price LITE would fall back to if the deal broke. At base assumptions the
> expected return is only about 1 point a year above Treasuries, and under stress it is negative. Full reasoning,
> numbers and sources: **[docs/REPORT.md](docs/REPORT.md)**. Research only, not investment advice.

Interactive version: https://rasmuscodes.github.io/projects/merger-arb/ (static, no trading).

## What is in here
| Path | What |
|---|---|
| `deals/mrvl_lite.yaml` | Verified deal terms with section-level citations: ratio, collar, dates, outside-date chain, fees, vote standards, efforts covenant, dividend policy. A new deal is a new YAML file. |
| `mergerarb/analytics/` | Spread maths and history stats; carry (dividends, borrow, IBKR interest tiers, commissions); standalone (break) prices from pre-deal factor models; probabilities (break-even, market-implied, bottom-up); IBKR Reg T margin and survival-first sizing; 9+ path-simulated scenarios; Monte Carlo with model uncertainty; hedge-ratio comparison; trade/watch/no-trade signal. |
| `mergerarb/execution/` | Order export (JSON/CSV) and a local-only IBKR trading service: paper by default, typed approval, short-first leg management, partial-fill and unhedged-leg alerts, journal. See **[docs/EXECUTION.md](docs/EXECUTION.md)**. |
| `app/streamlit_app.py` | Local dashboard: all research views, plus an optional Execution tab that talks to the local service. |
| `web/` | Public static dashboard. A JS port of the model, checked against Python outputs (`web/data/parity.json`: 102 checks, max difference 0). |
| `scripts/` | Data pulls (Yahoo, Treasury, FINRA short interest, option chains), analysis runs, report charts, web snapshot, synthetic sample data, CRSP comparables. |
| `tests/` | 50 tests: exchange-ratio, P&L, carry and margin maths; quote-freshness labels; scenarios; signal rules; Monte Carlo; execution safety (approval, duplicates, partial fills, imbalance, rejection, reconnect, service auth); dashboard smoke test; full offline pipeline. |

## Quick start
```
python -m venv .venv
.venv\Scripts\activate             # Windows (source .venv/bin/activate elsewhere)
pip install -r requirements.txt
pip install -e .
pytest                             # 50 tests; the dashboard smoke test is skipped without a local data snapshot
```
**Offline (synthetic sample, labelled everywhere):**
```
MERGERARB_DATA=sample streamlit run app/streamlit_app.py
```
**With fresh data** (unofficial Yahoo endpoints; research use; raw files stay local in `data/raw/`):
```
python scripts/pull_yahoo.py
python scripts/pull_market_extras.py      # Treasury curve, FINRA short interest, option chains
python scripts/run_analysis.py            # console summary + outputs/
python scripts/build_report_assets.py     # docs/figures + docs/report_numbers.json
python scripts/build_web.py               # web/data snapshot + parity vectors
streamlit run app/streamlit_app.py
```
Borrow fees: IBKR's published short-stock availability (via iBorrowDesk, or TWS's Short Stock Availability window) is
entered as an input. The execution service reads shortability live from IBKR (tick 236).

## Data: live, delayed and historical
- **Quotes are labelled by real freshness.** "real-time" appears only for a recent regular-session print from a source
  that declares itself real-time (Yahoo's is Nasdaq last sale only, not the consolidated tape). Everything else is
  "delayed N min", "closing print", "stale" or "unavailable". Crossed or stale after-hours bid/ask pairs are flagged
  and ignored.
- **Committed:** derived data only (the spread series, model estimates, CRSP-derived comparables statistics), the
  synthetic sample, and the public-domain Treasury curve. **Not committed:** raw Yahoo or option snapshots, raw
  CRSP data, execution journals, tokens, order exports.

## Method in one paragraph
- **Payoffs.** Completion pays the dollar spread minus carry ($101 per LITE share), whatever MRVL does.
- **Break price.** A break sends LITE to its standalone value. That value is projected from the Oct-28 unaffected
  price with pre-deal factor models, not reset to the unaffected price, because the sector has since crashed 25-50%.
  MRVL recovers part of its estimated 21% deal discount.
- **Probability.** Break-even completion probability is -B/(C-B). Completion probability is decomposed into the two
  votes, regulators and other risks (labelled judgment). The Monte Carlo draws outcomes and dates, MRVL and LITE
  standalone paths, and spread dynamics (bridges for completion, widening before breaks). It runs every path through
  an IBKR-style margin account.
- **Sizing.** Survival-first: worst plausible stress loss ≤ 10% of equity, excess liquidity ≥ 50%, no margin call in
  extreme stresses, and a 95% expected shortfall ≤ 20% of equity.

## Licence
MIT (code). Data sources keep their own terms; see docs/REPORT.md for provenance.
