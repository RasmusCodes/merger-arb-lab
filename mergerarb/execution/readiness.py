"""IBKR readiness check - run once TWS / IB Gateway is installed and logged in. Places NO orders.

    python -m mergerarb.execution.readiness                 # TWS paper (7497)
    python -m mergerarb.execution.readiness --app gateway   # IB Gateway paper (4002)
    python -m mergerarb.execution.readiness --sim           # offline dry run of the checks

Checks: API connection, paper vs live account, net liquidation and available funds, market-data type for both legs
(live / delayed; delayed is a FAIL in live mode), two-sided quotes, MRVL shortability (tick 46/89; unknown is a FAIL),
IBKR what-if margin for one exact lot (any failure or timeout is a FAIL - 'Read-Only API' blocks what-if), the trading
session, and the current executable spread. Exit code 0 = ready (warnings allowed), 1 = something blocks trading.
"""
from __future__ import annotations

import argparse
import sys

from ..deal import load_deal
from .broker import READ_ONLY_HINT, IBKRBroker, SimBroker
from .config import ExecutionConfig
from .executor import market_session
from .orders import build_pair_plan, exact_lot
from .trigger import executable_spread


def run(broker, cfg: ExecutionConfig, deal_id: str = "mrvl_lite", now_utc=None) -> list[tuple[str, str, str]]:
    deal = load_deal(deal_id)
    T, A, R = deal.target.ticker, deal.acquirer.ticker, deal.exchange_ratio
    lot = exact_lot(R)
    out: list[tuple[str, str, str]] = []
    try:
        broker.connect()
    except Exception as e:  # noqa: BLE001 - report any connection failure plainly
        return [("API connection", "FAIL", f"{e} - is TWS/Gateway running, logged in, API enabled on port "
                                           f"{cfg.effective_port}, 127.0.0.1 trusted?")]
    try:
        out.append(("API connection", "PASS", f"connected on port {cfg.effective_port}"))
        accts = broker.accounts()
        if cfg.account and cfg.account not in accts:
            out.append(("account", "FAIL", f"{cfg.account} is not among the managed accounts {accts}"))
            return out
        acct = cfg.account or (accts[0] if len(accts) == 1 else None)
        if not acct:
            out.append(("account", "FAIL", f"several accounts {accts}: pass --account"))
            return out
        paper = acct.startswith("DU")
        out.append(("account type", "PASS" if (paper == (cfg.mode == "paper")) else "FAIL",
                    f"{acct} is a {'paper' if paper else 'LIVE'} account; mode={cfg.mode}"))
        if cfg.mode == "live" and not cfg.live_allowed():
            out.append(("live switch", "FAIL", "live mode needs MERGERARB_ALLOW_LIVE=1"))
        snap = None
        try:
            snap = broker.account_snapshot(acct)
            if snap.net_liquidation <= 0:
                out.append(("funds", "FAIL", "account summary returned no NetLiquidation (non-USD base currency or "
                                             "summary not loaded?)"))
            else:
                out.append(("funds", "PASS" if snap.net_liquidation >= 2000 else "FAIL",
                            f"net liquidation {snap.net_liquidation:,.0f} {snap.currency}, available "
                            f"{snap.available_funds:,.0f}, excess liquidity {snap.excess_liquidity:,.0f} "
                            f"(Reg T needs >= 2,000)"))
        except Exception as e:  # noqa: BLE001
            out.append(("funds", "FAIL", f"account summary unavailable: {e}"))
        q = {}
        for s in (T, A):
            try:
                qq = broker.quote(s)
            except Exception as e:  # noqa: BLE001
                out.append((f"{s} market data", "FAIL", f"no quote: {e}"))
                continue
            q[s] = qq
            if not (qq.last or (qq.bid and qq.ask)):
                out.append((f"{s} market data", "FAIL", f"{qq.data_type}: no bid/ask/last from the broker"))
                continue
            if qq.data_type == "live":
                st = "PASS"
            elif qq.data_type in ("delayed", "simulated"):
                st = "FAIL" if cfg.mode == "live" else "WARN"
            else:
                st = "FAIL"
            out.append((f"{s} market data", st, f"{qq.data_type}: last {qq.last} bid {qq.bid} ask {qq.ask}"
                        + ("" if qq.data_type == "live" else
                           " - subscribe to US real-time data (NASDAQ) before live trading")))
        try:
            si = broker.short_info(A)
            st, msg = si.status(int(lot * R))
            out.append((f"{A} shortability", "FAIL" if not si.known else st, msg))
        except Exception as e:  # noqa: BLE001
            out.append((f"{A} shortability", "FAIL", f"shortability unavailable: {e}"))
        tl = (q[T].ask or q[T].last) if T in q else None
        al = (q[A].bid or q[A].last) if A in q else None
        if not (tl and al):
            out.append(("what-if margin (1 lot)", "FAIL", "no prices to price a what-if order"))
        else:
            try:
                plan = build_pair_plan(deal, lot, tl, al, {T: q[T].last or tl, A: q[A].last or al})
                wi = [broker.what_if(leg, acct) for leg in plan.legs]
                if any(w is None or w.init_margin_change is None or w.maint_margin_change is None for w in wi):
                    raise RuntimeError("what-if returned no margin numbers")
                init = sum(w.init_margin_change for w in wi)
                enough = snap is None or snap.available_funds >= init
                out.append(("what-if margin (1 lot)", "PASS" if enough else "FAIL",
                            f"initial +{init:,.0f}, maintenance +{sum(w.maint_margin_change for w in wi):,.0f}, "
                            f"commission {sum(w.commission or 0 for w in wi):.2f}"
                            + ("" if enough else f" - more than the available {snap.available_funds:,.0f}")))
            except Exception as e:  # noqa: BLE001
                out.append(("what-if margin (1 lot)", "FAIL", f"{e}. {READ_ONLY_HINT}"))
        open_, why = market_session(now_utc)
        out.append(("session", "PASS" if open_ else "WARN", why))
        if T in q and A in q and q[T].ask and q[A].bid:
            out.append(("executable spread", "PASS", f"{executable_spread(q[T].ask, q[A].bid, R, deal.cash_per_share):.2%}"
                                                     f" (sell {A} at bid, buy {T} at ask)"))
        else:
            out.append(("executable spread", "WARN", "no two-sided quotes (market closed or no data subscription)"))
        return out
    finally:
        try:
            broker.disconnect()
        except Exception:  # noqa: BLE001
            pass


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="IBKR readiness check (no orders)")
    ap.add_argument("--app", default="tws", choices=["tws", "gateway"])
    ap.add_argument("--mode", default="paper", choices=["paper", "live"])
    ap.add_argument("--ib-port", type=int, default=None)
    ap.add_argument("--client-id", type=int, default=18)
    ap.add_argument("--account", default=None)
    ap.add_argument("--sim", action="store_true")
    a = ap.parse_args(argv)
    cfg = ExecutionConfig(mode=a.mode, app=a.app, port=a.ib_port, client_id=a.client_id, account=a.account)
    broker = SimBroker(quotes={"LITE": {"last": 487.12, "bid": 486.9, "ask": 487.3},
                               "MRVL": {"last": 131.57, "bid": 131.55, "ask": 131.6}}) if a.sim else IBKRBroker(cfg)
    rows = run(broker, cfg)
    for name, st, msg in rows:
        print(f"[{st:4s}] {name}: {msg}")
    fails = [r for r in rows if r[1] == "FAIL"]
    print("READY (warnings allowed)" if not fails else f"NOT READY: {len(fails)} blocking item(s)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
