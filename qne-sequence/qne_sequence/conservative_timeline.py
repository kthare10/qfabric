# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Komal Thareja
#
# Author: Komal Thareja (kthare10@renci.org)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""A timeline that advances only as far as the central time authority allows.

``ConservativeTimeline`` keeps ``RealTimeTimeline``'s thread-safe event injection
but replaces its wall-clock pacing with grants from ``time_authority.TimeClient``:
an event at time *T* runs only once the authority has granted an LBTS ≥ *T*. Its
``now()`` is the *logical* clock (the time of the event being executed), so frames
are stamped with simulator time, not wall time, and ``Listener`` delivers them at
exactly ``t_send + delay`` — which, by the grant invariant, is never in the past.

Optional ``wall_guards`` are callables run before each batch of due events; the
runner uses one to wait for wall-clock quiescence of the (uncounted, lossy) raw
photon socket before Bob processes classical events that must follow the photons.
"""
from __future__ import annotations

from time import sleep, time_ns
from typing import Callable

from .guarded_stub import RemoteAccessError
from .rt_timeline import RealTimeTimeline
from .time_authority import REREPORT

_IDLE_WAIT_S = 0.02
_MAX_WALL_S = 900.0     # hard safety stop for a wedged run


class ConservativeTimeline(RealTimeTimeline):
    def __init__(self, client, counts: Callable[[], tuple[int, int]],
                 wall_guards: list[Callable[[], None]] | None = None,
                 stop_time: int = 10 ** 23, formalism: str = "ket_vector",
                 max_wall_s: float = _MAX_WALL_S):
        super().__init__(stop_time, time_scale=1.0, formalism=formalism)
        self.client = client
        self._counts = counts
        self.wall_guards = list(wall_guards or [])
        self.max_wall_s = max_wall_s
        self.granted: int = -1          # events with time <= granted may run
        self.rounds = 0                 # grants requested
        self.rereports = 0
        self.finished_by_authority = False
        self.timed_out = False

    # logical clock: the timestamp of the event being executed
    def now(self) -> int:
        return self.time

    def set_epoch(self, epoch_ns: int) -> None:   # wall epoch is irrelevant here
        self._epoch_ns = epoch_ns

    def run(self) -> None:
        self.is_running = True
        wall_deadline = time_ns() + int(self.max_wall_s * 1e9)
        while True:
            due = []
            # Snapshot the link counters BEFORE looking at the queue: a message the
            # link has counted as received is guaranteed to be in the queue already
            # (links count after on_frame), so "idle + counts balanced" really means
            # nothing is pending anywhere.
            sent, recv = self._counts()
            with self._cond:
                if self._stop_flag:
                    break
                while len(self.events) > 0:
                    top = self.events.top()
                    if top.time >= self.stop_time or top.time > self.granted:
                        break
                    ev = self.events.pop()
                    if ev.is_invalid():
                        continue
                    due.append(ev)
                    break               # one at a time: handlers may schedule earlier events
                next_ps = None
                if len(self.events) > 0 and self.events.top().time < self.stop_time:
                    next_ps = self.events.top().time
                if len(self.events) > 0 and self.events.top().time >= self.stop_time and not due:
                    break

            if due:
                for guard in self.wall_guards:
                    guard()
                for ev in due:
                    if ev.time > self.time:
                        self.time = ev.time
                    try:
                        ev.process.run()
                    except RemoteAccessError as exc:
                        self.remote_access_errors.append(exc)
                    self.run_counter += 1
                continue

            if time_ns() > wall_deadline:
                self.timed_out = True
                break

            # nothing runnable: ask the authority how far the world may advance
            lbts = self.client.request(next_ps, sent, recv)
            if lbts == REREPORT:
                self.rereports += 1
                sleep(0.002)          # let transients land, then re-snapshot
                continue
            self.rounds += 1
            if lbts is None:
                self.finished_by_authority = True
                break
            if lbts > self.granted:
                self.granted = lbts
            if next_ps is None or next_ps > self.granted:
                # idle, or blocked behind a slower node: wait for an injected event
                # (or the stop flag) before reporting again
                with self._cond:
                    if not self._stop_flag and (len(self.events) == 0
                                                or self.events.top().time > self.granted):
                        self._cond.wait(timeout=_IDLE_WAIT_S)
        self.is_running = False
