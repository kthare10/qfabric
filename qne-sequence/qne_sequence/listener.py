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

from qne.auth import AuthError, FrameAuthenticator

from .wire_codec import WireCodec

_LEN = struct.Struct("!I")


class Link:
    """A single bidirectional TCP connection with framed send/recv.

    One side calls ``serve`` (listen + accept one peer), the other ``connect``
    (with retry). After the connection is up, ``send`` is full-duplex and the RX
    thread invokes ``on_frame(bytes)`` for each inbound frame.

    With ``auth_key`` set, every frame carries an HMAC tag + sequence number
    (qne/auth.py). A frame that fails verification tears the connection down
    (like TLS): the RX loop stops, ``auth_failures`` is incremented, and no
    payload is delivered — the run then aborts on its timeouts rather than
    proceeding with attacker-controlled sifting traffic.
    """

    def __init__(self, on_frame=None, auth_key: bytes | str | None = None):
        self.on_frame = on_frame
        self._auth = FrameAuthenticator(auth_key) if auth_key else None
        self._sock: socket.socket | None = None
        self._listen_sock: socket.socket | None = None
        self._rx_thread: threading.Thread | None = None
        self._running = False
        self._send_lock = threading.Lock()
        self.tx_count = 0
        self.rx_count = 0
        self.auth_failures = 0

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

    def recv_one(self) -> bytes | None:
        """Read exactly one frame synchronously (header + payload + auth open).

        Returns None on EOF / closed socket; raises AuthError on a bad tag.
        Safe only before ``start_rx`` (the timesync handshake) or from within
        the RX thread itself — two concurrent readers would interleave frames.
        """
        header = self._recv_exact(_LEN.size)
        if header is None:
            return None
        (length,) = _LEN.unpack(header)
        payload = self._recv_exact(length)
        if payload is None:
            return None
        if self._auth is not None:
            payload = self._auth.open(payload)
        self.rx_count += 1
        return payload

    def _rx_loop(self) -> None:
        while self._running:
            try:
                payload = self.recv_one()
            except AuthError as exc:
                self.auth_failures += 1
                print(f"Link: authentication failure, closing: {exc}",
                      flush=True)
                self.close()
                break
            if payload is None:
                break
            if self.on_frame is not None:
                self.on_frame(payload)

    def send(self, payload: bytes) -> None:
        with self._send_lock:
            if self._auth is not None:
                payload = self._auth.seal(payload)
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

    Delivery time — the emulation-fidelity contract:

    * Frame carries ``t_send`` AND ``delay > 0`` (lookahead mode): deliver at
      exactly ``t_send + delay`` in shared sim time — the event time a pure
      simulator would use. The modeled channel delay is the *lookahead* of a
      conservative distributed DES: as long as the real wire latency stays
      below ``delay * time_scale``, the frame arrives before its deadline and
      the emulation executes the simulator's exact event schedule. A frame that
      arrives past its deadline fires immediately and is counted
      (``late_events`` / ``max_lateness_ps``) — the per-run fidelity report.
      Requires the timesync epoch handshake so both clocks share an origin.
    * Otherwise (no ``t_send``, or ``delay == 0``): legacy behavior — deliver
      at local arrival time plus ``delay`` (real latency + modeled extra).

    Args:
        timeline: the RealTimeTimeline to inject events into (thread-safe).
        node: the local SeQUeNCe node (classical frames -> node.receive_message).
        protocol: the local DistributedBB84 (quantum frames -> protocol.receive_qubits).
        delay: modeled one-way channel delay (ps).
    """

    def __init__(self, timeline, node, protocol, delay: int = 0):
        self.timeline = timeline
        self.node = node
        self.protocol = protocol
        self.delay = delay
        self._seq = 0  # monotonic priority -> preserve wire (FIFO) order at equal times
        self.on_time_events = 0
        self.late_events = 0
        self.max_lateness_ps = 0

    def on_frame(self, data: bytes) -> None:
        frame = WireCodec.decode(data)
        now = self.timeline.now()
        t_send = frame.get("t_send")
        if t_send is not None and self.delay > 0:
            fire = t_send + self.delay
            lateness = now - fire
            if lateness > 0:        # missed the sim deadline: fire ASAP, record
                self.late_events += 1
                self.max_lateness_ps = max(self.max_lateness_ps, lateness)
                fire = now
            else:
                self.on_time_events += 1
        else:
            fire = now + self.delay
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
