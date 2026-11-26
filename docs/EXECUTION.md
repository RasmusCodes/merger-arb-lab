# IBKR execution: design, safety and setup

The execution tooling exists so that a ratio-hedged pair can be **exported exactly** and, if you choose, **sent to
your own TWS / IB Gateway after explicit approval**. It never trades on its own, never runs on the public website and
never handles credentials.

The current analysis signal for MRVL/LITE is **WATCH**: potentially attractive, small size only (see
[REPORT.md](REPORT.md)). The tool transmits a non-TRADE plan only with a written override reason, and it is meant for
paper trading first.

*Revised 2026-11-25.* An independent review found serious bugs, all now fixed with regression tests
(`tests/test_execution_regressions.py`):
- "Cancel all" could send a new LITE order.
- A LITE order that ended unfilled was re-sent automatically, without limit and without approval.
- After a reconnect the tool stopped seeing fills.
- An edited plan was accepted if its hash field was left out.

## Components (`mergerarb/execution/`)
| File | Role |
|---|---|
| `orders.py` | `OrderPlan`: the exact hedge (SELL ratio × N MRVL short / BUY N LITE). Whole shares only: a target quantity whose hedge is not a whole number of MRVL shares is refused, never rounded (multiples of 2 LITE / 9 MRVL). JSON export with a SHA-256 plan hash; `verify_plan_integrity` rebuilds the canonical plan from the deal file (ratio, actions, symbols, DAY, regular hours) and compares hashes. CSV export and static checks. |
| `broker.py` | `IBKRBroker` (ib_async) and `SimBroker`. IBKRBroker keeps no library objects between calls (ib_async discards them on every disconnect), takes fills from executions, uses a 10 s request timeout, does nothing on a second connect and records TWS error messages per order. SimBroker reproduces the ib_async behaviour the executor relies on: rejections as `Cancelled` plus error text, cancel races, orders completed during a disconnect returning with filled = 0, failing or timed-out what-if. |
| `executor.py` | Pre-flight, typed approval, budgeted submission, fill monitoring, UNHEDGED / NEEDS_ACTION alerts, residual approvals, cancel, recovery, closing, and rebuilding unfinished plans from the journal. |
| `journal.py` | Append-only JSONL log (`data/execution/journal.jsonl`; the simulator writes `journal_sim.jsonl`; both gitignored) of every pre-flight, approval, order attempt, submission, status change, fill, error, cancel, alert and residual approval. The service rebuilds unfinished plans from it at start-up. |
| `service.py` | The only process that talks to TWS/Gateway: a single-threaded HTTP service on **127.0.0.1**, token-authenticated, which refuses non-local Host/Origin headers, re-validates every plan against the deal file, answers bad requests with 400 JSON and never returns internals. |
| `cli.py` | Writes the order spec (JSON + CSV) with no broker connection; refuses hedges that are not whole shares. |
| `trigger.py` | Alert-only spread-trigger ladder on the *executable* spread (sell MRVL at the bid, buy LITE at the ask). Fires only on live quotes younger than 15 s during the regular session. Each rung's first hit per day is journalled with suggested limits; there is no automatic order. Default (`default_ladder`): **23%** for one lot (the base case clears the 5% hurdle after financing at 21.0%), and **27%** for a second lot from $20k of equity (the pessimistic break case breaks even there). Never more than the Monte Carlo tail cap of one lot per $10k. |
| `readiness.py` | `python -m mergerarb.execution.readiness`: a one-command check of the connection, paper vs live account, funds, data subscription, shortability, IBKR what-if margin for one lot, session and executable spread. It places no orders; exit code 1 means something blocks trading (missing prices, delayed data in live mode, unknown shortability and any what-if failure are FAILs). |

## Safety model
1. **Paper by default.** Live mode needs all three: `--mode live`, the environment variable `MERGERARB_ALLOW_LIVE=1`,
   and a connected account that is *not* an IBKR paper account (paper accounts start with `DU`; paper mode refuses
   anything else).
2. **Server-side plan integrity.** A plan must arrive with its `plan_hash`. The service rebuilds the canonical plan from
   the deal file and refuses (HTTP 400):
   - a missing or mismatched hash;
   - a ratio other than the deal file's;
   - a hedge that is not a whole number of shares;
   - wrong actions or symbols;
   - anything but DAY orders in regular hours;
   - market orders in live mode.
3. **Pre-flight before every approval.** The checks are:
   - **plan and duplicates:** plan integrity; no unfinished plan (including plans rebuilt from the journal and not yet
     reconciled); duplicate plans (same legs on the same New York trade date) and reused plan ids;
   - **connection and account:** connection; account type;
   - **prices:** broker quotes and whether they are live or delayed (delayed data blocks live orders); limits within ±2%
     of the broker price;
   - **short sale:** Rule 201 warning; shortability (IBKR tick 46/89; unknown is a FAIL in live mode and a warning in
     paper mode); borrow fee (required; hard stop above 3%);
   - **model signal** (required);
   - **margin and exposure:** IBKR's **what-if margin** (required: an empty, failed or timed-out what-if blocks, with a
     reminder that "Read-Only API" refuses what-if); available funds against that margin; excess liquidity after the
     trade of at least 50% of net liquidation; gross exposure cap; existing positions; open strategy orders;
   - **timing:** regular trading hours (holiday and half-day calendar).
