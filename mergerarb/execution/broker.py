"""Broker adapters behind one small interface.

* ``IBKRBroker`` - TWS / IB Gateway via ib_async (maintained fork of ib_insync; falls back to ib_insync if that is
  what is installed). Single-threaded by design: call it only from the thread that owns it.
* ``SimBroker`` - an offline simulator with the same interface and ib_async's observable behaviour where it matters
  (rejections arrive as 'Cancelled' + error text, a reconnect drops the session's cached orders, orders that ended
  while disconnected come back with filled=0 and their fills only in executions, what-if can fail or time out).
  It never touches the network.

Facts the executor relies on: ``refresh()`` returns what the library reports for every strategy order (orderRef
"MA:..."), ``executions()`` returns the fills; the executor merges the two (fills are taken from executions).
"""
from __future__ import annotations

import asyncio
import dataclasses
import datetime as dt
import math
from typing import Protocol

from .orders import OrderLeg

UNSET = 1.7976931348623157e308
DATA_TYPES = {1: "live", 2: "frozen", 3: "delayed", 4: "delayed-frozen"}
TERMINAL_STATUSES = ("Filled", "Cancelled", "ApiCancelled", "Inactive", "Unknown")
READ_ONLY_HINT = ("IBKR's what-if margin check is required before any order. TWS refuses or never answers what-if "
                  "requests while 'Read-Only API' is ticked (Configure > API > Settings): untick it to trade, and "
                  "check that TWS / IB Gateway is logged in and responsive")
# simulator-only Reg T / IBKR-style rates (kept here so the simulator does not depend on the analytics package)
_SIM_REGT_INITIAL, _SIM_LONG_MAINT, _SIM_SHORT_MAINT = 0.50, 0.25, 0.30


class WhatIfError(RuntimeError):
    """The what-if request failed, came back empty or timed out."""


@dataclasses.dataclass
class BrokerQuote:
    symbol: str
    last: float | None
    bid: float | None
    ask: float | None
    prev_close: float | None
    time_utc: dt.datetime | None
    data_type: str              # live / delayed / frozen / delayed-frozen / simulated / unknown


@dataclasses.dataclass
class ShortInfo:
    symbol: str
    level: float | None          # IBKR tick 46: >2.5 shortable (easy), 1.5-2.5 locate needed, <=1.5 not shortable
    shares: float | None         # IBKR tick 89

    @property
    def known(self) -> bool:
        return self.level is not None and not (isinstance(self.level, float) and math.isnan(self.level))

    def status(self, needed: int) -> tuple[str, str]:
        if not self.known:
            return "FAIL", "shortability unknown: no IBKR tick 46 received (with delayed data it may never arrive)"
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
    currency: str = "USD"


@dataclasses.dataclass
class OrderHandle:
    order_id: int
    order_ref: str
    symbol: str
    action: str
    quantity: int
    limit_price: float | None
    status: str = "PendingSubmit"   # IBKR: PendingSubmit, PreSubmitted, Submitted, ValidationError, PendingCancel,
    filled: float = 0.0             # Filled, Cancelled, ApiCancelled, Inactive; 'Unknown' = not reported by the broker
    remaining: float = 0.0
    avg_fill_price: float = 0.0
    last_update_utc: str = ""
    error: str = ""

    @property
    def is_done(self) -> bool:
        return self.status in TERMINAL_STATUSES

    @property
    def unfilled(self) -> float:
        return max(float(self.quantity) - float(self.filled), 0.0)


@dataclasses.dataclass
class ExecFill:
    order_ref: str
    exec_id: str
    symbol: str
    side: str            # BOT / SLD
    shares: float
    price: float
    cum_qty: float       # cumulative quantity of the order after this execution
    avg_price: float
    time_utc: str = ""


