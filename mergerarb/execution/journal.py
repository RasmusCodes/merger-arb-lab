"""Append-only JSONL journal of every execution event (submit, status, fill, error, cancel, alert, approval, ...).

The service rebuilds unfinished plans from it at start-up. The journal never stores credentials (none are handled by
this package: authentication happens in TWS/Gateway)."""
from __future__ import annotations

import datetime as dt
import json
import pathlib
import threading


class Journal:
    def __init__(self, path: pathlib.Path):
        self.path = pathlib.Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def write(self, event: str, **payload) -> dict:
        rec = {"ts_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds"), "event": event, **payload}
        line = json.dumps(rec, default=str, sort_keys=True)
        with self._lock, self.path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        return rec

    def read(self, limit: int | None = None) -> list[dict]:
        if not self.path.exists():
            return []
        lines = self.path.read_text(encoding="utf-8").splitlines()
        if limit:
            lines = lines[-limit:]
        out = []
        for x in lines:
            if not x.strip():
                continue
            try:
                out.append(json.loads(x))
            except json.JSONDecodeError:      # a line cut short by a crash must not make the journal unreadable
                continue
        return out

    def submitted_keys(self) -> set[str]:
        return {r.get("idempotency_key") for r in self.read() if r.get("event") == "plan_submitted"}

    def submitted_plan_ids(self) -> set[str]:
        return {r.get("plan_id") for r in self.read() if r.get("event") == "plan_submitted"}
