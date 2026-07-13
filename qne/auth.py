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

"""Authenticated classical channel — per-frame MAC + anti-replay sequence numbers.

BB84's security proof requires the classical channel to be *authenticated* (not
secret): an adversary who can rewrite sifting/reconciliation traffic can
man-in-the-middle the whole key exchange. Production QKD authenticates with
information-theoretic Wegman–Carter MACs keyed from previously distilled secret;
we model the same wire protocol with HMAC-SHA256 under a pre-shared key —
computationally rather than information-theoretically secure, but byte-for-byte
the same framing overhead and round-trip structure, which is what the emulator
measures.

Frame layout (prepended to the existing length-prefixed payload):

    [8-byte big-endian seq][16-byte truncated HMAC-SHA256 tag][payload]

The tag covers seq+payload. The receiver enforces *strictly sequential* sequence
numbers: TCP already delivers in order, so any gap, repeat, or reorder can only
mean tampering (or a second injected connection) and raises ``AuthError``.
"""

from __future__ import annotations

import hashlib
import hmac
import struct

_SEQ = struct.Struct("!Q")
TAG_LEN = 16                       # truncated HMAC-SHA256
HEADER_LEN = _SEQ.size + TAG_LEN


class AuthError(Exception):
    """A frame failed authentication (bad tag, replay, reorder, or truncation)."""


class FrameAuthenticator:
    """Seals outbound frames and verifies inbound ones under a pre-shared key.

    One instance per direction-pair per connection: ``seal`` numbers outbound
    frames 0,1,2,…; ``open`` requires inbound frames to arrive in exactly that
    order. Not thread-safe — callers serialize (both transports already hold a
    send lock / single RX thread).
    """

    def __init__(self, key: bytes | str):
        self._key = key.encode("utf-8") if isinstance(key, str) else bytes(key)
        if not self._key:
            raise ValueError("auth key must be non-empty")
        self._tx_seq = 0
        self._rx_seq = 0

    def _tag(self, seq_hdr: bytes, payload: bytes) -> bytes:
        return hmac.new(self._key, seq_hdr + payload, hashlib.sha256).digest()[:TAG_LEN]

    def seal(self, payload: bytes) -> bytes:
        hdr = _SEQ.pack(self._tx_seq)
        self._tx_seq += 1
        return hdr + self._tag(hdr, payload) + payload

    def open(self, frame: bytes) -> bytes:
        if len(frame) < HEADER_LEN:
            raise AuthError(f"frame too short for auth header ({len(frame)} bytes)")
        hdr, tag = frame[:_SEQ.size], frame[_SEQ.size:HEADER_LEN]
        payload = frame[HEADER_LEN:]
        if not hmac.compare_digest(tag, self._tag(hdr, payload)):
            raise AuthError("bad MAC — frame tampered with or wrong pre-shared key")
        (seq,) = _SEQ.unpack(hdr)
        if seq != self._rx_seq:
            raise AuthError(f"sequence break (got {seq}, expected {self._rx_seq}) "
                            "— replayed, reordered, or dropped frame")
        self._rx_seq += 1
        return payload
