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

_TAG = "__e91_rpc__"


class RpcChannel:
    """Synchronous framed RPC over a Link: send a typed frame, block for a reply.

    Install ``on_frame`` as the Link's callback. ``call``/``recv`` block for the
    next frame of the expected type; ``send`` is fire-and-forget. Frames are the
    same length-prefixed JSON the rest of the wire uses.
    """

    def __init__(self, link):
        self.link = link
        self._q: "queue.Queue[dict]" = queue.Queue()
        link.on_frame = self._on_frame

    def _on_frame(self, payload: bytes) -> None:
        self._q.put(json.loads(payload.decode("utf-8")))

    def send(self, kind: str, body: dict) -> None:
        self.link.send(json.dumps({_TAG: kind, "body": body},
                                  separators=(",", ":")).encode("utf-8"))

    def recv(self, expected: str, timeout: float = 120.0) -> dict:
        frame = self._q.get(timeout=timeout)
        if frame.get(_TAG) != expected:
            raise ValueError(f"expected {expected!r}, got {frame.get(_TAG)!r}")
        return frame["body"]

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
