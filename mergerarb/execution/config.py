"""Execution configuration. Defaults are deliberately conservative: paper mode, small caps, regular hours only."""
from __future__ import annotations

import dataclasses
import os
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]

# IBKR defaults: TWS paper 7497 / live 7496; IB Gateway paper 4002 / live 4001
PAPER_PORTS = {"tws": 7497, "gateway": 4002}
LIVE_PORTS = {"tws": 7496, "gateway": 4001}


@dataclasses.dataclass(frozen=True)
class ExecutionConfig:
    mode: str = "paper"                    # "paper" | "live"
    host: str = "127.0.0.1"
    app: str = "tws"                       # "tws" | "gateway"
    port: int | None = None                # None -> default for app + mode
    client_id: int = 17
    account: str | None = None             # None -> the single managed account (refuses if several)
    max_order_notional: float = 15_000.0   # per leg, USD
    max_gross_notional: float = 30_000.0   # whole strategy after the trade, USD
    max_unhedged_usd: float = 1_500.0      # alert threshold for leg imbalance, USD
    unhedged_timeout_s: float = 60.0       # how long an imbalance may persist before an alert
    price_band_pct: float = 0.02           # limit prices must sit within +/-2% of the reference price
    max_borrow_fee: float = 0.03           # hard stop above 3%/yr
    min_excess_liquidity_pct: float = 0.50  # post-trade excess liquidity >= 50% of net liquidation
    allow_outside_rth: bool = False
    allow_delayed_data_in_paper: bool = True
    approval_ttl_s: float = 300.0          # approvals expire after 5 minutes
    sequencing: str = "short_first"        # "short_first" | "simultaneous"
    hedge_policy: str = "alert_only"       # "alert_only" | "auto_within_band" (follow-up orders inside the approved band)
    journal_path: pathlib.Path = ROOT / "data" / "execution" / "journal.jsonl"

    @property
    def effective_port(self) -> int:
        if self.port:
            return self.port
        return (LIVE_PORTS if self.mode == "live" else PAPER_PORTS)[self.app]

    def live_allowed(self) -> bool:
        return self.mode == "live" and os.environ.get("MERGERARB_ALLOW_LIVE") == "1"

    def validate(self) -> list[str]:
        p = []
        if self.mode not in ("paper", "live"):
            p.append("mode must be 'paper' or 'live'")
        if self.mode == "live" and not self.live_allowed():
            p.append("live mode requested but MERGERARB_ALLOW_LIVE=1 is not set in the environment")
        if self.host not in ("127.0.0.1", "localhost", "::1"):
            p.append("TWS/Gateway host must be local (127.0.0.1); remote API access is not supported")
        if self.sequencing not in ("short_first", "simultaneous"):
            p.append("sequencing must be 'short_first' or 'simultaneous'")
        if self.hedge_policy not in ("alert_only", "auto_within_band"):
            p.append("hedge_policy must be 'alert_only' or 'auto_within_band'")
        return p
