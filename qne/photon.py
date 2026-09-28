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

"""Photon packet data structure and wire format.

Custom EtherType 0x7101 photon frames for P4 quantum channel emulation.
Photon header is 17 bytes after the 14-byte Ethernet header.

The last header byte is the pulse's photon count (a weak-coherent source emits
Poisson(mu) photons per pulse). Legacy frames wrote 0 there; 0 and 1 both mean a
single photon. The P4 switch thins the count per photon and forwards the survivor
count, so a decoy-state run can ride the same 0x7101 path as plain BB84.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import IntEnum

ETHERTYPE_PHOTON = 0x7101
PHOTON_VERSION = 0x01

# Ethernet (14) + Photon header (17) = 31 bytes minimum frame
ETHERNET_HDR_LEN = 14
PHOTON_HDR_LEN = 17
PHOTON_HDR_FORMAT = "!4B3IB"
# Largest photon count the frame (and the P4 program's unrolled thinning) carries.
MAX_PHOTON_COUNT = 8


class Basis(IntEnum):
    """Measurement/preparation basis."""
    Z = 0  # Rectilinear (|0>, |1>)
    X = 1  # Diagonal (|+>, |->)


class State(IntEnum):
    """Qubit state within a basis."""
    ZERO = 0  # |0> in Z basis, |+> in X basis
    ONE = 1   # |1> in Z basis, |-> in X basis


@dataclass
class PhotonPacket:
    """Represents a single photon packet for BB84 QKD.

    Attributes:
        basis: Preparation/measurement basis (Z or X).
        state: Qubit state (0 or 1 within the basis).
        sequence_num: Monotonic photon identifier.
        wavelength: Channel tag for future WDM support.
        timestamp_hi: Upper 32 bits of TX timestamp (picoseconds).
        timestamp_lo: Lower 32 bits of TX timestamp (picoseconds).
        photon_count: Photons in this pulse (1 for an ideal single-photon source;
            a decoy-state source sends Poisson(mu) counts). Capped at
            MAX_PHOTON_COUNT on the wire; 0 on the wire is read as 1.
        version: Protocol version.
    """
    basis: int
    state: int
    sequence_num: int
    wavelength: int = 0
    timestamp_hi: int = 0
    timestamp_lo: int = 0
    photon_count: int = 1
    version: int = PHOTON_VERSION

    @property
    def bit_value(self) -> int:
        """The classical bit value encoded in this photon."""
        return self.state

    def to_bytes(self) -> bytes:
        """Serialize the photon header to 17 bytes."""
        return struct.pack(
            PHOTON_HDR_FORMAT,
            self.version,
            self.basis,
            self.state,
            self.wavelength,
            self.sequence_num,
            self.timestamp_hi,
            self.timestamp_lo,
            max(1, min(int(self.photon_count), MAX_PHOTON_COUNT)),
        )

    @classmethod
    def from_bytes(cls, data: bytes) -> PhotonPacket:
        """Deserialize a photon header from 17 bytes."""
        if len(data) < PHOTON_HDR_LEN:
            raise ValueError(f"Need at least {PHOTON_HDR_LEN} bytes, got {len(data)}")
        version, basis, state, wavelength, seq, ts_hi, ts_lo, count = struct.unpack(
            PHOTON_HDR_FORMAT, data[:PHOTON_HDR_LEN]
        )
        return cls(
            version=version,
            basis=basis,
            state=state,
            wavelength=wavelength,
            sequence_num=seq,
            timestamp_hi=ts_hi,
            timestamp_lo=ts_lo,
            photon_count=count if count > 0 else 1,   # legacy padding byte = 1 photon
        )

    def to_ethernet_frame(
        self, dst_mac: bytes = b"\xff\xff\xff\xff\xff\xff",
        src_mac: bytes = b"\x00\x00\x00\x00\x00\x01",
    ) -> bytes:
        """Build a complete Ethernet frame with photon payload.

        Args:
            dst_mac: 6-byte destination MAC address.
            src_mac: 6-byte source MAC address.

        Returns:
            Complete Ethernet frame bytes (minimum 60 bytes with padding).
        """
        eth_header = dst_mac + src_mac + struct.pack("!H", ETHERTYPE_PHOTON)
        frame = eth_header + self.to_bytes()
        # Pad to minimum Ethernet frame size (60 bytes without FCS)
        if len(frame) < 60:
            frame += b"\x00" * (60 - len(frame))
        return frame

    @classmethod
    def from_ethernet_frame(cls, frame: bytes) -> PhotonPacket:
        """Parse a photon packet from an Ethernet frame.

        Args:
            frame: Raw Ethernet frame bytes.

        Returns:
            Parsed PhotonPacket.

        Raises:
            ValueError: If EtherType is not 0x7101.
        """
        if len(frame) < ETHERNET_HDR_LEN + PHOTON_HDR_LEN:
            raise ValueError(
                f"Frame too short: need {ETHERNET_HDR_LEN + PHOTON_HDR_LEN} bytes, "
                f"got {len(frame)}"
            )
        ethertype = struct.unpack("!H", frame[12:14])[0]
        if ethertype != ETHERTYPE_PHOTON:
            raise ValueError(f"Expected EtherType 0x{ETHERTYPE_PHOTON:04x}, got 0x{ethertype:04x}")
        return cls.from_bytes(frame[ETHERNET_HDR_LEN:])
