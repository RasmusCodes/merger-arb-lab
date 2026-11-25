"""IBKR readiness check - run once TWS / IB Gateway is installed and logged in. Places NO orders.

    python -m mergerarb.execution.readiness                 # TWS paper (7497)
    python -m mergerarb.execution.readiness --app gateway   # IB Gateway paper (4002)
    python -m mergerarb.execution.readiness --sim           # offline dry run of the checks

Checks: API connection, paper vs live account, net liquidation and available funds, market-data type for both legs
(live / delayed), two-sided quotes, MRVL shortability (tick 46/89), IBKR what-if margin for one 2/9 lot, the trading
session, and the current executable spread. Exit code 0 = ready (warnings allowed), 1 = something blocks trading.
"""
from __future__ import annotations

import argparse
import sys

from ..deal import load_deal
from .broker import IBKRBroker, SimBroker
from .config import ExecutionConfig
from .executor import market_session
from .orders import build_pair_plan
from .trigger import executable_spread


def run(broker, cfg: ExecutionConfig, deal_id: str = "mrvl_lite") -> list[tuple[str, str, str]]:
    deal = load_deal(deal_id)
    T, A, R = deal.target.ticker, deal.acquirer.ticker, deal.exchange_ratio
    out: list[tuple[str, str, str]] = []
    try:
        broker.connect()
    except Exception as e:  # noqa: BLE001 - report any connection failure plainly
        return [("API connection", "FAIL", f"{e} - is TWS/Gateway running, logged in, API enabled on port "
                                           f"{cfg.effective_port}, 127.0.0.1 trusted?")]
    out.append(("API connection", "PASS", f"connected on port {cfg.effective_port}"))
    accts = broker.accounts()
    acct = cfg.account or (accts[0] if len(accts) == 1 else None)
    if not acct:
        out.append(("account", "FAIL", f"several accounts {accts}: pass --account"))
        return out
    paper = acct.startswith("DU")
    out.append(("account type", "PASS" if (paper == (cfg.mode == "paper")) else "FAIL",
                f"{acct} is a {'paper' if paper else 'LIVE'} account; mode={cfg.mode}"))
    snap = broker.account_snapshot(acct)
    out.append(("funds", "PASS" if snap.net_liquidation >= 2000 else "FAIL",
                f"net liquidation {snap.net_liquidation:,.0f}, available {snap.available_funds:,.0f}, "
                f"excess liquidity {snap.excess_liquidity:,.0f} (Reg T needs >= 2,000)"))
    q = {}
    for s in (T, A):
        qq = broker.quote(s)
        q[s] = qq
        st = "PASS" if qq.data_type == "live" else ("WARN" if qq.data_type in ("delayed", "simulated") else "FAIL")
        out.append((f"{s} market data", st, f"{qq.data_type}: last {qq.last} bid {qq.bid} ask {qq.ask}"
                    + ("" if qq.data_type == "live" else " - subscribe to US real-time data (NASDAQ) before live trading")))
    si = broker.short_info(A)
    st, msg = si.status(int(round(2 * R)))
    out.append((f"{A} shortability", st, msg))
    plan = build_pair_plan(deal, 2, q[T].ask or q[T].last, q[A].bid or q[A].last,
                           {T: q[T].last, A: q[A].last})
    try:
        wi = [broker.what_if(leg, acct) for leg in plan.legs]
        out.append(("what-if margin (1 lot)", "PASS",
                    "initial +" + f"{sum(w.init_margin_change or 0 for w in wi):,.0f}, maintenance +"
                    f"{sum(w.maint_margin_change or 0 for w in wi):,.0f}, commission "
                    f"{sum(w.commission or 0 for w in wi):.2f}"))
    except Exception as e:  # noqa: BLE001
        out.append(("what-if margin (1 lot)", "FAIL", str(e)))
    open_, why = market_session()
    out.append(("session", "PASS" if open_ else "WARN", why))
    if q[T].ask and q[A].bid:
        out.append(("executable spread", "PASS", f"{executable_spread(q[T].ask, q[A].bid, R):.2%} "
                                                 f"(sell {A} at bid, buy {T} at ask)"))
    else:
        out.append(("executable spread", "WARN", "no two-sided quotes (market closed or no data subscription)"))
    broker.disconnect()
    return out


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
