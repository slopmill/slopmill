# SPDX-License-Identifier: AGPL-3.0-or-later
"""The meter's ledger: one line per model call in WORKSPACE/usage.jsonl.

Tokens are exact when the provider reported them and estimated (about four characters a
token) when it did not; every line says which, and a total that includes any estimate is
shown as an estimate. A call that failed or was stopped is recorded too, marked as such:
it may still have been billed.

Totals are kept in memory and brought up to date from where the file was last read, so
they stay exact however long the ledger grows without re-reading all of it each time.
"""
import collections
import json
import os
import threading
import time

RECENT = 12


def _blank():
    return {"in": 0, "out": 0, "images": 0, "calls": 0, "exact": True}


def _add(t, e):
    t["in"] += int(e.get("in") or 0)
    t["out"] += int(e.get("out") or 0)
    t["images"] += int(e.get("images") or 0)
    t["calls"] += 1
    if not e.get("exact", False) and (e.get("in") or e.get("out")):
        t["exact"] = False


def _day(ts):
    return time.strftime("%Y-%m-%d", time.localtime(ts))


class Ledger:
    def __init__(self, workspace_root):
        self.path = os.path.join(os.path.abspath(workspace_root), "usage.jsonl")
        self.lock = threading.Lock()
        self._reset()

    def _reset(self):
        self._offset = 0
        self._by_slug = collections.defaultdict(_blank)
        self._by_day = collections.defaultdict(_blank)
        self._recent = collections.defaultdict(lambda: collections.deque(maxlen=RECENT))

    def record(self, **entry):
        entry = {"ts": time.time(), **entry}
        line = json.dumps(entry, ensure_ascii=False) + "\n"
        with self.lock:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line)
        return entry

    def _catch_up(self):
        """Fold in every complete line written since the last read (caller holds the lock)."""
        try:
            size = os.path.getsize(self.path)
        except OSError:
            return
        if size < self._offset:          # the file was replaced or truncated: start over
            self._reset()
        with open(self.path, "rb") as f:
            f.seek(self._offset)
            raw = f.read()
        end = raw.rfind(b"\n") + 1       # a half-written last line waits for the next read
        for ln in raw[:end].decode("utf-8", "replace").splitlines():
            try:
                e = json.loads(ln)
            except ValueError:
                continue
            if not isinstance(e, dict):
                continue
            _add(self._by_day[_day(e.get("ts") or 0)], e)
            if e.get("slug"):
                _add(self._by_slug[e["slug"]], e)
                self._recent[e["slug"]].append(e)
        self._offset += end

    def entries(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                return [json.loads(ln) for ln in f if ln.strip()]
        except (OSError, ValueError):
            return []

    def summary(self, slug=None, now=None):
        with self.lock:
            self._catch_up()
            today = _day(now or time.time())
            return {"issue": dict(self._by_slug[slug]) if slug else _blank(),
                    "today": dict(self._by_day[today]) if today in self._by_day else _blank(),
                    "recent": list(self._recent[slug])[::-1] if slug else []}
