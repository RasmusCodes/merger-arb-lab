"""Append-only JSONL journal of every execution event (submit, modify, cancel, status, fill, error, alert).

The journal never stores credentials (none are handled by this package: authentication happens in TWS/Gateway)."""
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
        return [json.loads(x) for x in lines if x.strip()]

    def submitted_keys(self) -> set[str]:
        return {r.get("idempotency_key") for r in self.read() if r.get("event") == "plan_submitted"}
