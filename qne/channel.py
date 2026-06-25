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

"""Classical TCP channel for BB84 sifting messages.

Alice and Bob exchange basis lists and sifting results over a standard
TCP connection. On FABRIC, this traffic traverses real WAN links,
introducing realistic classical-quantum feedback latency.
"""

from __future__ import annotations

import json
import socket
import struct
from typing import Any


class ClassicalChannel:
    """TCP-based classical channel for BB84 sifting.

    Protocol: length-prefixed JSON messages.
    Each message: [4-byte big-endian length][JSON payload]
    """

    def __init__(self, sock: socket.socket):
        self._sock = sock

    def send_message(self, msg: dict[str, Any]) -> None:
        """Send a JSON message with length prefix."""
        data = json.dumps(msg).encode("utf-8")
        header = struct.pack("!I", len(data))
        self._sock.sendall(header + data)

    def recv_message(self) -> dict[str, Any]:
        """Receive a length-prefixed JSON message."""
        header = self._recv_exact(4)
        length = struct.unpack("!I", header)[0]
        data = self._recv_exact(length)
        return json.loads(data.decode("utf-8"))

    def _recv_exact(self, n: int) -> bytes:
        """Receive exactly n bytes."""
        buf = bytearray()
        while len(buf) < n:
            chunk = self._sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("Connection closed while receiving data")
            buf.extend(chunk)
        return bytes(buf)

    def close(self) -> None:
        self._sock.close()


class ClassicalServer:
    """TCP server side of the classical channel (Bob)."""

    def __init__(self, host: str = "0.0.0.0", port: int = 5100):
        self.host = host
        self.port = port
        self._server_sock: socket.socket | None = None

    def start(self) -> None:
        # Detect address family from the host parameter
        if ":" in self.host or self.host == "":
            # IPv6 address or empty (bind all)
            family = socket.AF_INET6
        else:
            # IPv4 address (e.g., 0.0.0.0, 10.10.1.2)
            family = socket.AF_INET

        self._server_sock = socket.socket(family, socket.SOCK_STREAM)
        self._server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

        if family == socket.AF_INET6:
            # Allow dual-stack (IPv4-mapped IPv6)
            try:
                self._server_sock.setsockopt(
                    socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0
                )
            except (AttributeError, OSError):
                pass

        self._server_sock.bind((self.host, self.port))
        self._server_sock.listen(1)

    def accept(self) -> ClassicalChannel:
        """Wait for Alice to connect."""
        if self._server_sock is None:
            raise RuntimeError("Server not started")
        conn, _addr = self._server_sock.accept()
        return ClassicalChannel(conn)

    def close(self) -> None:
        if self._server_sock:
            self._server_sock.close()


class ClassicalClient:
    """TCP client side of the classical channel (Alice)."""

    @staticmethod
    def connect(
        host: str,
        port: int = 5100,
        timeout: float = 30.0,
        max_retries: int = 30,
        retry_delay: float = 2.0,
    ) -> ClassicalChannel:
        """Connect to Bob's classical channel server with retries.

        Retries with fixed delay to handle the case where Bob is still
        receiving photons and hasn't started the classical server yet.
        """
        import time

        infos = socket.getaddrinfo(host, port, socket.AF_UNSPEC, socket.SOCK_STREAM)
        if not infos:
            raise ConnectionError(f"Cannot resolve {host}:{port}")

        last_err = None
        for attempt in range(max_retries):
            for family, socktype, proto, canonname, sockaddr in infos:
                try:
                    sock = socket.socket(family, socktype, proto)
                    sock.settimeout(timeout)
                    sock.connect(sockaddr)
                    return ClassicalChannel(sock)
                except OSError as e:
                    last_err = e
                    sock.close()
            if attempt < max_retries - 1:
                print(f"  Retrying connection to {host}:{port} "
                      f"(attempt {attempt + 2}/{max_retries})...")
                time.sleep(retry_delay)

        raise ConnectionError(f"Cannot connect to {host}:{port} "
                              f"after {max_retries} attempts: {last_err}")
