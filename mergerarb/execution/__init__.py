"""Local-only, manually-approved order export and execution for ratio-hedged merger-arbitrage pairs.

Safety model (see docs/EXECUTION.md):
  * paper trading by default; live trading needs MERGERARB_ALLOW_LIVE=1, mode="live", a non-paper account AND a typed
    confirmation that names the mode, the account and the plan hash;
  * nothing is ever sent because a model says so: every plan needs an explicit approval object bound to its hash;
  * pairs are NOT atomic - legs are sequenced, partial fills are tracked, unhedged exposure raises alerts;
  * an order is "filled" only when the broker reports it filled; submission is never treated as execution;
  * every submit / modify / cancel / fill / error is appended to a local JSONL journal.
This package never runs on the public website.
"""
