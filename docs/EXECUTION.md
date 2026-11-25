# IBKR execution: design, safety and setup

The execution tooling exists so that a ratio-hedged pair can be **exported exactly** and, if you choose, **sent to
your own TWS / IB Gateway after explicit approval**. It never trades on its own, never runs on the public website and
never handles credentials. The current analysis signal for MRVL/LITE is **WATCH**: the tool refuses to transmit unless
you type a written override reason, and it is meant for paper trading.

## Components (`mergerarb/execution/`)
| File | Role |
|---|---|
| `orders.py` | `OrderPlan`: the exact hedge (SELL 4.5N MRVL short / BUY N LITE). Exports to JSON (with a SHA-256 plan hash that detects edits) and CSV. Static checks: legs, exact ratio, price band, notional caps, no market orders in live mode. |
| `broker.py` | `IBKRBroker` (ib_async, the maintained fork of ib_insync) and `SimBroker` (offline simulator with fills, partial fills, rejections and disconnects). |
| `executor.py` | Pre-flight checks, approval, leg-managed submission, fill monitoring, unhedged-leg alerts, cancel-all and recovery after reconnect. |
| `journal.py` | Append-only JSONL log (`data/execution/journal.jsonl`, gitignored) of every preflight, approval, submit, status change, fill, cancel, alert and error. |
| `service.py` | The only process that talks to TWS/Gateway: a single-threaded HTTP service on **127.0.0.1**, token-authenticated, which refuses non-local Host/Origin headers. |
| `cli.py` | Writes the order spec (JSON + CSV) with no broker connection at all. |

## Safety model
1. **Paper by default.** Live mode needs all three: `--mode live`, the environment variable `MERGERARB_ALLOW_LIVE=1`,
   and a connected account that is *not* an IBKR paper account (paper accounts start with `DU`; paper mode refuses
   anything else).
2. **Pre-flight before every approval.** The checks are: connection; account type; broker quotes and whether they are
   live or delayed (delayed data blocks live orders); limits within ±2% of the broker price; Rule 201 warning;
   shortability (IBKR tick 46/89) and borrow fee (hard stop above 3%); available funds versus IBKR's **what-if
   margin**; excess liquidity after the trade of at least 50% of net liquidation; gross exposure cap; existing
   positions; duplicate orders and duplicate plans; regular trading hours (holiday and half-day calendar); and the
   model signal.
3. **Explicit approval.** You type `PAPER <account> <plan-hash>` (or `LIVE ...`). The approval is bound to the plan's
   hash, account and mode, and expires after 5 minutes. A non-TRADE signal can only be overridden with a written reason,
   which is journalled. Any other failed check cannot be overridden.
4. **No atomicity assumed.** IBKR does not execute stock pairs atomically (SMART stock combos are "non-guaranteed").
   - The default sequencing is **short first**. MRVL is sold first, and LITE is bought only against short shares that
     have already filled, in whole shares. A rejected short (no borrow) therefore leaves no unhedged long.
   - `simultaneous` sequencing is optional.
   - Leg imbalance is measured in acquirer-share equivalents. If it exceeds `max_unhedged_usd` for longer than
     `unhedged_timeout_s`, the tool raises an **UNHEDGED** alert with the options: complete the lagging leg (needs a
     fresh approval), cancel and unwind, or hold.
5. **Never "filled" on submission.** A plan becomes `FILLED` only when IBKR reports both legs filled in full. Partial
   fills are tracked per order (`filled`, `remaining`, `avgFillPrice`).
6. **Duplicates.** Each plan's idempotency key is the hash of its legs plus the trade date. A second submission is
   refused at pre-flight (through the journal) and again at submission. Orders carry `orderRef = MA:<plan>:<leg>:<n>`
   so open strategy orders can be found at the broker.
7. **Connection loss.** The state moves to `DISCONNECTED` and nothing is assumed. `recover()` reconnects, rebuilds
   order states from the broker by `orderRef`, then resumes monitoring.

## Setup (local only)
1. Install TWS or IB Gateway and log in yourself (paper account first). In *Configure > API > Settings*:
   - enable "ActiveX and Socket Clients";
   - set the socket port (TWS paper 7497 / live 7496; Gateway paper 4002 / live 4001);
   - set trusted IP 127.0.0.1;
   - keep "Read-Only API" **on** until you intend to send orders.
2. `pip install -r requirements.txt`
3. Start the service in its own terminal:
   ```
   python -m mergerarb.execution.service            # IBKR paper (TWS 7497)
   python -m mergerarb.execution.service --sim      # simulator, no IBKR needed
   python -m mergerarb.execution.service --app gateway
   ```
   It writes a random token to `~/.mergerarb/service_token`. Never share or commit it.
4. Start the dashboard with the Execution tab enabled:
   ```
   MERGERARB_ENABLE_EXECUTION=1 streamlit run app/streamlit_app.py
   ```
   (PowerShell: `$env:MERGERARB_ENABLE_EXECUTION=1; streamlit run app/streamlit_app.py`)
5. In the Execution tab: build the plan, export JSON/CSV, Connect, Run pre-flight, type the confirmation phrase,
   Approve and transmit, then use Refresh, Cancel all or Recover.

## Not verified
- No TWS or IB Gateway was installed on the build machine. The IBKR adapter follows ib_async 2.1's API (`IB.connect`,
  `reqMarketDataType(3)`, `reqMktData(..., "236")`, `whatIfOrder`, `placeOrder`, `cancelOrder`, `trades`), but it
  has **only been exercised against the simulator**. Run it on a paper account first and compare with TWS's own
  order window.
- IBKR may impose higher house margin on merger targets. The what-if check reports IBKR's number; the rules-based
  model in `analytics/margin.py` is an estimate only.
- Rule 201 detection is a heuristic (a fall of 10% or more today). IBKR enforces the rule itself.
