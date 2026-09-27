"""A backend with scripted answers, for the watcher and the CLI without a host."""

from __future__ import annotations

from edarunner.backend import Handle, Live, Request
from edarunner.hosts import HostProbe


class FakeBackend:
    """Records every submit and stop; `alive` answers from `states`, one entry per call, the last one repeats."""

    name = "fake"

    def __init__(self) -> None:
        self.requests: list[Request] = []
        self.states: dict[str, list[Live]] = {}
        self.asked: list[list[str]] = []
        self.stopped: list[tuple[str, bool, tuple[int, ...]]] = []
        self.probes: dict[str, HostProbe | str] | None = None

    def submit(self, req: Request) -> Handle:
        self.requests.append(req)
        return Handle(self.name, str(len(self.requests)), req.host)

    def alive(self, handles):
        handles = list(handles)
        self.asked.append([h.id for h in handles])
        out = {}
        for h in handles:
            seq = self.states.get(h.id) or [Live.GONE]
            state = seq.pop(0) if len(seq) > 1 else seq[0]
            out[h] = (state, state.value)
        return out

    def stop(self, handle, hard, pgids=()):
        self.stopped.append((handle.id, hard, tuple(pgids)))

    def free(self, hosts):
        return self.probes

    def file_host(self, run):
        return str(run.get("host") or "")
