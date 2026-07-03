"""Raw-socket 0x7101 photon transport — the FABRIC data plane (DESIGN.md §5.2, Phase C2).

On FABRIC the quantum channel is real `0x7101` Ethernet frames on a photon interface,
forwarded through the BMv2 P4 switch that applies fiber loss as a probabilistic drop.
This module sends/receives those frames, **reusing qfabric's `qne.photon.PhotonPacket`
wire format** so the existing P4 program and control-plane table apply unchanged.

It mirrors `RemoteQuantumChannel`'s interface (`transmit_batch` / `transmit_one`) so the
`PhotonEmissionStrategy` and `DistributedBB84` use it without modification — selecting
`--quantum-transport raw` swaps the channel and adds a raw RX thread; nothing else
changes. Loss is NOT applied here (the P4 switch owns the loss model).

`AF_PACKET` is Linux-only; this module imports cleanly everywhere but raises a clear
error if a raw socket is actually opened on a platform without it (e.g. macOS dev box).
Run the live path on a FABRIC slice / Linux with veth + BMv2.
"""

from __future__ import annotations

import socket
import threading
from time import perf_counter, sleep, time_ns

import numpy

from sequence.kernel.event import Event
from sequence.kernel.process import Process

from qne.photon import PhotonPacket, ETHERTYPE_PHOTON

_HAS_AF_PACKET = hasattr(socket, "AF_PACKET")


def parse_mac(mac: str | bytes) -> bytes:
    """Accept "02:00:00:00:00:01" or raw 6 bytes; return 6 bytes."""
    if isinstance(mac, bytes):
        if len(mac) != 6:
            raise ValueError(f"MAC must be 6 bytes, got {len(mac)}")
        return mac
    return bytes(int(b, 16) for b in mac.split(":"))


def _require_af_packet() -> None:
    if not _HAS_AF_PACKET:
        raise RuntimeError(
            "raw 0x7101 photon transport requires AF_PACKET (Linux). This host lacks "
            "it — run the raw/P4 path on a FABRIC slice or Linux with veth + BMv2. "
            "Use --quantum-transport tcp for local/dev runs."
        )


def _open_raw_socket(interface: str) -> "socket.socket":
    _require_af_packet()
    sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(ETHERTYPE_PHOTON))
    sock.bind((interface, 0))
    return sock


class RawQuantumChannel:
    """Alice-side photon TX over a raw 0x7101 socket.

    Loss is configurable so the channel works **with or without** a P4 switch:
      * `loss_probability == 0` (default) — send every photon; an external BMv2 P4
        switch applies the fiber-loss drop (the FABRIC path).
      * `loss_probability > 0` — drop photons in **software** here and send the
        survivors as 0x7101 frames node-to-node over a direct L2 link (no switch).
    """

    def __init__(self, interface: str, src_mac="02:00:00:00:00:01",
                 dst_mac="02:00:00:00:00:02", wavelength: int = 0, delay: int = 0,
                 loss_probability: float = 0.0, seed: int = 0,
                 rate_hz: float = 0.0):
        self.interface = interface
        self.src_mac = parse_mac(src_mac)
        self.dst_mac = parse_mac(dst_mac)
        self.wavelength = wavelength
        self.delay = delay
        self.loss_probability = loss_probability
        # Frames-per-second cap for transmit_batch (0 = unpaced). An unpaced
        # 20k-frame burst overruns BMv2 and Bob's socket buffer — the drops
        # look like extra fiber loss. qne's scenarios use 10 kHz for BMv2.
        self.rate_hz = rate_hz
        self._rng = numpy.random.default_rng(seed)
        self._sock = None
        self.tx_count = 0

    def _dropped(self) -> bool:
        return self.loss_probability > 0.0 and self._rng.random() < self.loss_probability

    def _socket(self):
        if self._sock is None:
            self._sock = _open_raw_socket(self.interface)
        return self._sock

    def _send_photon(self, seq: int, basis: int, bit: int) -> None:
        ts = time_ns()  # ps fits in 64 bits split hi/lo
        pkt = PhotonPacket(basis=int(basis), state=int(bit), sequence_num=int(seq),
                           wavelength=self.wavelength,
                           timestamp_hi=(ts >> 32) & 0xFFFFFFFF, timestamp_lo=ts & 0xFFFFFFFF)
        self._socket().send(pkt.to_ethernet_frame(dst_mac=self.dst_mac, src_mac=self.src_mac))
        self.tx_count += 1

    # interface parity with RemoteQuantumChannel (used by PhotonEmissionStrategy)
    def transmit_batch(self, src_name: str, receiver_proto: str, pulses: list) -> None:
        # Absolute-deadline pacing (frame i leaves at t0 + i/rate) so sleep
        # overhead doesn't accumulate into a lower effective rate.
        interval = (1.0 / self.rate_hz) if self.rate_hz > 0 else 0.0
        t0 = perf_counter()
        emitted = 0
        for seq, basis, bit in pulses:
            if self._dropped():
                continue
            if interval:
                target = t0 + emitted * interval
                remaining = target - perf_counter()
                if remaining > 0:
                    sleep(remaining)
            self._send_photon(seq, basis, bit)
            emitted += 1

    def transmit_one(self, src_name: str, receiver_proto: str,
                     seq: int, basis: int, bit: int) -> None:
        if not self._dropped():
            self._send_photon(seq, basis, bit)

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass


class RawPhotonReceiver:
    """Bob-side photon RX: a thread that parses 0x7101 frames and injects each as a
    one-pulse ``receive_qubits`` event into the timeline (mirrors listener.Listener
    for the TCP path, preserving wire order via a monotonic priority)."""

    def __init__(self, interface: str, timeline, protocol, peer_name: str,
                 delay: int = 0):
        self.interface = interface
        self.timeline = timeline
        self.protocol = protocol
        self.peer_name = peer_name
        self.delay = delay
        self._sock = None
        self._thread = None
        self._running = False
        self._seq = 0
        self.rx_count = 0

    def start(self) -> None:
        self._sock = _open_raw_socket(self.interface)
        self._sock.settimeout(1.0)
        self._running = True
        self._thread = threading.Thread(target=self._rx_loop, daemon=True)
        self._thread.start()

    def _rx_loop(self) -> None:
        while self._running:
            try:
                frame, _addr = self._sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                pkt = PhotonPacket.from_ethernet_frame(frame)
            except ValueError:
                continue  # not a photon frame
            self.rx_count += 1
            self._seq += 1
            pulse = [[pkt.sequence_num, pkt.basis, pkt.state]]
            proc = Process(self.protocol, "receive_qubits", [self.peer_name, pulse])
            self.timeline.inject(Event(self.timeline.now() + self.delay, proc,
                                       priority=self._seq))

    def stop(self) -> None:
        self._running = False
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
