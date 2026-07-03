"""RealTimeTimeline — wall-clock-driven SeQUeNCe timeline (DESIGN.md §4).

SeQUeNCe's stock Timeline pops events from a heap and *jumps* virtual time to each
event instantly. Here we instead pace execution against the wall clock so that an
event scheduled for simulation-time T fires at wall-time ``epoch + T*time_scale``.
This lets channel delays line up with real socket latency and lets events injected
by network listener threads preempt a pending sleep.

Time mapping (ns from time_ns(), ps in the simulator):
    now_ps         = (wall_ns - epoch_ns) * 1e3 / time_scale
    wall_ns(sim_ps)= epoch_ns + sim_ps * time_scale / 1e3

``time_scale = 1.0`` is real-time; ``time_scale > 1.0`` is slow-motion (events
spread out in wall-clock), which keeps sub-microsecond simulator delays above OS /
network jitter.
"""

from __future__ import annotations

import threading
from time import time_ns

from sequence.kernel.timeline import Timeline
from sequence.kernel.event import Event

from .guarded_stub import RemoteAccessError

_NS_PER_PS = 1e3  # 1 ns = 1000 ps

# Idle/poll cap (s): longest a waiting loop sleeps before re-checking stop/empty.
_MAX_WAIT_S = 0.2


class RealTimeTimeline(Timeline):
    """A Timeline whose ``run`` loop is paced by the wall clock and is safe to
    have events injected into from other threads (the network listeners).

    Attributes:
        time_scale (float): wall-seconds per simulation-second.
        remote_access_errors (list): RemoteAccessErrors caught during event
            execution — the §8.1 proof. Should be empty after a clean run.
    """

    def __init__(self, stop_time: int = 10 ** 23, time_scale: float = 1.0,
                 formalism: str = "ket_vector", epoch_ns: int | None = None):
        super().__init__(stop_time, formalism=formalism)
        self.time_scale = float(time_scale)
        self._epoch_ns = epoch_ns
        self._cond = threading.Condition()
        self._stop_flag = False
        self.remote_access_errors: list[RemoteAccessError] = []

    # -- time mapping ----------------------------------------------------------

    def set_epoch(self, epoch_ns: int) -> None:
        """Set the shared wall-clock epoch (distributed by the orchestrator)."""
        self._epoch_ns = epoch_ns

    def now(self) -> int:
        if self._epoch_ns is None:
            return self.time
        return int(round((time_ns() - self._epoch_ns) * _NS_PER_PS / self.time_scale))

    def _wall_deadline_ns(self, sim_ps: int) -> float:
        return self._epoch_ns + sim_ps * self.time_scale / _NS_PER_PS

    # -- thread-safe scheduling ------------------------------------------------

    def schedule(self, event: "Event") -> None:
        """Thread-safe replacement for Timeline.schedule.

        Resolves a string owner to its entity (as the base does), assigns the
        process number, pushes onto the heap, and wakes the run loop. Safe to call
        from listener threads — this is how inbound network frames become events.
        """
        with self._cond:
            if type(event.process.owner) is str:
                event.process.owner = self.get_entity_by_name(event.process.owner)
            self.schedule_counter += 1
            event.process.number = self.schedule_counter
            self.events.push(event)
            self._cond.notify()

    # Inbound network frames inject events via the same path.
    inject = schedule

    def stop_loop(self) -> None:
        """Ask the run loop to exit at the next opportunity (thread-safe)."""
        with self._cond:
            self._stop_flag = True
            self._cond.notify()

    # -- main loop -------------------------------------------------------------

    def run(self) -> None:
        if self._epoch_ns is None:
            self._epoch_ns = time_ns()
        self.is_running = True

        while True:
            due: list[Event] = []
            with self._cond:
                if self._stop_flag:
                    break
                # Wall-clock stop: terminate even if the queue is empty (a stalled
                # protocol waiting on a message that never arrives must not hang
                # forever — it self-terminates at stop_time). Matters on FABRIC too.
                if self.now() >= self.stop_time:
                    break
                if len(self.events) == 0:
                    # idle: wait for an injected event or stop
                    self._cond.wait(timeout=_MAX_WAIT_S)
                    continue

                top = self.events.top()
                if top.time >= self.stop_time:
                    break

                deadline_ns = self._wall_deadline_ns(top.time)
                wait_s = (deadline_ns - time_ns()) / 1e9
                if wait_s > 0:
                    # sleep until the earliest event is due (or a sooner one arrives)
                    self._cond.wait(timeout=min(wait_s, _MAX_WAIT_S))
                    continue

                # drain everything that is due now
                while len(self.events) > 0:
                    nxt = self.events.top()
                    if nxt.time >= self.stop_time:
                        break
                    if self._wall_deadline_ns(nxt.time) - time_ns() > 0:
                        break
                    ev = self.events.pop()
                    if ev.is_invalid():
                        continue
                    due.append(ev)

            # Execute outside the lock: handlers schedule new events via inject(),
            # which re-acquires the lock. Keep simulated time monotonic.
            for ev in due:
                if ev.time > self.time:
                    self.time = ev.time
                try:
                    ev.process.run()
                except RemoteAccessError as exc:
                    # The §8.1 gate: record rather than crash so the runner can
                    # report a count. A clean DistributedBB84 produces zero.
                    self.remote_access_errors.append(exc)
                self.run_counter += 1

        self.is_running = False
