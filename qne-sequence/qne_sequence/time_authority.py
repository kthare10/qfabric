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

"""One authoritative simulation clock for a distributed run — the *global timeline*.

Why. ``RealTimeTimeline`` ties simulation time to the wall clock: events fire at
``epoch + T·time_scale`` and a frame is "on time" only if it physically arrives
before ``t_send + delay`` in wall-clock terms. That makes emulation fidelity
hostage to things the model does not own — Python throughput, socket buffers, L2
retransmissions, clock-offset error. The 2026-08-04 slice run showed it: every
frame Bob received was late (worst 27 s) at a 245 µs modeled delay, and the
Cristian sync error alone (~350 µs) exceeded the modeled delay.

What. This module is the alternative the SeQUeNCe team asked for ("fix the timeline
on one node"): a **central time authority** running a conservative
(Chandy–Misra–Bryant-style) synchronization with a coordinator. Simulation time is
decoupled from the wall clock. Each node keeps its local event queue but may only
execute events whose timestamp is ≤ the last *grant* it received. The authority
computes the grant as the lower bound on any timestamp that can still be generated,

    LBTS = min over live nodes i of ( next_event_time_i + lookahead_i ),

where ``lookahead_i`` is the modeled one-way channel delay of node *i*'s links: a
node whose earliest pending event is at *T* cannot send anything that arrives before
``T + delay``. Messages already in flight are handled by message counting — a grant
is only issued when Σ sent == Σ received over all nodes; otherwise everyone is asked
to report again once the transients have landed. The result: **no receiver can ever
process an event past the arrival time of a message it has not yet received**, so
``late_events`` is zero by construction, at any wall-clock speed, on any network.
That is what makes the run an exact distributed execution of the sequential
simulator's schedule — the property the lookahead certificate measures.

Protocol (newline-delimited JSON over TCP; one connection per node):

    node → authority   {"kind":"hello",  "node":N, "lookahead_ps":L}
    node → authority   {"kind":"report", "node":N, "round":r, "next_ps":T|null,
                        "sent":S, "recv":R}
    authority → node   {"kind":"grant",  "round":r+1, "lbts":LBTS|null}   (null = global end)
    authority → node   {"kind":"rereport"}   (transients in flight: report again)
    node → authority   {"kind":"bye",    "node":N}

``next_ps`` is the node's earliest unexecuted event (null = idle). ``sent``/``recv``
are cumulative counts of *emulated classical* messages on the node's links (the raw
0x7101 photon path is lossy by design and is excluded; the runner covers it with a
wall-clock quiescence guard instead). Rounds are barrier-synchronous: a grant needs a
fresh report from every live node. A node that leaves (``bye``) stays in the message
counts with its last totals so its in-flight messages are still accounted for.

Run standalone (e.g. on the switch/orchestrator node):

    python -m qne_sequence.time_authority --port 57200 --nodes 2
"""
from __future__ import annotations

import argparse
import json
import socket
import threading
import time
from dataclasses import dataclass, field
from time import sleep

_INF = float("inf")
REREPORT = "rereport"       # TimeClient.request sentinel: transients in flight


@dataclass
class _NodeState:
    name: str
    lookahead_ps: int
    conn: socket.socket
    lock: threading.Lock = field(default_factory=threading.Lock)
    round: int = -1            # round of the latest report (-1 = none yet)
    next_ps: float = _INF      # earliest pending event (inf = idle)
    sent: int = 0
    recv: int = 0
    done: bool = False         # said bye

    def send(self, obj: dict) -> None:
        data = (json.dumps(obj, separators=(",", ":")) + "\n").encode("utf-8")
        with self.lock:
            try:
                self.conn.sendall(data)
            except OSError:
                pass


