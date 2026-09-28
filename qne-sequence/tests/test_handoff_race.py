"""BB84 -> Cascade hand-over of the classical link (review S13, hit on the slice 2026-09-28).

Bob finishes the protocol first and sends PARITY_REQ while Alice's link still routes
frames to the protocol Listener. That frame must be kept for the RpcChannel, not
crash the Listener (and with it the link's receive thread).
"""

from __future__ import annotations

import json
import types

from qne_sequence.listener import Link, Listener
from qne_sequence.remote_qm import RpcChannel
from qne_sequence.wire_codec import WireCodec


def _listener():
    tl = types.SimpleNamespace(now=lambda: 0, inject=lambda ev: None)
    return Listener(tl, node=types.SimpleNamespace(), protocol=types.SimpleNamespace(), delay=0)


def test_listener_keeps_early_rpc_frames_and_rpc_channel_adopts_them():
    lst = _listener()
    rpc_frame = json.dumps({"__e91_rpc__": "PARITY_REQ", "body": {"blocks": [[0, 1]]}}).encode()
    lst.on_frame(rpc_frame)                                   # no KeyError
    assert lst.pending_rpc == [rpc_frame]

    # a protocol frame still goes to the timeline as before
    injected = []
    lst.timeline = types.SimpleNamespace(now=lambda: 0, inject=injected.append)
    lst.on_frame(WireCodec.encode("classical", "alice", "bob.BB84", "QUBITS_DONE", {}))
    assert len(injected) == 1 and lst.pending_rpc == [rpc_frame]

    link = types.SimpleNamespace(on_frame=lst.on_frame)
    rpc = RpcChannel(link, handoff=lst)
    assert link.on_frame == rpc._on_frame                      # link handed over
    assert lst.pending_rpc == []                               # adopted, not duplicated
    kind, body = rpc.recv_any(timeout=1)
    assert kind == "PARITY_REQ" and body == {"blocks": [[0, 1]]}


def test_frame_dispatched_to_the_listener_during_handoff_is_forwarded_in_order():
    """The RX thread reads link.on_frame before calling it: a frame it already
    dispatched to the Listener while the RpcChannel was taking over must still
    reach the RpcChannel, after the stashed ones and before anything later."""
    lst = _listener()
    f = lambda k: json.dumps({"__e91_rpc__": k, "body": {}}).encode()  # noqa: E731
    lst.on_frame(f("FIRST"))                                   # stashed before hand-over
    link = types.SimpleNamespace(on_frame=lst.on_frame)
    rpc = RpcChannel(link, handoff=lst)
    lst.on_frame(f("SECOND"))            # dispatched to the OLD handler by a racing RX thread
    link.on_frame(f("THIRD"))            # dispatched to the new handler
    assert [rpc.recv_any(timeout=1)[0] for _ in range(3)] == ["FIRST", "SECOND", "THIRD"]
    # a protocol straggler after the protocol ended is counted, not injected or crashing
    lst.on_frame(WireCodec.encode("classical", "alice", "bob.BB84", "QUBITS_DONE", {}))
    assert lst.dropped_after_handoff == 1


def test_handoff_is_atomic_against_a_concurrent_rx_thread():
    """Frames arriving from another thread throughout the hand-over are all
    delivered exactly once and in order (no lost frame in the drain/re-point gap)."""
    import threading
    lst = _listener()
    link = types.SimpleNamespace(on_frame=lst.on_frame)
    n = 2000
    def rx():
        for i in range(n):
            link.on_frame(json.dumps({"__e91_rpc__": str(i), "body": {}}).encode())
    th = threading.Thread(target=rx)
    th.start()
    rpc = RpcChannel(link, handoff=lst)       # races the sender
    th.join()
    got = [rpc.recv_any(timeout=1)[0] for _ in range(n)]
    assert got == [str(i) for i in range(n)]
    assert rpc._q.empty() and lst.pending_rpc == []


def test_link_rx_loop_survives_a_handler_exception(monkeypatch):
    """A bug in the frame handler used to end the receive thread silently, so both
    ends waited out their timeouts. The loop must log and keep receiving."""
    link = Link()
    payloads = iter([b"first", b"second", None])
    monkeypatch.setattr(link, "recv_one", lambda count=False: next(payloads))
    link._running = True
    seen = []

    def handler(p):
        seen.append(p)
        if p == b"first":
            raise KeyError("kind")
    link.on_frame = handler
    link._rx_loop()
    assert seen == [b"first", b"second"]
    assert link.rx_count == 2
