"""Transport + listener — the receive-side seam (DESIGN.md §5).

`Link` is one persistent TCP connection to the peer carrying length-prefixed frames
([4-byte big-endian length][JSON payload], the same framing as qfabric/qne/channel.py).
A background thread reads frames and hands each to a callback.

`Listener` is that callback: it decodes the envelope and injects an event into the
RealTimeTimeline so the frame is delivered exactly as SeQUeNCe expects —
``node.receive_message(src, msg)`` for classical traffic, and (Phase A) the
protocol's ``receive_qubits(src, pulses)`` for the stubbed quantum batch.
"""

from __future__ import annotations

import socket
import struct
import threading
from time import sleep

from sequence.kernel.event import Event
from sequence.kernel.process import Process

from .wire_codec import WireCodec

_LEN = struct.Struct("!I")


class Link:
    """A single bidirectional TCP connection with framed send/recv.

    One side calls ``serve`` (listen + accept one peer), the other ``connect``
    (with retry). After the connection is up, ``send`` is full-duplex and the RX
    thread invokes ``on_frame(bytes)`` for each inbound frame.
    """

    def __init__(self, on_frame=None):
        self.on_frame = on_frame
        self._sock: socket.socket | None = None
        self._listen_sock: socket.socket | None = None
        self._rx_thread: threading.Thread | None = None
        self._running = False
        self._send_lock = threading.Lock()
        self.tx_count = 0
        self.rx_count = 0

    def serve(self, host: str, port: int, timeout: float = 30.0) -> None:
        ls = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        ls.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        ls.bind((host, port))
        ls.listen(1)
        ls.settimeout(timeout)
        self._listen_sock = ls
        conn, _addr = ls.accept()
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._sock = conn

    def connect(self, host: str, port: int, retries: int = 120, delay: float = 0.25) -> None:
        last = None
        for _ in range(retries):
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.connect((host, port))
                s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                self._sock = s
                return
            except OSError as exc:  # peer not listening yet
                last = exc
                sleep(delay)
        raise ConnectionError(f"could not connect to {host}:{port}: {last}")

    def start_rx(self) -> None:
        self._running = True
        self._rx_thread = threading.Thread(target=self._rx_loop, daemon=True)
        self._rx_thread.start()

    def _recv_exact(self, n: int) -> bytes | None:
        buf = b""
        while len(buf) < n:
            try:
                chunk = self._sock.recv(n - len(buf))
            except OSError:
                return None
            if not chunk:
                return None
            buf += chunk
        return buf

    def _rx_loop(self) -> None:
        while self._running:
            header = self._recv_exact(_LEN.size)
            if header is None:
                break
            (length,) = _LEN.unpack(header)
            payload = self._recv_exact(length)
            if payload is None:
                break
            self.rx_count += 1
            if self.on_frame is not None:
                self.on_frame(payload)

    def send(self, payload: bytes) -> None:
        with self._send_lock:
            self._sock.sendall(_LEN.pack(len(payload)) + payload)
            self.tx_count += 1

    def close(self) -> None:
        self._running = False
        for s in (self._sock, self._listen_sock):
            try:
                if s is not None:
                    s.close()
            except OSError:
                pass


class Listener:
    """Decodes inbound frames and injects them into the local timeline.

    Args:
        timeline: the RealTimeTimeline to inject events into (thread-safe).
        node: the local SeQUeNCe node (classical frames -> node.receive_message).
        protocol: the local DistributedBB84 (quantum frames -> protocol.receive_qubits).
        delay: modeled extra delay (ps) added on top of the real wire latency.
    """

    def __init__(self, timeline, node, protocol, delay: int = 0):
        self.timeline = timeline
        self.node = node
        self.protocol = protocol
        self.delay = delay
        self._seq = 0  # monotonic priority -> preserve wire (FIFO) order at equal times

    def on_frame(self, data: bytes) -> None:
        frame = WireCodec.decode(data)
        fire = self.timeline.now() + self.delay
        if frame["kind"] == "classical":
            msg = WireCodec.to_message(frame)
            proc = Process(self.node, "receive_message", [frame["src"], msg])
        else:  # "quantum" — descriptor batch / single photon
            proc = Process(self.protocol, "receive_qubits",
                           [frame["src"], frame["payload"]["pulses"]])
        # Events sort by (time, priority); an increasing priority keeps inbound
        # frames in arrival order even when they share a fire time (e.g. a burst of
        # PerPhotonEvent QUBITS frames followed by QUBITS_DONE).
        self._seq += 1
        self.timeline.inject(Event(fire, proc, priority=self._seq))