4. **Explicit approval.** You type `PAPER <account> <plan-hash>` (or `LIVE ...`). The approval is bound to the plan's
   hash, account and mode; the pre-flight report and the approval each expire after 5 minutes. A non-TRADE signal can
   only be overridden with a written reason, which is journalled. Any other failed check cannot be overridden.
5. **Each leg's quantity is a fixed budget; no atomicity assumed.** IBKR does not execute stock pairs atomically.
   - The default sequencing is **short first**. MRVL is sold first. LITE is bought only:
     - against MRVL shares that have already filled;
     - in whole shares;
     - within the approved quantity;
     - while the plan is WORKING or PARTIAL.
     A short that ends with nothing filled (rejection, no borrow) makes the plan FAILED and nothing else is sent.
   - `simultaneous` sequencing is optional. If the short ends unfilled, the LITE order is cancelled, monitoring continues
     and an alert is raised.
   - **Unfilled quantity is never re-sent automatically.** This covers any order that ends unfilled: a rejection, a DAY
     order expiring, a cancel made in TWS.
     - The quantity becomes an unplaced residual: journalled, alerted, plan status **NEEDS_ACTION**.
     - Re-placing it needs a residual pre-flight and a fresh typed approval,
       `PAPER <account> <plan-hash> <LEG> <qty> @ <limit>`. The approval is bound to plan hash, leg, quantity, limit,
       account and mode, and is valid for 5 minutes.
     - LITE residuals are never bought ahead of the filled short.
   - Leg imbalance is measured in MRVL-equivalent shares. The cap is the lower of $500 and 25% of one lot's short value,
     about $296 for 9 MRVL. If the imbalance stays above it for longer than `unhedged_timeout_s` (60 s), the tool raises
     an **UNHEDGED** alert. Alerts and required actions appear at the top of `/status`.
6. **Never "filled" on submission.** A plan becomes `FILLED` only when the broker's facts show both legs filled exactly to
   the approved quantity with no order working. Fills are taken from IBKR executions. An over-fill puts the plan in
   NEEDS_ACTION.
7. **Cancel.** `cancel_all` moves the plan to CANCELLING before any cancel is sent.
   - Nothing is placed while a plan is CANCELLING, NEEDS_ACTION or finished.
   - The service keeps monitoring until every order is terminal, late fills included.
   - A non-zero final imbalance gives NEEDS_ACTION with an UNHEDGED alert; otherwise the plan is CANCELLED.
8. **Duplicates.** The idempotency key (plan hash + New York trade date) is checked at pre-flight and again at
   submission; plan ids cannot be reused. Orders carry `orderRef = MA:<plan>:<leg>:<n>`.
9. **Connection loss and restarts.** The state moves to `DISCONNECTED` and nothing is assumed.
   - `recover()` reconnects and reconciles every order by `orderRef` with the broker's orders and executions.
   - It never places an order and never revives a FILLED, CANCELLED, FAILED or CLOSED plan.
   - A CANCELLING plan gets its cancels re-sent.
   - Orders the broker no longer reports are marked Unknown and must be checked in TWS.
   - At start-up the service rebuilds unfinished plans from the journal and blocks new plans until `recover()` has
     reconciled them.
   - `close(reason)` hands an unfinished plan with no working orders over to manual handling (written reason, journalled).

## Setup (local only)
1. Install TWS or IB Gateway and log in yourself (paper account first). In *Configure > API > Settings*:
   - enable "ActiveX and Socket Clients";
   - set the socket port (TWS paper 7497 / live 7496; Gateway paper 4002 / live 4001);
   - set trusted IP 127.0.0.1;
   - untick "Read-Only API" on the account you will trade from. With it ticked, TWS refuses IBKR's what-if margin
     check, so readiness and pre-flight FAIL (requests time out after 10 s). Keep it ticked on any account you do not
     intend to trade.
2. `pip install -r requirements.txt`
3. Run `python -m mergerarb.execution.readiness` (add `--app gateway` for IB Gateway). Exit code 0 = ready.
4. Start the service in its own terminal:
   ```
   python -m mergerarb.execution.service            # IBKR paper (TWS 7497)
   python -m mergerarb.execution.service --sim      # simulator, no IBKR needed (journal_sim.jsonl)
   python -m mergerarb.execution.service --app gateway
   ```
   It writes a random token to `~/.mergerarb/service_token`. Never share or commit it. If the journal holds an
   unfinished plan, the service says so: Connect, then Recover, before anything else.
