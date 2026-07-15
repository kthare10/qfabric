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


class RpcChannel:
    """Synchronous framed RPC over a Link: send a typed frame, block for a reply.

    Install ``on_frame`` as the Link's callback. ``call``/``recv`` block for the
    next frame of the expected type; ``send`` is fire-and-forget. Frames are the
    same length-prefixed JSON the rest of the wire uses.

    Lookahead pacing (the emulation-fidelity contract, mirroring
    listener.Listener): with ``delay_ps > 0``, outbound frames carry the
    sender's clock and inbound frames are DELIVERED no earlier than
    ``t_send + delay`` in the local clock (``peer_offset_ns`` from
    timesync.sync_link translates between the two clocks). As long as real
    wire latency stays under the modeled delay, every message is handed to
    the protocol at exactly the time a simulator would deliver it; misses are
    counted (``late_events`` / ``max_lateness_ns``) — the per-run certificate.
    """

    def __init__(self, link, delay_ps: int = 0, peer_offset_ns: int = 0):
        self.link = link
        self.delay_ns = int(delay_ps) // 1000
        self.peer_offset_ns = int(peer_offset_ns)
        self._q: "queue.Queue[dict]" = queue.Queue()
        self.on_time_events = 0
        self.late_events = 0
        self.max_lateness_ns = 0
        link.on_frame = self._on_frame

    def _on_frame(self, payload: bytes) -> None:
        self._q.put(json.loads(payload.decode("utf-8")))

    def _paced(self, frame: dict) -> dict:
        ts = frame.get("_ts")
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
        if self.delay_ns > 0:
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