class TimeAuthority:
    """The coordinator. ``num_nodes`` nodes must say hello before any grant."""

    def __init__(self, host: str = "0.0.0.0", port: int = 0, num_nodes: int = 2,
                 settle_s: float = 0.002):
        self.host = host
        self.port = port
        self.num_nodes = num_nodes
        self.settle_s = settle_s
        self._nodes: dict[str, _NodeState] = {}
        self._lock = threading.Lock()
        self._round = 0
        self._ls: socket.socket | None = None
        self._threads: list[threading.Thread] = []
        self._running = False
        # observability
        self.grants = 0
        self.rereports = 0
        self.lbts_history: list[float] = []

    # -- lifecycle --------------------------------------------------------------

    def start(self) -> "TimeAuthority":
        family = socket.AF_INET6 if (":" in self.host or self.host == "") else socket.AF_INET
        ls = socket.socket(family, socket.SOCK_STREAM)
        ls.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        ls.bind((self.host, self.port))
        ls.listen(16)
        ls.settimeout(0.5)
        self._ls = ls
        self.port = ls.getsockname()[1]
        self._running = True
        t = threading.Thread(target=self._accept_loop, daemon=True)
        t.start()
        self._threads.append(t)
        return self

    def stop(self) -> None:
        self._running = False
        if self._ls is not None:
            try:
                self._ls.close()
            except OSError:
                pass
        with self._lock:
            for n in self._nodes.values():
                try:
                    n.conn.close()
                except OSError:
                    pass

    def serve_forever(self, timeout_s: float | None = None) -> None:
        """Block until every registered node has said bye (or ``timeout_s``)."""
        t0 = time.monotonic()
        while self._running:
            with self._lock:
                joined = len(self._nodes)
                all_done = joined >= self.num_nodes and all(n.done for n in self._nodes.values())
            if all_done:
                break
            if timeout_s is not None and time.monotonic() - t0 > timeout_s:
                break
            sleep(0.05)
        self.stop()

    # -- connections ----------------------------------------------------------------

    def _accept_loop(self) -> None:
        while self._running:
            try:
                conn, _ = self._ls.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            t = threading.Thread(target=self._node_loop, args=(conn,), daemon=True)
            t.start()
            self._threads.append(t)

    def _node_loop(self, conn: socket.socket) -> None:
        f = conn.makefile("r", encoding="utf-8")
        state: _NodeState | None = None
        try:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                msg = json.loads(line)
                kind = msg.get("kind")
                if kind == "hello":
                    state = _NodeState(msg["node"], int(msg["lookahead_ps"]), conn)
                    with self._lock:
                        self._nodes[state.name] = state
                    state.send({"kind": "hello_ack", "nodes": self.num_nodes})
                elif kind == "report" and state is not None:
                    with self._lock:
                        state.round = int(msg.get("round", 0))
                        nxt = msg.get("next_ps")
                        state.next_ps = _INF if nxt is None else float(nxt)
                        state.sent = int(msg.get("sent", 0))
                        state.recv = int(msg.get("recv", 0))
                        self._maybe_grant_locked()
                elif kind == "bye" and state is not None:
                    with self._lock:
                        # adopt the final counts: a node may have sent a frame
                        # after its last report, and a stale total would keep the
                        # remaining nodes in the transient check forever
                        if msg.get("sent") is not None:
                            state.sent = int(msg["sent"])
                            state.recv = int(msg.get("recv", state.recv))
                        state.done = True
                        state.next_ps = _INF
                        self._maybe_grant_locked()
                    break
        except (OSError, ValueError):
            pass
        finally:
            if state is not None:
                with self._lock:
                    if not state.done:
                        # connection lost = treat as departed so others are not stuck
                        state.done = True
                        state.next_ps = _INF
                        self._maybe_grant_locked()
            try:
                conn.close()
            except OSError:
                pass

    # -- the LBTS computation -------------------------------------------------------

    def _maybe_grant_locked(self) -> None:
        if len(self._nodes) < self.num_nodes:
            return                                   # not everyone has joined
        live = [n for n in self._nodes.values() if not n.done]
        if not live:
            return
        if any(n.round != self._round for n in live):
            return                                   # waiting for this round's reports
        total_sent = sum(n.sent for n in self._nodes.values())
        total_recv = sum(n.recv for n in self._nodes.values())
        if total_sent != total_recv:
            # transient messages in flight: a grant now could let a receiver run
            # past an arrival it has not seen. Ask everyone to report again.
            self.rereports += 1
            for n in live:
                n.round = -1
                n.send({"kind": "rereport"})
            return
        lbts = min(n.next_ps + n.lookahead_ps for n in live)
        self._round += 1
        self.grants += 1
        if lbts == _INF:
            # every live node is idle and nothing is in flight: the run is over
            self.lbts_history.append(_INF)
            for n in live:
                n.send({"kind": "grant", "round": self._round, "lbts": None})
            return
        self.lbts_history.append(lbts)
        for n in live:
            n.send({"kind": "grant", "round": self._round, "lbts": int(lbts)})