5. Start the dashboard with the Execution tab enabled:
   ```
   MERGERARB_ENABLE_EXECUTION=1 streamlit run app/streamlit_app.py
   ```
   (PowerShell: `$env:MERGERARB_ENABLE_EXECUTION=1; streamlit run app/streamlit_app.py`)
6. In the Execution tab:
   1. build the plan and export JSON/CSV;
   2. Connect, then Run pre-flight;
   3. type the confirmation phrase, then Approve and transmit;
   4. afterwards use Refresh, Cancel all or Recover, and watch `attention` and `alerts` in the status output.
7. When a plan is NEEDS_ACTION, decide outside the tool, then re-place a residual or close the plan. These steps have no
   dashboard buttons yet; call the service (PowerShell):
   ```
   $h = @{Authorization = "Bearer $(Get-Content ~/.mergerarb/service_token)"}; $u = "http://127.0.0.1:8813"
   Invoke-RestMethod -Method Post "$u/residual_preflight" -Headers $h -ContentType application/json -Body '{"leg":"target","quantity":2,"limit_price":487.50}'
   Invoke-RestMethod -Method Post "$u/approve_residual" -Headers $h -ContentType application/json -Body '{"leg":"target","quantity":2,"limit_price":487.50,"typed":"<phrase from residual_preflight>"}'
   Invoke-RestMethod -Method Post "$u/close" -Headers $h -ContentType application/json -Body '{"reason":"unwound the MRVL short manually in TWS"}'
   ```

## Not verified
- **No real TWS test.** No TWS or IB Gateway was installed on the build machine.
  - The IBKR adapter targets the installed ib_async 2.1.0: `IB.connect`, `reqMarketDataType(3)`, `reqMktData(..., "236")`,
    `whatIfOrder` with a 10 s `RequestTimeout`, `placeOrder`, `cancelOrder`, `trades`, `openTrades`, `fills`,
    `reqExecutions`, `errorEvent`.
  - Its handling of disconnects, rejections and completed orders was checked by driving the library's own callbacks
    offline; everything else ran only against the simulator.
  - Run it on a paper account first and compare with TWS's order window and Trade Log.
- Which TWS messages arrive for a rejection, a DAY expiry or a cancel race, and whether ib_async ends such orders as
  Cancelled or Inactive. Any terminal order the tool did not cancel is treated as ended unfilled, so either outcome is
  handled.
- Whether the shortability ticks (46/89) arrive with delayed market data. Live mode requires them; paper mode accepts
  "unknown" with a warning.
- That "Read-Only API" makes TWS refuse what-if requests (the tool fails within 10 s either way).
- Execution corrections are matched by execId prefix, and the order's cumulative quantity is used as the filled quantity.
- Orders older than the executions TWS returns after a restart (roughly the current session / last 24 hours) show as
  Unknown after a reconnect and must be checked in TWS.
- For accounts whose base currency is not USD, account values are read from the base-currency rows; the $2,000 and
  notional caps are compared without FX conversion.
- IBKR may impose higher house margin on merger targets. The what-if check reports IBKR's number; the rules-based model
  in `analytics/margin.py` is an estimate only.
- Rule 201 detection is a heuristic (a fall of 10% or more today). IBKR enforces the rule itself.

## Paper-account test checklist (before any live use)
- [ ] **Readiness and Read-Only API.**
  - With Read-Only API ticked, the what-if row should FAIL with the hint within about 10 s, not hang. With it unticked:
    PASS.
  - Note the data type shown and whether the shortability tick arrives on delayed data.
- [ ] Call `/connect` twice: the TWS API log should show one connection and no drop.
- [ ] One lot at marketable limits: journal fills for both legs, status FILLED, and figures matching the TWS Trade Log.
- [ ] **Forced LITE rejection** (for example a TWS precautionary order-value limit below the LITE order value):
  - expect exactly one LITE order, the error text journalled and NEEDS_ACTION;
  - note whether the final status is Cancelled or Inactive.
- [ ] Leave a non-marketable LITE limit working into the 16:00 close, and separately cancel a LITE order by hand in TWS.
  Expect a residual and NEEDS_ACTION, and nothing re-sent that evening or the next morning.
- [ ] Residual approval end to end, including a wrong phrase and an expired approval.
- [ ] "Cancel all" while MRVL is filling: CANCELLING until every order is terminal, then CANCELLED or NEEDS_ACTION with
  UNHEDGED, and no new orders.
- [ ] **Disconnect mid-fill.** Drop the API connection or restart TWS while MRVL works and fills, then call `/recover`:
  - fills should come from executions;
  - recover places nothing;
  - the next pump buys LITE.
- [ ] **Service restart mid-plan:**
  - the plan is rebuilt from the journal;
  - new plans are blocked;
  - `/recover` reconciles it and `/close` works.
- [ ] Triggers on live data: hits only during regular hours on fresh quotes; delayed quotes never fire.
- [ ] Only if your account's base currency isn't USD: the readiness funds row and the what-if versus available-funds
  check.
