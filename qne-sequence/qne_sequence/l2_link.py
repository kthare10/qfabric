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

"""Reliable message transport over raw EtherType 0x7102 datagrams.

The protocol header is ``!2sBBQHH``: a two-byte magic, version, packet type,
64-bit message sequence, 16-bit fragment index, and 16-bit fragment count.
DATA packets append one payload fragment; ACK packets echo sequence/index;
HELLO and HELLO_ACK use zeroed sequence/fragment fields. Each DATA fragment is
acknowledged independently and retransmitted on timeout. Complete messages are
buffered by sequence and delivered strictly in order, with duplicate fragments
and already-delivered messages acknowledged but never delivered twice.
"""

from __future__ import annotations

import queue
import socket
import struct
import threading
from collections.abc import Callable
from time import monotonic
from typing import Protocol

from qne.auth import AuthError, FrameAuthenticator

ETHERTYPE_CLASSICAL = 0x7102
_DEFAULT_FRAGMENT_SIZE = 1400
_DEFAULT_ACK_TIMEOUT = 0.02
_DEFAULT_MAX_RETRIES = 50
_HEADER = struct.Struct("!2sBBQHH")
_MAGIC = b"QL"
_VERSION = 1
_DATA = 1
_ACK = 2
_HELLO = 3
_HELLO_ACK = 4
_ETHERNET_HEADER = struct.Struct("!6s6sH")
_HAS_AF_PACKET = hasattr(socket, "AF_PACKET")
_STOP = object()


class DatagramEndpoint(Protocol):
    """Minimal datagram backend used by :class:`ReliableLink`."""

    def open(self, *, server: bool, host: str, port: int) -> None: ...

    def send(self, data: bytes) -> None: ...

    def recv(self, timeout: float | None = None) -> bytes | None: ...

    def close(self) -> None: ...


def _require_af_packet() -> None:
    if not _HAS_AF_PACKET:
        raise RuntimeError(
            "raw 0x7102 classical transport requires AF_PACKET (Linux). This host "
            "lacks it — run the raw/P4 path on a FABRIC slice or Linux with veth + "
            "BMv2, or use LoopbackDatagram for tests."
        )


def _parse_mac(mac: str | bytes) -> bytes:
    if isinstance(mac, bytes):
        if len(mac) != 6:
            raise ValueError(f"MAC must be 6 bytes, got {len(mac)}")
        return mac
    parts = mac.split(":")
    if len(parts) != 6:
        raise ValueError(f"invalid MAC address: {mac}")
    return bytes(int(part, 16) for part in parts)


class RawL2Datagram:
    """Datagram endpoint backed by a Linux AF_PACKET raw Ethernet socket."""

    def __init__(
        self,
        interface: str,
        src_mac: str | bytes,
        dst_mac: str | bytes,
    ) -> None:
        self.interface = interface
        self.src_mac = _parse_mac(src_mac)
        self.dst_mac = _parse_mac(dst_mac)
        self._sock: socket.socket | None = None

    def open(self, *, server: bool, host: str, port: int) -> None:
        del server, host, port
        _require_af_packet()
        sock = socket.socket(
            socket.AF_PACKET,
            socket.SOCK_RAW,
            socket.htons(ETHERTYPE_CLASSICAL),
        )
        sock.bind((self.interface, 0))
        self._sock = sock

    def send(self, data: bytes) -> None:
        if self._sock is None:
            raise ConnectionError("raw L2 endpoint is not open")
        frame = _ETHERNET_HEADER.pack(self.dst_mac, self.src_mac, ETHERTYPE_CLASSICAL) + data
        self._sock.send(frame)

    def recv(self, timeout: float | None = None) -> bytes | None:
        if self._sock is None:
            return None
        self._sock.settimeout(timeout)
        try:
            frame = self._sock.recv(65535)
        except socket.timeout:
            return b""
        except OSError:
            return None
        if len(frame) < _ETHERNET_HEADER.size:
            return b""
        # Filter on EtherType only, NOT on peer MAC: the BMv2 P4 switch rewrites
        # src/dst MACs on forward (see quantum_channel.p4's classical_channel_params
        # / port_forward actions), so the frame Bob receives no longer carries the
        # peer's original MAC. This mirrors RawPhotonReceiver, which accepts any
        # 0x7101 frame. The socket is already bound to ETHERTYPE_CLASSICAL, so this
        # is belt-and-suspenders; the ReliableLink magic/version check rejects any
        # stray non-QFabric 0x7102 traffic. NOTE: this assumes ONE 0x7102 link per
        # interface (point-to-point emulated link) — multiple classical links
        # sharing an interface would cross-talk and need distinct interfaces (or a
        # connection-id header), consistent with "single link per run".
        _dst, _src, ethertype = _ETHERNET_HEADER.unpack_from(frame)
        if ethertype != ETHERTYPE_CLASSICAL:
            return b""
        return frame[_ETHERNET_HEADER.size :]

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None