class TimeClient:
    """Node-side stub. ``request`` blocks until the authority grants a bound."""

    def __init__(self, host: str, port: int, node: str, lookahead_ps: int,
                 timeout_s: float = 300.0):
        self.host, self.port, self.node = host, port, node
        self.lookahead_ps = int(lookahead_ps)
        self.timeout_s = timeout_s
        self.round = 0
        self.reports = 0
        self.rereports = 0
        self._sock: socket.socket | None = None
        self._f = None

    def connect(self, retries: int = 200, delay: float = 0.05) -> "TimeClient":
        last = None
        for _ in range(retries):
            try:
                s = socket.create_connection((self.host, self.port), timeout=self.timeout_s)
                s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                self._sock = s
                self._f = s.makefile("r", encoding="utf-8")
                self._send({"kind": "hello", "node": self.node, "lookahead_ps": self.lookahead_ps})
                ack = self._recv()
                if ack.get("kind") != "hello_ack":
                    raise ConnectionError(f"time authority: unexpected {ack}")
                return self
            except OSError as exc:
                last = exc
                sleep(delay)
        raise ConnectionError(f"could not reach time authority {self.host}:{self.port}: {last}")

    def _send(self, obj: dict) -> None:
        self._sock.sendall((json.dumps(obj, separators=(",", ":")) + "\n").encode("utf-8"))

    def _recv(self) -> dict:
        line = self._f.readline()
        if not line:
            raise ConnectionError("time authority closed the connection")
        return json.loads(line)

    def request(self, next_ps: int | None, sent: int, recv: int):
        """Report ``next_ps`` (None = idle) with the message counts ``sent``/``recv``.

        Returns the granted LBTS (int), ``None`` when the authority declares the
        run finished (everyone idle, nothing in flight), or ``REREPORT`` when
        transients are in flight -- the caller must re-snapshot counts *then* its
        queue (in that order) and call again.
        """
        self._send({"kind": "report", "node": self.node, "round": self.round,
                    "next_ps": None if next_ps is None else int(next_ps),
                    "sent": int(sent), "recv": int(recv)})
        self.reports += 1
        msg = self._recv()
        kind = msg.get("kind")
        if kind == "rereport":
            self.rereports += 1
            return REREPORT
        if kind == "grant":
            self.round = int(msg["round"])
            lbts = msg.get("lbts")
            return None if lbts is None else int(lbts)
        raise ConnectionError(f"time authority: unexpected {msg}")

    def bye(self, sent: int | None = None, recv: int | None = None) -> None:
        """Leave the grant protocol, reporting final message counts.

        A departed node no longer constrains the LBTS (the others must not wait on
        it), but its counts stay in the in-flight totals so a message still on the
        wire is accounted for.
        """
        try:
            msg = {"kind": "bye", "node": self.node}
            if sent is not None:
                msg["sent"] = int(sent)
                msg["recv"] = int(recv if recv is not None else 0)
            self._send(msg)
        except OSError:
            pass
        try:
            self._sock.close()
        except OSError:
            pass


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="QFabric central time authority")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=57200)
    ap.add_argument("--nodes", type=int, default=2, help="nodes that must join")
    ap.add_argument("--timeout", type=float, default=None,
                    help="stop after this many seconds even if nodes remain")
    args = ap.parse_args(argv)
    ta = TimeAuthority(args.host, args.port, args.nodes).start()
    print(json.dumps({"time_authority": f"{args.host}:{ta.port}", "nodes": args.nodes}),
          flush=True)
    ta.serve_forever(args.timeout)
    print(json.dumps({"grants": ta.grants, "rereports": ta.rereports,
                      "final_lbts": (None if not ta.lbts_history or ta.lbts_history[-1] == _INF
                                     else ta.lbts_history[-1])}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
