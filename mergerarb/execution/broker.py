"""Broker adapters behind one small interface.

* ``IBKRBroker`` - TWS / IB Gateway via ib_async (maintained fork of ib_insync; falls back to ib_insync if that is
  what is installed). Single-threaded by design: call it only from the thread that owns it.
* ``SimBroker`` - an offline simulator with the same interface (fills, partial fills, rejections, disconnects) used by
  the tests and the dashboard's demo mode. It never touches the network.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import math
from typing import Protocol

from ..analytics.margin import MarginRules
from .orders import OrderLeg

UNSET = 1.7976931348623157e308
DATA_TYPES = {1: "live", 2: "frozen", 3: "delayed", 4: "delayed-frozen"}


@dataclasses.dataclass
class BrokerQuote:
    symbol: str
    last: float | None
    bid: float | None
    ask: float | None
    prev_close: float | None
    time_utc: dt.datetime | None
    data_type: str              # live / delayed / frozen / delayed-frozen / simulated


@dataclasses.dataclass
class ShortInfo:
    symbol: str
    level: float | None          # IBKR tick 46: >2.5 shortable (easy), 1.5-2.5 locate needed, <1.5 not shortable
    shares: float | None         # IBKR tick 89

    def status(self, needed: int) -> tuple[str, str]:
        if self.level is None or (isinstance(self.level, float) and math.isnan(self.level)):
            return "WARN", "shortability unknown (no tick 46 yet)"
        if self.level > 2.5:
            if self.shares is not None and not math.isnan(self.shares) and self.shares < needed:
                return "FAIL", f"only {self.shares:,.0f} shares available to borrow, need {needed}"
            return "PASS", f"shortable (level {self.level}), {self.shares if self.shares is not None else '?'} available"
        if self.level > 1.5:
            return "WARN", f"hard to borrow: locate required (level {self.level})"
        return "FAIL", f"not shortable (level {self.level})"


@dataclasses.dataclass
class WhatIf:
    init_margin_change: float | None
    maint_margin_change: float | None
    equity_with_loan_change: float | None
    commission: float | None
    warning: str = ""


@dataclasses.dataclass
class AccountSnapshot:
    account: str
    net_liquidation: float
    available_funds: float
    excess_liquidity: float
    buying_power: float
    init_margin_req: float
    maint_margin_req: float


@dataclasses.dataclass
class OrderHandle:
    order_id: int
    order_ref: str
    symbol: str
    action: str
    quantity: int
    limit_price: float | None
    status: str = "PendingSubmit"   # IBKR statuses: PendingSubmit, PreSubmitted, Submitted, Filled, Cancelled, Inactive...
    filled: float = 0.0
    remaining: float = 0.0
    avg_fill_price: float = 0.0
    last_update_utc: str = ""
    error: str = ""

    @property
    def is_done(self) -> bool:
        return self.status in ("Filled", "Cancelled", "ApiCancelled", "Inactive")


class BrokerClient(Protocol):
    def connect(self) -> None: ...
    def disconnect(self) -> None: ...
    def is_connected(self) -> bool: ...
    def accounts(self) -> list[str]: ...
    def account_snapshot(self, account: str) -> AccountSnapshot: ...
    def positions(self, account: str) -> dict[str, float]: ...
    def quote(self, symbol: str, primary_exchange: str = "NASDAQ") -> BrokerQuote: ...
    def short_info(self, symbol: str, primary_exchange: str = "NASDAQ") -> ShortInfo: ...
    def what_if(self, leg: OrderLeg, account: str, quantity: int | None = None) -> WhatIf: ...
    def place(self, leg: OrderLeg, order_ref: str, account: str, quantity: int | None = None,
              limit_price: float | None = None) -> OrderHandle: ...
    def cancel(self, handle: OrderHandle) -> None: ...
    def refresh(self) -> list[OrderHandle]: ...
    def open_orders(self) -> list[OrderHandle]: ...


def _f(x) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return None if (math.isnan(v) or abs(v) >= UNSET) else v


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds")


# ------------------------------------------------------------------------------------------------------------------
class IBKRBroker:
    """ib_async adapter. Requires TWS or IB Gateway running locally with the API enabled (Configure > API >
    Settings: 'Enable ActiveX and Socket Clients', trusted IP 127.0.0.1; leave 'Read-Only API' ON until you intend to
    trade). Authentication happens in TWS/Gateway; this class never sees credentials."""

    def __init__(self, cfg):
        try:
            import ib_async as ibm
        except ImportError:  # pragma: no cover - fallback for older installs
            import ib_insync as ibm
        self.ibm, self.cfg = ibm, cfg
        self.ib = ibm.IB()
        self._contracts: dict[str, object] = {}
        self._tickers: dict[str, object] = {}
        self._trades: dict[str, object] = {}

    def connect(self) -> None:
        self.ib.connect(self.cfg.host, self.cfg.effective_port, clientId=self.cfg.client_id, timeout=15)
        # 3 = real-time if subscribed, otherwise delayed; every quote is labelled with its actual type
        self.ib.reqMarketDataType(3)

    def disconnect(self) -> None:
        self.ib.disconnect()

    def is_connected(self) -> bool:
        return bool(self.ib.isConnected())

    def accounts(self) -> list[str]:
        return list(self.ib.managedAccounts())

    def account_snapshot(self, account: str) -> AccountSnapshot:
        vals = {(v.tag, v.currency): v.value for v in self.ib.accountSummary(account)}
        g = lambda tag: _f(vals.get((tag, "USD")) or vals.get((tag, "BASE"))) or 0.0  # noqa: E731
        return AccountSnapshot(account, g("NetLiquidation"), g("AvailableFunds"), g("ExcessLiquidity"),
                               g("BuyingPower"), g("InitMarginReq"), g("MaintMarginReq"))

    def positions(self, account: str) -> dict[str, float]:
        return {p.contract.symbol: float(p.position) for p in self.ib.positions(account)}

    def _contract(self, symbol: str, primary: str):
        if symbol not in self._contracts:
            c = self.ibm.Stock(symbol, "SMART", "USD", primaryExchange=primary)
            q = self.ib.qualifyContracts(c)
            if not q:
                raise RuntimeError(f"could not qualify contract {symbol}")
            self._contracts[symbol] = q[0]
        return self._contracts[symbol]

    def _ticker(self, symbol: str, primary: str):
        if symbol not in self._tickers:
            self._tickers[symbol] = self.ib.reqMktData(self._contract(symbol, primary), "236", False, False)
            self.ib.sleep(2.0)
        else:
            self.ib.sleep(0.1)
        return self._tickers[symbol]

    def quote(self, symbol: str, primary_exchange: str = "NASDAQ") -> BrokerQuote:
        t = self._ticker(symbol, primary_exchange)
        tm = t.time if getattr(t, "time", None) else None
        return BrokerQuote(symbol, _f(t.last), _f(t.bid), _f(t.ask), _f(t.close), tm,
                           DATA_TYPES.get(int(getattr(t, "marketDataType", 1) or 1), "unknown"))

    def short_info(self, symbol: str, primary_exchange: str = "NASDAQ") -> ShortInfo:
        t = self._ticker(symbol, primary_exchange)
        return ShortInfo(symbol, _f(getattr(t, "shortable", None)), _f(getattr(t, "shortableShares", None)))

    def _order(self, leg: OrderLeg, account: str, quantity: int | None, limit_price: float | None):
        q = int(quantity if quantity is not None else leg.quantity)
        px = limit_price if limit_price is not None else leg.limit_price
        if leg.order_type == "MKT":
            o = self.ibm.MarketOrder(leg.action, q)
        else:
            o = self.ibm.LimitOrder(leg.action, q, float(px))
        o.account, o.tif, o.outsideRth, o.transmit = account, leg.tif, leg.outside_rth, True
        return o

    def what_if(self, leg: OrderLeg, account: str, quantity: int | None = None) -> WhatIf:
        st = self.ib.whatIfOrder(self._contract(leg.symbol, leg.primary_exchange), self._order(leg, account, quantity, None))
        return WhatIf(_f(st.initMarginChange), _f(st.maintMarginChange), _f(st.equityWithLoanChange),
                      _f(st.commission), getattr(st, "warningText", "") or "")

    def _handle(self, trade) -> OrderHandle:
        o, s = trade.order, trade.orderStatus
        err = ""
        for e in getattr(trade, "log", [])[-3:]:
            if getattr(e, "errorCode", 0):
                err = f"{e.errorCode}: {e.message}"
        return OrderHandle(o.orderId, o.orderRef, trade.contract.symbol, o.action, int(o.totalQuantity),
                           _f(getattr(o, "lmtPrice", None)), s.status, float(s.filled), float(s.remaining),
                           float(s.avgFillPrice or 0.0), _now(), err)

    def place(self, leg: OrderLeg, order_ref: str, account: str, quantity: int | None = None,
              limit_price: float | None = None) -> OrderHandle:
        o = self._order(leg, account, quantity, limit_price)
        o.orderRef = order_ref
        trade = self.ib.placeOrder(self._contract(leg.symbol, leg.primary_exchange), o)
        self._trades[order_ref] = trade
        self.ib.sleep(0.5)
        return self._handle(trade)

    def cancel(self, handle: OrderHandle) -> None:
        tr = self._trades.get(handle.order_ref)
        if tr is None:
            tr = next((t for t in self.ib.openTrades() if t.order.orderRef == handle.order_ref), None)
        if tr is not None:
            self.ib.cancelOrder(tr.order)
            self.ib.sleep(0.5)

    def refresh(self) -> list[OrderHandle]:
        self.ib.sleep(0.05)
        for t in self.ib.trades():
            if t.order.orderRef.startswith("MA:"):
                self._trades.setdefault(t.order.orderRef, t)
        return [self._handle(t) for t in self._trades.values()]

    def open_orders(self) -> list[OrderHandle]:
        self.ib.reqAllOpenOrders()
        self.ib.sleep(0.5)
        return [self._handle(t) for t in self.ib.openTrades() if t.order.orderRef.startswith("MA:")]


# ------------------------------------------------------------------------------------------------------------------
class SimBroker:
    """Offline simulator. ``fill_mode``: 'immediate' (fills at limit on the next refresh), 'partial' (fills
    ``partial_fraction`` per refresh), 'none' (rests). Short sales are rejected when ``short_level`` < 1.5."""

    def __init__(self, account: str = "DU0000001", net_liq: float = 10_000.0, quotes: dict | None = None,
                 short_level: float = 3.0, short_shares: float = 10_000_000, fill_mode: str = "immediate",
                 partial_fraction: float = 0.5, data_type: str = "simulated", fill_mode_by_symbol: dict | None = None):
        self.account, self.net_liq = account, net_liq
        self.quotes = quotes or {}
        self.short_level, self.short_shares = short_level, short_shares
        self.fill_mode, self.partial_fraction = fill_mode, partial_fraction
        self.fill_mode_by_symbol = fill_mode_by_symbol or {}
        self.data_type = data_type
        self.connected = False
        self._orders: dict[str, OrderHandle] = {}
        self._next_id = 1000
        self.pos: dict[str, float] = {}
        self.rules = MarginRules()

    def connect(self) -> None:
        self.connected = True

    def disconnect(self) -> None:
        self.connected = False

    def is_connected(self) -> bool:
        return self.connected

    def _need(self) -> None:
        if not self.connected:
            raise ConnectionError("not connected")

    def accounts(self) -> list[str]:
        self._need()
        return [self.account]

    def _gross(self) -> tuple[float, float]:
        lv = sum(max(q, 0) * self.quotes.get(s, {}).get("last", 0) for s, q in self.pos.items())
        sv = sum(max(-q, 0) * self.quotes.get(s, {}).get("last", 0) for s, q in self.pos.items())
        return lv, sv

    def account_snapshot(self, account: str) -> AccountSnapshot:
        self._need()
        lv, sv = self._gross()
        maint = self.rules.long_maint * lv + self.rules.short_maint * sv
        init = self.rules.regt(lv, sv)
        return AccountSnapshot(account, self.net_liq, self.net_liq - init, self.net_liq - maint,
                               2 * (self.net_liq - init), init, maint)

    def positions(self, account: str) -> dict[str, float]:
        self._need()
        return dict(self.pos)

    def quote(self, symbol: str, primary_exchange: str = "NASDAQ") -> BrokerQuote:
        self._need()
        q = self.quotes[symbol]
        return BrokerQuote(symbol, q.get("last"), q.get("bid"), q.get("ask"), q.get("prev_close"),
                           q.get("time_utc", dt.datetime.now(dt.timezone.utc)), self.data_type)

    def short_info(self, symbol: str, primary_exchange: str = "NASDAQ") -> ShortInfo:
        self._need()
        return ShortInfo(symbol, self.short_level, self.short_shares)

    def what_if(self, leg: OrderLeg, account: str, quantity: int | None = None) -> WhatIf:
        self._need()
        q = quantity if quantity is not None else leg.quantity
        px = leg.limit_price or self.quotes[leg.symbol]["last"]
        v = q * px
        return WhatIf(self.rules.regt_initial * v, (self.rules.long_maint if leg.action == "BUY" else self.rules.short_maint) * v,
                      0.0, max(1.0, 0.005 * q))

    def place(self, leg: OrderLeg, order_ref: str, account: str, quantity: int | None = None,
              limit_price: float | None = None) -> OrderHandle:
        self._need()
        q = int(quantity if quantity is not None else leg.quantity)
        self._next_id += 1
        h = OrderHandle(self._next_id, order_ref, leg.symbol, leg.action, q,
                        limit_price if limit_price is not None else leg.limit_price, "Submitted", 0.0, float(q), 0.0, _now())
        if leg.action == "SELL" and self.pos.get(leg.symbol, 0) <= 0 and self.short_level < 1.5:
            h.status, h.error = "Inactive", "201: Order rejected - no shares available to borrow"
        self._orders[order_ref] = h
        return dataclasses.replace(h)

    def cancel(self, handle: OrderHandle) -> None:
        self._need()
        h = self._orders.get(handle.order_ref)
        if h and not h.is_done:
            h.status, h.last_update_utc = "Cancelled", _now()

    def refresh(self) -> list[OrderHandle]:
        self._need()
        for h in self._orders.values():
            if h.is_done:
                continue
            mode = self.fill_mode_by_symbol.get(h.symbol, self.fill_mode)
            if mode == "none":
                continue
            step = h.remaining if mode == "immediate" else max(1.0, math.floor(h.quantity * self.partial_fraction))
            step = min(step, h.remaining)
            px = h.limit_price or self.quotes[h.symbol]["last"]
            h.avg_fill_price = (h.avg_fill_price * h.filled + px * step) / (h.filled + step)
            h.filled += step
            h.remaining -= step
            self.pos[h.symbol] = self.pos.get(h.symbol, 0) + (step if h.action == "BUY" else -step)
            h.status = "Filled" if h.remaining <= 0 else "Submitted"
            h.last_update_utc = _now()
        return [dataclasses.replace(h) for h in self._orders.values()]

    def open_orders(self) -> list[OrderHandle]:
        self._need()
        return [dataclasses.replace(h) for h in self._orders.values() if not h.is_done]