class LoopbackDatagram:
    """Cross-platform paired endpoint with an optional packet transform hook."""

    def __init__(self) -> None:
        self._inbox: queue.Queue[bytes | object] = queue.Queue()
        self._peer: LoopbackDatagram | None = None
        self._open = False
        self._closed = False
        self.transform: Callable[[bytes], list[bytes]] = lambda data: [data]

    @classmethod
    def pair(cls) -> tuple[LoopbackDatagram, LoopbackDatagram]:
        left = cls()
        right = cls()
        left._peer = right
        right._peer = left
        return left, right

    def open(self, *, server: bool, host: str, port: int) -> None:
        del server, host, port
        if self._closed:
            raise ConnectionError("loopback endpoint is closed")
        self._open = True

    def send(self, data: bytes) -> None:
        if not self._open or self._closed:
            raise ConnectionError("loopback endpoint is not open")
        if self._peer is None or not self._peer._open or self._peer._closed:
            return
        for packet in self.transform(bytes(data)):
            self._peer._inbox.put(packet)

    def recv(self, timeout: float | None = None) -> bytes | None:
        if self._closed:
            return None
        try:
            packet = self._inbox.get(timeout=timeout)
        except queue.Empty:
            return b""
        if packet is _STOP:
            return None
        return bytes(packet)

    def inject(self, data: bytes) -> None:
        """Insert a datagram directly into this endpoint's receive queue."""
        self._inbox.put(bytes(data))

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._inbox.put(_STOP)