class BrokerClient(Protocol):
    def connect(self) -> None: ...          # idempotent: a no-op while connected
    def disconnect(self) -> None: ...
    def is_connected(self) -> bool: ...
    def accounts(self) -> list[str]: ...
    def account_snapshot(self, account: str) -> AccountSnapshot: ...
    def positions(self, account: str) -> dict[str, float]: ...
    def quote(self, symbol: str, primary_exchange: str = "NASDAQ") -> BrokerQuote: ...
    def short_info(self, symbol: str, primary_exchange: str = "NASDAQ") -> ShortInfo: ...
    def what_if(self, leg: OrderLeg, account: str, quantity: int | None = None,
                limit_price: float | None = None) -> WhatIf: ...      # raises WhatIfError
    def place(self, leg: OrderLeg, order_ref: str, account: str, quantity: int | None = None,
              limit_price: float | None = None) -> OrderHandle: ...
    def cancel(self, handle: OrderHandle) -> None: ...
    def refresh(self) -> list[OrderHandle]: ...
    def open_orders(self) -> list[OrderHandle]: ...
    def executions(self) -> list[ExecFill]: ...


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
    Settings: 'Enable ActiveX and Socket Clients', trusted IP 127.0.0.1). 'Read-Only API' must be unticked for
    what-if checks and orders. Authentication happens in TWS/Gateway; this class never sees credentials.

    ib_async discards its whole session state (trades, tickers, fills) on every disconnect, so nothing here caches
    library objects across calls: every refresh reads the library's current Trade objects, and fills come from
    executions (orders that completed while disconnected come back with filled=0)."""

    def __init__(self, cfg):
        try:
            import ib_async as ibm
        except ImportError:  # pragma: no cover - fallback for older installs
            import ib_insync as ibm
        self.ibm, self.cfg = ibm, cfg
        self.ib = ibm.IB()
        self.ib.RequestTimeout = float(getattr(cfg, "request_timeout_s", 10.0))
        self._contracts: dict[str, object] = {}
        self._tickers: dict[str, object] = {}
        self._order_errors: dict[int, str] = {}
        self._last_error = ""
        self.ib.errorEvent += self._on_error

    def _on_error(self, reqId, errorCode, errorString, contract=None) -> None:
        msg = f"{errorCode}: {errorString}"
        self._last_error = msg
        if isinstance(reqId, int) and reqId > 0:
            self._order_errors[reqId] = msg

    def connect(self) -> None:
        if self.ib.isConnected():
            return                      # idempotent: a second connect would drop and rebuild the live session
        self._tickers.clear()           # market-data subscriptions die with the socket
        self.ib.RequestTimeout = float(getattr(self.cfg, "request_timeout_s", 10.0))
        self.ib.connect(self.cfg.host, self.cfg.effective_port, clientId=self.cfg.client_id, timeout=15)
        # 3 = real-time if subscribed, otherwise delayed; every quote is labelled with its actual type
        self.ib.reqMarketDataType(3)
        try:
            self.ib.reqExecutions()     # fills of orders that completed while we were away
        except Exception:  # noqa: BLE001 - the startup sync already asked for executions; this is a refresh
            pass

    def disconnect(self) -> None:
        self.ib.disconnect()
        self._tickers.clear()

    def is_connected(self) -> bool:
        return bool(self.ib.isConnected())

    def _need(self) -> None:
        if not self.is_connected():
            raise ConnectionError("not connected to TWS / IB Gateway")

    def accounts(self) -> list[str]:
        self._need()
        return list(self.ib.managedAccounts())

    def account_snapshot(self, account: str) -> AccountSnapshot:
        self._need()
        vals: dict[str, dict[str, str]] = {}
        for v in self.ib.accountSummary(account):
            vals.setdefault(v.tag, {})[v.currency] = v.value

        def g(tag: str) -> tuple[float, str]:
            d = vals.get(tag, {})
            for cur in ("USD", "BASE", *d.keys()):     # base-currency rows when the account is not USD-based
                if _f(d.get(cur)) is not None:
                    return _f(d.get(cur)), cur
            return 0.0, ""

        nl, cur = g("NetLiquidation")
        return AccountSnapshot(account, nl, g("AvailableFunds")[0], g("ExcessLiquidity")[0], g("BuyingPower")[0],
                               g("InitMarginReq")[0], g("MaintMarginReq")[0], cur or "USD")

    def positions(self, account: str) -> dict[str, float]:
        self._need()
        return {p.contract.symbol: float(p.position) for p in self.ib.positions(account)}

    def _contract(self, symbol: str, primary: str):
        if symbol not in self._contracts:
            c = self.ibm.Stock(symbol, "SMART", "USD", primaryExchange=primary)
            q = self.ib.qualifyContracts(c)
            if not q or q[0] is None or not getattr(q[0], "conId", 0):     # ib_async returns [None] on failure
                raise RuntimeError(f"could not qualify contract {symbol} (unknown or ambiguous at IBKR)")
            self._contracts[symbol] = q[0]
        return self._contracts[symbol]

    def _ticker(self, symbol: str, primary: str):
        self._need()
        if symbol not in self._tickers:
            self._tickers[symbol] = self.ib.reqMktData(self._contract(symbol, primary), "236", False, False)
            self.ib.sleep(2.0)
        else:
            self.ib.sleep(0.1)
        return self._tickers[symbol]

    def quote(self, symbol: str, primary_exchange: str = "NASDAQ") -> BrokerQuote:
        t = self._ticker(symbol, primary_exchange)
        tm = getattr(t, "time", None)
        if isinstance(tm, dt.datetime) and tm.tzinfo is None:
            tm = tm.replace(tzinfo=dt.timezone.utc)
        return BrokerQuote(symbol, _f(t.last), _f(t.bid), _f(t.ask), _f(t.close),
                           tm if isinstance(tm, dt.datetime) else None,
                           DATA_TYPES.get(int(getattr(t, "marketDataType", 0) or 0), "unknown"))

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
        o.account, o.tif, o.outsideRth, o.transmit = account, "DAY", False, True
        return o

    def what_if(self, leg: OrderLeg, account: str, quantity: int | None = None,
                limit_price: float | None = None) -> WhatIf:
        self._need()
        contract = self._contract(leg.symbol, leg.primary_exchange)
        self._last_error = ""
        try:
            st = self.ib.whatIfOrder(contract, self._order(leg, account, quantity, limit_price))
        except (asyncio.TimeoutError, TimeoutError) as e:
            raise WhatIfError(f"what-if for {leg.symbol} not answered within {self.ib.RequestTimeout:.0f}s") from e
        if not hasattr(st, "initMarginChange"):        # ib_async returns [] when TWS rejects the what-if
            raise WhatIfError(f"what-if for {leg.symbol} rejected ({self._last_error or 'no reason given'})")
        init, maint = _f(st.initMarginChange), _f(st.maintMarginChange)
        if init is None or maint is None:
            raise WhatIfError(f"what-if for {leg.symbol} returned no margin numbers")
        return WhatIf(init, maint, _f(st.equityWithLoanChange), _f(st.commission), getattr(st, "warningText", "") or "")

    def _handle(self, trade) -> OrderHandle:
        o, s = trade.order, trade.orderStatus
        err = self._order_errors.get(o.orderId, "")
        for e in getattr(trade, "log", [])[-5:]:
            if getattr(e, "errorCode", 0):
                err = e.message or err
        return OrderHandle(int(o.orderId or 0), o.orderRef or "", trade.contract.symbol, o.action, int(o.totalQuantity),
                           _f(getattr(o, "lmtPrice", None)), s.status or "PendingSubmit", float(s.filled or 0.0),
                           float(s.remaining or 0.0), float(s.avgFillPrice or 0.0), _now(), err)

    def place(self, leg: OrderLeg, order_ref: str, account: str, quantity: int | None = None,
              limit_price: float | None = None) -> OrderHandle:
        self._need()
        o = self._order(leg, account, quantity, limit_price)
        o.orderRef = order_ref
        trade = self.ib.placeOrder(self._contract(leg.symbol, leg.primary_exchange), o)
        self.ib.sleep(0.5)
        return self._handle(trade)

    def cancel(self, handle: OrderHandle) -> None:
        self._need()
        tr = next((t for t in self.ib.trades() if (t.order.orderRef or "") == handle.order_ref and not t.isDone()),
                  None)
        if tr is not None:
            self.ib.cancelOrder(tr.order)
        elif handle.order_id:
            self.ib.cancelOrder(self.ibm.Order(orderId=handle.order_id, clientId=self.cfg.client_id))
        else:
            raise LookupError(f"order {handle.order_ref} not found at the broker")
        self.ib.sleep(0.5)

    def refresh(self) -> list[OrderHandle]:
        self._need()
        self.ib.sleep(0.05)
        out: dict[str, OrderHandle] = {}
        for t in self.ib.trades():                      # the library's current objects, never a stale cache
            ref = t.order.orderRef or ""
            if ref.startswith("MA:"):
                h = self._handle(t)
                if ref not in out or h.filled >= out[ref].filled:
                    out[ref] = h
        return list(out.values())

    def open_orders(self) -> list[OrderHandle]:
        self._need()
        self.ib.reqAllOpenOrders()
        self.ib.sleep(0.2)
        return [self._handle(t) for t in self.ib.openTrades() if (t.order.orderRef or "").startswith("MA:")]

    def executions(self) -> list[ExecFill]:
        self._need()
        perm = {t.order.permId: (t.order.orderRef or "") for t in self.ib.trades() if t.order.permId}
        out = []
        for f in self.ib.fills():
            e = f.execution
            ref = e.orderRef or perm.get(e.permId, "")
            if not ref.startswith("MA:"):
                continue
            tm = f.time.isoformat() if isinstance(f.time, dt.datetime) else ""
            out.append(ExecFill(ref, e.execId, f.contract.symbol, e.side, float(e.shares), float(e.price),
                                float(e.cumQty or 0.0), float(e.avgPrice or 0.0), tm))
        return out


# ------------------------------------------------------------------------------------------------------------------
class SimBroker:
    """Offline simulator. ``fill_mode``: 'immediate' (fills at the limit on the next market step), 'partial' (fills
    ``partial_fraction`` per step), 'none' (rests). ``refresh()`` runs one market step; ``step()`` runs one while
    disconnected (orders keep working at the broker). Faithful to ib_async where it matters:

    * a rejected order shows status 'Cancelled' plus an error text (ib_async maps order errors to Cancelled);
      short sales are rejected when ``short_level`` < 1.5, any order in ``reject_symbols`` is rejected;
    * a cancel goes PendingCancel first; with ``cancel_fill_race`` the order can still fill before the cancel lands;
    * a reconnect drops the session state like ``wrapper.reset()``: orders that are no longer working come back as
      completed orders with filled=0, their fills only in ``executions()``; market-data subscriptions are gone;
    * ``what_if_mode`` 'ok' / 'error' / 'timeout' / 'empty';
    * ``mismark_cancelled()`` reproduces ib_async marking a still-working order Cancelled after a TWS error;
    * ``expire()`` ends working orders the way a DAY order expires (Cancelled, no error text).
    It never touches the network."""

    def __init__(self, account: str = "DU0000001", net_liq: float = 10_000.0, quotes: dict | None = None,
                 short_level: float = 3.0, short_shares: float = 10_000_000, fill_mode: str = "immediate",
                 partial_fraction: float = 0.5, data_type: str = "simulated", fill_mode_by_symbol: dict | None = None,
                 what_if_mode: str = "ok", reject_symbols=(), cancel_fill_race: bool = False,
                 fill_on_place: bool = False):
        self.account, self.net_liq = account, net_liq
        self.quotes = quotes or {}
        self.short_level, self.short_shares = short_level, short_shares
        self.fill_mode, self.partial_fraction = fill_mode, partial_fraction
        self.fill_mode_by_symbol = dict(fill_mode_by_symbol or {})
        self.data_type = data_type
        self.what_if_mode = what_if_mode
        self.reject_symbols = set(reject_symbols or ())
        self.cancel_fill_race, self.fill_on_place = cancel_fill_race, fill_on_place
        self.connected = False
        self.connect_calls = 0
        self.session = 0
        self._orders: dict[str, OrderHandle] = {}           # the broker's truth
        self._completed_view: set[str] = set()              # reported with filled=0 after a reconnect
        self._mismarked: dict[str, tuple[str, float]] = {}
        self._execs: list[ExecFill] = []
        self._subscriptions: set[str] = set()
        self._next_id = 1000
        self.pos: dict[str, float] = {}

    # ----- session --------------------------------------------------------------------------------------------
    def connect(self) -> None:
        if self.connected:
            return
        self.connect_calls += 1
        if self.session:      # a reconnect: the library rebuilds its state from TWS
            self._completed_view |= {r for r, h in self._orders.items() if h.is_done}
            self._mismarked.clear()
        self.session += 1
        self._subscriptions = set()
        self.connected = True

    def disconnect(self) -> None:
        self.connected = False

    def is_connected(self) -> bool:
        return self.connected

    def _need(self) -> None:
        if not self.connected:
            raise ConnectionError("not connected")

    # ----- account and data -----------------------------------------------------------------------------------
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
        maint = _SIM_LONG_MAINT * lv + _SIM_SHORT_MAINT * sv
        init = _SIM_REGT_INITIAL * (lv + sv)
        return AccountSnapshot(account, self.net_liq, self.net_liq - init, self.net_liq - maint,
                               2 * (self.net_liq - init), init, maint)

    def positions(self, account: str) -> dict[str, float]:
        self._need()
        return dict(self.pos)

    def quote(self, symbol: str, primary_exchange: str = "NASDAQ") -> BrokerQuote:
        self._need()
        self._subscriptions.add(symbol)
        q = self.quotes[symbol]
        return BrokerQuote(symbol, q.get("last"), q.get("bid"), q.get("ask"), q.get("prev_close"),
                           q.get("time_utc") or dt.datetime.now(dt.timezone.utc), self.data_type)

    def short_info(self, symbol: str, primary_exchange: str = "NASDAQ") -> ShortInfo:
        self._need()
        return ShortInfo(symbol, self.short_level, self.short_shares)

    def what_if(self, leg: OrderLeg, account: str, quantity: int | None = None,
                limit_price: float | None = None) -> WhatIf:
        self._need()
        if self.what_if_mode == "timeout":
            raise WhatIfError(f"what-if for {leg.symbol} not answered within the request timeout")
        if self.what_if_mode == "error":
            raise WhatIfError(f"what-if for {leg.symbol} rejected (201: Order rejected - simulated)")
        if self.what_if_mode == "empty":
            return WhatIf(None, None, None, None, "")
        q = quantity if quantity is not None else leg.quantity
        px = limit_price or leg.limit_price or self.quotes[leg.symbol]["last"]
        v = q * px
        return WhatIf(_SIM_REGT_INITIAL * v, (_SIM_LONG_MAINT if leg.action == "BUY" else _SIM_SHORT_MAINT) * v,
                      0.0, max(1.0, 0.005 * q))

    # ----- orders ---------------------------------------------------------------------------------------------
    def _view(self, h: OrderHandle) -> OrderHandle:
        v = dataclasses.replace(h)
        if h.order_ref in self._completed_view:
            v.filled, v.avg_fill_price = 0.0, 0.0           # completed orders carry no fill numbers in ib_async
        mm = self._mismarked.get(h.order_ref)
        if mm:
            if h.is_done or h.filled > mm[1]:               # the next orderStatus from TWS overwrites the wrong status
                del self._mismarked[h.order_ref]
            else:
                v.status, v.error = "Cancelled", mm[0]
        return v

    def _fill(self, h: OrderHandle) -> None:
        mode = self.fill_mode_by_symbol.get(h.symbol, self.fill_mode)
        if mode == "none" or h.remaining <= 0:
            return
        step = h.remaining if mode == "immediate" else max(1.0, math.floor(h.quantity * self.partial_fraction))
        step = min(step, h.remaining)
        px = h.limit_price or self.quotes[h.symbol]["last"]
        h.avg_fill_price = (h.avg_fill_price * h.filled + px * step) / (h.filled + step)
        h.filled += step
        h.remaining -= step
        self.pos[h.symbol] = self.pos.get(h.symbol, 0) + (step if h.action == "BUY" else -step)
        self._execs.append(ExecFill(h.order_ref, f"sim{h.order_id:06d}.{len(self._execs):05d}.01.01", h.symbol,
                                    "BOT" if h.action == "BUY" else "SLD", step, px, h.filled, h.avg_fill_price, _now()))
        if h.status != "PendingCancel":
            h.status = "Filled" if h.remaining <= 0 else "Submitted"
        h.last_update_utc = _now()

    def step(self) -> None:
        """One market step; also runs while the API is disconnected."""
        for h in self._orders.values():
            if h.is_done:
                continue
            if h.status == "PendingCancel" and not self.cancel_fill_race:
                h.status, h.last_update_utc = "Cancelled", _now()
                continue
            self._fill(h)
            if h.status == "PendingCancel":
                h.status = "Filled" if h.remaining <= 0 else "Cancelled"

    def place(self, leg: OrderLeg, order_ref: str, account: str, quantity: int | None = None,
              limit_price: float | None = None) -> OrderHandle:
        self._need()
        q = int(quantity if quantity is not None else leg.quantity)
        self._next_id += 1
        px = limit_price if limit_price is not None else leg.limit_price
        h = OrderHandle(self._next_id, order_ref, leg.symbol, leg.action, q, px, "Submitted", 0.0, float(q), 0.0, _now())
        reason = None
        if leg.symbol in self.reject_symbols:
            reason = "Order rejected - reason: simulated broker rejection"
        elif leg.action == "SELL" and self.pos.get(leg.symbol, 0) <= 0 and self.short_level < 1.5:
            reason = "Order rejected - reason: no shares available to borrow for this short sale"
        if reason:
            h.status, h.error = "Cancelled", f"Error 201, reqId {self._next_id}: {reason}"
        self._orders[order_ref] = h
        if self.fill_on_place and not h.is_done:
            self._fill(h)
        return self._view(h)

    def cancel(self, handle: OrderHandle) -> None:
        self._need()
        h = self._orders.get(handle.order_ref)
        if h is None:
            raise LookupError(f"order {handle.order_ref} not found at the broker")
        if h.is_done:
            h.error = "161: Cancel attempted when order is not in a cancellable state"
            return
        h.status, h.last_update_utc = "PendingCancel", _now()

    def refresh(self) -> list[OrderHandle]:
        self._need()
        self.step()
        return [self._view(h) for h in self._orders.values()]

    def open_orders(self) -> list[OrderHandle]:
        self._need()
        return [v for v in (self._view(h) for h in self._orders.values()) if not v.is_done]

    def executions(self) -> list[ExecFill]:
        self._need()
        return [dataclasses.replace(e) for e in self._execs]

    # ----- test hooks -----------------------------------------------------------------------------------------
    def mismark_cancelled(self, order_ref: str, error: str = "Error 10349, simulated error on a working order") -> None:
        self._mismarked[order_ref] = (error, self._orders[order_ref].filled)

    def expire(self, symbol: str | None = None) -> None:
        for h in self._orders.values():
            if not h.is_done and (symbol is None or h.symbol == symbol):
                h.status, h.last_update_utc = "Cancelled", _now()
