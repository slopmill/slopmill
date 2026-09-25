# SPDX-License-Identifier: AGPL-3.0-or-later
"""Model passes run in a thread; the browser follows them through an event stream.

Every event has a sequence number, so a browser that reconnects asks for what it missed
instead of guessing. One pass per issue at a time, and one model call on the machine at a
time: the model's plan is shared with other work.
"""
import threading
import time
import uuid

MODEL_SLOT = threading.Semaphore(1)


class Bus:
    def __init__(self, keep=5000):
        self.events = []
        self.seq = 0
        self.keep = keep
        self.cond = threading.Condition()

    def publish(self, type_, **data):
        with self.cond:
            self.seq += 1
            ev = {"seq": self.seq, "type": type_, "ts": time.time(), **data}
            self.events.append(ev)
            if len(self.events) > self.keep:
                del self.events[: len(self.events) - self.keep]
            self.cond.notify_all()
            return ev

    def since(self, seq):
        """Events after seq, and whether some were already dropped (the caller should
        reload state rather than trust a partial replay)."""
        with self.cond:
            gap = bool(self.events) and seq < self.events[0]["seq"] - 1
            return [e for e in self.events if e["seq"] > seq], gap


class Job:
    def __init__(self, kind):
        self.id = uuid.uuid4().hex[:10]
        self.kind = kind
        self.status = "running"
        self.started = time.time()
        self.finished = None
        self.cancel = threading.Event()
        self.committed = False   # set, under the issue lock, once the result is written
        self.blocks = {}      # block id -> status, for a browser that joins mid-pass

    def to_json(self):
        return {"id": self.id, "kind": self.kind, "status": self.status,
                "started": self.started, "finished": self.finished, "blocks": self.blocks}


class Runner:
    def __init__(self):
        self.buses = {}
        self.jobs = {}
        self.lock = threading.Lock()

    def bus(self, slug):
        with self.lock:
            return self.buses.setdefault(slug, Bus())

    def current(self, slug):
        job = self.jobs.get(slug)
        return job if job and job.status == "running" else None

    def last(self, slug):
        return self.jobs.get(slug)

    def start(self, slug, kind, fn):
        """fn(job, log, block_event) runs in a thread. Returns the job, or None if one is
        already running for this issue."""
        with self.lock:
            if self.current(slug):
                return None
            job = Job(kind)
            self.jobs[slug] = job
        bus = self.bus(slug)

        def log(level, text):
            bus.publish("log", level=level, text=text, job=job.id)

        def block_event(bid, status, detail=""):
            job.blocks[bid] = {"status": status, "detail": detail}
            bus.publish("block", block=bid, status=status, detail=detail, job=job.id)

        def run():
            bus.publish("job", status="running", kind=kind, job=job.id, started=job.started)
            try:
                fn(job, log, block_event)
                job.status = "stopped" if job.cancel.is_set() else "done"
            except Exception as e:     # reported to the page, never swallowed
                job.status = "stopped" if job.cancel.is_set() else "failed"
                if job.status == "failed":
                    log("error", f"{type(e).__name__}: {e}")
            finally:
                job.finished = time.time()
                bus.publish("job", status=job.status, kind=kind, job=job.id,
                            elapsed=round(job.finished - job.started, 1))

        threading.Thread(target=run, name=f"job-{slug}", daemon=True).start()
        return job