class ReliableLink:
    """A bidirectional, reliable, in-order message link over datagrams."""

    def __init__(
        self,
        on_frame=None,
        auth_key: bytes | str | None = None,
        *,
        backend: DatagramEndpoint | None = None,
        interface: str | None = None,
        src_mac: str | bytes = "02:00:00:00:00:01",
        dst_mac: str | bytes = "02:00:00:00:00:02",
        fragment_size: int = _DEFAULT_FRAGMENT_SIZE,
        ack_timeout: float = _DEFAULT_ACK_TIMEOUT,
        max_retries: int = _DEFAULT_MAX_RETRIES,
    ) -> None:
        if backend is None:
            if interface is None:
                raise ValueError("interface is required when no datagram backend is supplied")
            backend = RawL2Datagram(interface, src_mac, dst_mac)
        if fragment_size <= 0 or fragment_size > 65535 - _HEADER.size:
            raise ValueError("fragment_size is outside the valid datagram range")
        if ack_timeout <= 0 or max_retries <= 0:
            raise ValueError("ack_timeout and max_retries must be positive")
        self.on_frame = on_frame
        self.tx_count = 0
        self.rx_count = 0
        self.auth_failures = 0
        self._auth = FrameAuthenticator(auth_key) if auth_key else None
        self._backend = backend
        self._fragment_size = fragment_size
        self._ack_timeout = ack_timeout
        self._max_retries = max_retries
        self._send_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._delivery_cv = threading.Condition(self._state_lock)
        self._handshake_cv = threading.Condition(self._state_lock)
        self._ack_cv = threading.Condition(self._state_lock)
        self._io_thread: threading.Thread | None = None
        self._rx_thread: threading.Thread | None = None
        self._running = False
        self._connected = False
        self._server = False
        self._next_tx_seq = 0
        self._next_rx_seq = 0
        self._assemblies: dict[int, tuple[int, dict[int, bytes]]] = {}
        self._completed: dict[int, bytes] = {}
        self._deliveries: queue.Queue[bytes | object] = queue.Queue()
        self._acked: set[tuple[int, int]] = set()
        self._error: BaseException | None = None

    def serve(self, host: str, port: int, timeout: float = 30.0) -> None:
        self._server = True
        self._open(host, port)
        deadline = monotonic() + timeout
        with self._handshake_cv:
            while not self._connected and self._running:
                remaining = deadline - monotonic()
                if remaining <= 0:
                    self.close()
                    raise TimeoutError(f"timed out waiting for peer on {host}:{port}")
                self._handshake_cv.wait(remaining)
        if not self._connected:
            raise ConnectionError("link closed before peer established")

    def connect(self, host: str, port: int, retries: int = 120, delay: float = 0.25) -> None:
        self._server = False
        self._open(host, port)
        for _ in range(retries):
            self._backend.send(self._packet(_HELLO))
            deadline = monotonic() + delay
            with self._handshake_cv:
                while not self._connected and self._running:
                    remaining = deadline - monotonic()
                    if remaining <= 0:
                        break
                    self._handshake_cv.wait(remaining)
            if self._connected:
                return
        self.close()
        raise ConnectionError(f"could not connect to {host}:{port}")

    def _open(self, host: str, port: int) -> None:
        if self._running:
            return
        self._backend.open(server=self._server, host=host, port=port)
        self._running = True
        self._io_thread = threading.Thread(target=self._io_loop, daemon=True)
        self._io_thread.start()

    def start_rx(self) -> None:
        if self._rx_thread is not None and self._rx_thread.is_alive():
            return
        self._rx_thread = threading.Thread(target=self._rx_loop, daemon=True)
        self._rx_thread.start()

    def recv_one(self) -> bytes | None:
        """Return one delivered message, or ``None`` when the link closes."""
        item = self._deliveries.get()
        if item is _STOP:
            if isinstance(self._error, AuthError):
                raise self._error
            return None
        payload = bytes(item)
        if self._auth is not None:
            try:
                payload = self._auth.open(payload)
            except AuthError as exc:
                self.auth_failures += 1
                self._error = exc
                self.close()
                raise
        self.rx_count += 1
        return payload

    def _rx_loop(self) -> None:
        while self._running:
            try:
                payload = self.recv_one()
            except AuthError as exc:
                print(f"ReliableLink: authentication failure, closing: {exc}", flush=True)
                break
            if payload is None:
                break
            if self.on_frame is not None:
                self.on_frame(payload)

    def send(self, payload: bytes) -> None:
        with self._send_lock:
            if not self._connected or not self._running:
                raise ConnectionError("link is not connected")
            message = bytes(payload)
            if self._auth is not None:
                message = self._auth.seal(message)
            seq = self._next_tx_seq
            self._next_tx_seq += 1
            fragments = [
                message[offset : offset + self._fragment_size]
                for offset in range(0, len(message), self._fragment_size)
            ] or [b""]
            frag_count = len(fragments)
            if frag_count > 65535:
                raise ValueError("message requires too many fragments")
            pending = set(range(frag_count))
            for _ in range(self._max_retries):
                for frag_index in sorted(pending):
                    self._backend.send(
                        self._packet(
                            _DATA,
                            seq=seq,
                            frag_index=frag_index,
                            frag_count=frag_count,
                            payload=fragments[frag_index],
                        )
                    )
                deadline = monotonic() + self._ack_timeout
                with self._ack_cv:
                    while pending and self._running:
                        pending = {
                            index for index in pending if (seq, index) not in self._acked
                        }
                        if not pending:
                            break
                        remaining = deadline - monotonic()
                        if remaining <= 0:
                            break
                        self._ack_cv.wait(remaining)
                if not pending:
                    with self._state_lock:
                        self._acked.difference_update((seq, index) for index in range(frag_count))
                    self.tx_count += 1
                    return
            error = TimeoutError(f"message {seq} was not acknowledged after retries")
            self._error = error
            self.close()
            raise error

    def _io_loop(self) -> None:
        while self._running:
            packet = self._backend.recv(timeout=self._ack_timeout)
            if packet is None:
                break
            if not packet:
                continue
            self._handle_packet(packet)
        if self._running:
            self.close()

    def _handle_packet(self, packet: bytes) -> None:
        if len(packet) < _HEADER.size:
            return
        magic, version, packet_type, seq, frag_index, frag_count = _HEADER.unpack_from(packet)
        if magic != _MAGIC or version != _VERSION:
            return
        payload = packet[_HEADER.size :]
        if packet_type == _HELLO:
            self._backend.send(self._packet(_HELLO_ACK))
            with self._handshake_cv:
                self._connected = True
                self._handshake_cv.notify_all()
        elif packet_type == _HELLO_ACK:
            with self._handshake_cv:
                self._connected = True
                self._handshake_cv.notify_all()
        elif packet_type == _ACK:
            with self._ack_cv:
                self._acked.add((seq, frag_index))
                self._ack_cv.notify_all()
        elif packet_type == _DATA:
            self._handle_data(seq, frag_index, frag_count, payload)

    def _handle_data(self, seq: int, frag_index: int, frag_count: int, payload: bytes) -> None:
        if frag_count == 0 or frag_index >= frag_count:
            return
        self._backend.send(self._packet(_ACK, seq=seq, frag_index=frag_index))
        with self._delivery_cv:
            if seq < self._next_rx_seq or seq in self._completed:
                return
            existing = self._assemblies.get(seq)
            if existing is None:
                existing = (frag_count, {})
                self._assemblies[seq] = existing
            expected_count, fragments = existing
            if expected_count != frag_count:
                return
            fragments.setdefault(frag_index, payload)
            if len(fragments) == frag_count:
                self._completed[seq] = b"".join(fragments[index] for index in range(frag_count))
                del self._assemblies[seq]
                while self._next_rx_seq in self._completed:
                    self._deliveries.put(self._completed.pop(self._next_rx_seq))
                    self._next_rx_seq += 1
                self._delivery_cv.notify_all()

    @staticmethod
    def _packet(
        packet_type: int,
        *,
        seq: int = 0,
        frag_index: int = 0,
        frag_count: int = 0,
        payload: bytes = b"",
    ) -> bytes:
        return _HEADER.pack(
            _MAGIC,
            _VERSION,
            packet_type,
            seq,
            frag_index,
            frag_count,
        ) + payload

    def close(self) -> None:
        if not self._running and not self._connected:
            return
        self._running = False
        self._connected = False
        self._backend.close()
        self._deliveries.put(_STOP)
        with self._handshake_cv:
            self._handshake_cv.notify_all()
        with self._ack_cv:
            self._ack_cv.notify_all()
