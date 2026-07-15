"""Clock alignment for lookahead delivery — no PTP.

Two flavors, both a single Cristian-style round trip on a freshly connected
Link BEFORE ``start_rx`` (frames are read synchronously via ``Link.recv_one``;
with ``auth_key`` set they are HMAC-sealed like all other traffic):

* ``serve_epoch``/``request_epoch`` — the two-node BB84 path. The serving side
  (Bob) is the time master: it distributes the shared RealTimeTimeline epoch,
  and the client translates it into its local clock. Both ends also learn the
  per-link peer clock offset (for paced RPC delivery after the timeline stops).
* ``sync_link`` — the RPC runners (E91, repeater chains). No timeline epoch,
  just the per-link offset so ``RpcChannel`` can deliver each frame at exactly
  ``t_send + delay`` in the receiver's clock.

Residual alignment error is ~RTT/2 — tens of microseconds on a co-located
slice — far below any modeled channel delay the lookahead scheduler cares
about (a 10 km fiber is ~49 us; 100 km is ~490 us). The per-run lateness
metrics expose whatever error remains, so fidelity is *verified*, not assumed.

PTP was considered and rejected: nothing in the protocols compares wall clocks
across nodes, so full clock synchronization solves a problem this design does
not have; a one-shot offset estimate per link is sufficient.
"""

from __future__ import annotations

import json
from time import time_ns


def _send(link, obj: dict) -> None:
    link.send(json.dumps(obj, separators=(",", ":")).encode("utf-8"))


def _recv(link, kind: str) -> dict:
    raw = link.recv_one()
    if raw is None:
        raise ConnectionError(f"link closed during clock sync (awaiting {kind})")
    msg = json.loads(raw.decode("utf-8"))
    if msg.get("kind") != kind:
        raise ValueError(f"expected {kind!r}, got {msg.get('kind')!r}")
    return msg


def serve_epoch(link, epoch_ns: int) -> tuple[int, int]:
    """Time-master side of the epoch handshake (one request, one ack).

    ``epoch_ns`` is the master's chosen wall-clock origin. Returns
    ``(peer_offset_ns, rtt_ns)`` where ``peer_offset_ns`` estimates
    (peer clock - local clock), as measured by the peer and reported back.
    """
    _recv(link, "epoch_req")
    _send(link, {"kind": "epoch_resp", "epoch_ns": epoch_ns,
                 "server_ns": time_ns()})
    ack = _recv(link, "epoch_ack")
    return int(ack["peer_offset_ns"]), int(ack["rtt_ns"])


def request_epoch(link) -> tuple[int, int, int]:
    """Client side: fetch the master's epoch and translate it to local clock.

    Returns ``(local_epoch_ns, offset_ns, rtt_ns)`` where ``offset_ns`` is the
    Cristian estimate of (master clock - local clock) and ``local_epoch_ns``
    is the master's epoch expressed in the local clock, i.e. both timelines'
    ``now()`` agree to within ~RTT/2.
    """
    t0 = time_ns()
    _send(link, {"kind": "epoch_req"})
    resp = _recv(link, "epoch_resp")
    t1 = time_ns()
    rtt = t1 - t0
    # Cristian: the master's clock read server_ns roughly RTT/2 before t1.
    offset = resp["server_ns"] + rtt // 2 - t1
    # Report back so the master also knows the offset to THIS peer.
    _send(link, {"kind": "epoch_ack", "peer_offset_ns": -offset, "rtt_ns": rtt})
    return resp["epoch_ns"] - offset, offset, rtt


def sync_link(link, serving: bool) -> tuple[int, int]:
    """Per-link clock alignment (no epoch). Returns ``(peer_offset_ns, rtt_ns)``.

    ``peer_offset_ns`` estimates (peer clock - local clock) as seen from THIS
    side. The connecting side (``serving=False``) runs the measurement and
    reports the mirrored offset back so both ends end up knowing it.
    """
    if serving:
        _recv(link, "sync_req")
        _send(link, {"kind": "sync_resp", "server_ns": time_ns()})
        ack = _recv(link, "sync_ack")
        return int(ack["peer_offset_ns"]), int(ack["rtt_ns"])
    t0 = time_ns()
    _send(link, {"kind": "sync_req"})
    resp = _recv(link, "sync_resp")
    t1 = time_ns()
    rtt = t1 - t0
    offset = resp["server_ns"] + rtt // 2 - t1        # peer - local
    _send(link, {"kind": "sync_ack", "peer_offset_ns": -offset, "rtt_ns": rtt})
    return offset, rtt
