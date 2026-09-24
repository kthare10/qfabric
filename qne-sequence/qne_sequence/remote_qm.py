"""RemoteQuantumManager — a proxy to a QuantumStateService over a Link (DESIGN §6.2).

Bob's node has no local quantum state (the entangled register lives in Alice's
service). This proxy turns Bob's ``measure_batch`` into a single MEASURE_REQ frame
over the existing TCP Link and blocks for the MEASURE_RESP — so the classical
coordination rides the real WAN while the entanglement bookkeeping stays authoritative
in the one service. A tiny synchronous RPC (queue-backed) is enough: the E91 flow is
strictly request/response, not event-driven.
"""

from __future__ import annotations

import json
import queue
from time import sleep, time_ns

_TAG = "__e91_rpc__"


class LogicalClock:
    """One simulation clock per NODE, shared by all of that node's RPC channels.

    A node is a single sequential entity: it cannot be at two simulation times at
    once, so a repeater station or an Alice serving several links must advance one
    clock, not one per link. Monotonic by construction.
    """

    def __init__(self, start_ps: int = 0):
        self.ps = int(start_ps)
        self.start_ps = int(start_ps)

    def advance_to(self, ps: int) -> int:
        if ps > self.ps:
            self.ps = ps
        return self.ps

    @property
    def elapsed_ps(self) -> int:
        return max(0, self.ps - self.start_ps)


class RpcChannel:
    """Synchronous framed RPC over a Link: send a typed frame, block for a reply.

    Install ``on_frame`` as the Link's callback. ``call``/``recv`` block for the
    next frame of the expected type; ``send`` is fire-and-forget. Frames are the
    same length-prefixed JSON the rest of the wire uses.

    Two pacing modes, both enforcing the same contract — every message is handed
    to the protocol at exactly ``t_send + delay`` in shared simulation time:

    * **Wall-clock** (default): outbound frames carry the sender's wall clock and
      inbound frames are held until ``t_send + delay`` in the local clock
      (``peer_offset_ns`` from timesync.sync_link translates between the two).
      Fidelity holds only while real wire latency stays under the modeled delay;
      misses are counted (``late_events`` / ``max_lateness_ns``).
    * **Logical** (``logical=True``, used with the central time authority): frames
      carry the sender's *simulation* clock and the receiver's clock jumps to
      ``t_send + delay`` on arrival. Nothing sleeps and nothing can be late — the
      arrival time IS the receiver's clock, exactly as in a sequential simulator.
      This is sound here because the post-processing phase is a strict
      request/response ping-pong: the waiting side executes nothing until the
      message lands, so the message order is the schedule. Local computation is
      modelled as instantaneous (as the timeline models its handlers), so a
      Cascade of N round trips costs N·2·delay of simulation time —
      ``sim_elapsed_ps``, a deterministic time-to-key contribution instead of one
      that depends on the wire.
    """

    def __init__(self, link, delay_ps: int = 0, peer_offset_ns: int = 0,
                 logical: bool = False, logical_start_ps: int = 0,
                 clock: "LogicalClock | None" = None):
        self.link = link
        self.delay_ps = int(delay_ps)
        self.delay_ns = int(delay_ps) // 1000
        self.peer_offset_ns = int(peer_offset_ns)
        # logical mode: an explicit shared clock, or a private one for a lone channel
        if clock is None and logical:
            clock = LogicalClock(logical_start_ps)
        self.clock = clock
        self.logical = clock is not None
        self._q: "queue.Queue[dict]" = queue.Queue()
        self.on_time_events = 0
        self.late_events = 0
        self.max_lateness_ns = 0
        link.on_frame = self._on_frame

    @property
    def logical_ps(self) -> int:
        """This node's simulation clock (0 when not in logical mode)."""
        return self.clock.ps if self.clock is not None else 0

    @property
    def sim_elapsed_ps(self) -> int:
        """Simulation time this phase consumed (logical mode; 0 otherwise)."""
        return self.clock.elapsed_ps if self.clock is not None else 0

    def _on_frame(self, payload: bytes) -> None:
        self._q.put(json.loads(payload.decode("utf-8")))

    def _paced(self, frame: dict) -> dict:
        ts = frame.get("_ts")
        if self.logical:
            if ts is None or self.delay_ps <= 0:
                return frame
            # the arrival time IS the local clock: advance to it, never late
            self.clock.advance_to(int(ts) + self.delay_ps)
            self.on_time_events += 1
            return frame
        if ts is None or self.delay_ns <= 0:
            return frame
        # sender clock -> local clock, plus the modeled propagation delay
        deadline = int(ts) - self.peer_offset_ns + self.delay_ns
        wait_ns = deadline - time_ns()
        if wait_ns > 0:
            sleep(wait_ns / 1e9)
            self.on_time_events += 1
        else:
            self.late_events += 1
            self.max_lateness_ns = max(self.max_lateness_ns, -wait_ns)
        return frame

    def send(self, kind: str, body: dict) -> None:
        frame: dict = {_TAG: kind, "body": body}
        if self.logical:
            if self.delay_ps > 0:
                frame["_ts"] = self.clock.ps        # simulation clock, not wall clock
        elif self.delay_ns > 0:
            frame["_ts"] = time_ns()
        self.link.send(json.dumps(frame, separators=(",", ":")).encode("utf-8"))

    def recv(self, expected: str, timeout: float = 120.0) -> dict:
        frame = self._paced(self._q.get(timeout=timeout))
        if frame.get(_TAG) != expected:
            raise ValueError(f"expected {expected!r}, got {frame.get(_TAG)!r}")
        return frame["body"]

    def recv_any(self, timeout: float = 120.0) -> tuple[str, dict]:
        """Receive the next frame, returning (kind, body) — for a serve loop that
        handles more than one message type (e.g. Cascade parity requests until done)."""
        frame = self._paced(self._q.get(timeout=timeout))
        return frame.get(_TAG), frame["body"]

    def call(self, kind: str, body: dict, expected: str, timeout: float = 120.0) -> dict:
        self.send(kind, body)
        return self.recv(expected, timeout=timeout)


class RemoteQuantumManager:
    """Measure-only proxy: forwards batched measurements to the remote service."""

    def __init__(self, rpc: RpcChannel):
        self.rpc = rpc

    def measure_batch(self, requests: list[tuple[int, int]]) -> list[int]:
        """requests: list of (qubit_id, angle_code). Returns outcomes in order."""
        resp = self.rpc.call("MEASURE_REQ", {"reqs": [[int(q), int(c)] for q, c in requests]},
                             expected="MEASURE_RESP")
        return resp["outcomes"]
