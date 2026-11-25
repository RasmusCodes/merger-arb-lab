"""Order-spec export without any broker connection.

    python -m mergerarb.execution.cli --target-shares 2 --target-limit 487.00 --acquirer-limit 131.60 --out plan
writes plan.json and plan.csv (exact 4.5 hedge: SELL 9 MRVL, BUY 2 LITE) and prints the static checks.
"""
from __future__ import annotations

import argparse
import pathlib

from ..deal import load_deal
from .orders import build_pair_plan, validate_plan


def main(argv=None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--deal", default="mrvl_lite")
    ap.add_argument("--target-shares", type=int, required=True)
    ap.add_argument("--target-limit", type=float, required=True)
    ap.add_argument("--acquirer-limit", type=float, required=True)
    ap.add_argument("--target-ref", type=float, default=None, help="reference price for the band check")
    ap.add_argument("--acquirer-ref", type=float, default=None)
    ap.add_argument("--out", default="plan")
    a = ap.parse_args(argv)
    deal = load_deal(a.deal)
    refs = {deal.target.ticker: a.target_ref or a.target_limit, deal.acquirer.ticker: a.acquirer_ref or a.acquirer_limit}
    plan = build_pair_plan(deal, a.target_shares, a.target_limit, a.acquirer_limit, refs)
    out = pathlib.Path(a.out)
    out.with_suffix(".json").write_text(plan.to_json(), encoding="utf-8")
    out.with_suffix(".csv").write_text(plan.to_csv(), encoding="utf-8")
    for name, st, msg in validate_plan(plan, "paper", 15_000, 0.02):
        print(f"[{st}] {name}: {msg}")
    print(f"wrote {out.with_suffix('.json')} and {out.with_suffix('.csv')} (plan {plan.short_hash}); "
          f"nothing was sent to any broker")


if __name__ == "__main__":
    main()
