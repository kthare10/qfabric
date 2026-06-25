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

"""Tests for photon packet serialization and Ethernet framing."""

import struct

import pytest

from qne.photon import (
    ETHERTYPE_PHOTON,
    PHOTON_HDR_LEN,
    Basis,
    PhotonPacket,
    State,
)


class TestPhotonSerialization:
    """Test PhotonPacket to_bytes / from_bytes round-trip."""

    def test_round_trip_basic(self):
        pkt = PhotonPacket(basis=Basis.Z, state=State.ZERO, sequence_num=1)
        data = pkt.to_bytes()
        assert len(data) == PHOTON_HDR_LEN
        restored = PhotonPacket.from_bytes(data)
        assert restored.basis == pkt.basis
        assert restored.state == pkt.state
        assert restored.sequence_num == pkt.sequence_num
        assert restored.version == pkt.version

    def test_round_trip_all_fields(self):
        pkt = PhotonPacket(
            basis=Basis.X,
            state=State.ONE,
            sequence_num=0xDEADBEEF,
            wavelength=42,
            timestamp_hi=0x12345678,
            timestamp_lo=0x9ABCDEF0,
        )
        restored = PhotonPacket.from_bytes(pkt.to_bytes())
        assert restored.basis == Basis.X
        assert restored.state == State.ONE
        assert restored.sequence_num == 0xDEADBEEF
        assert restored.wavelength == 42
        assert restored.timestamp_hi == 0x12345678
        assert restored.timestamp_lo == 0x9ABCDEF0

    def test_from_bytes_too_short(self):
        with pytest.raises(ValueError, match="Need at least"):
            PhotonPacket.from_bytes(b"\x00" * 5)

    def test_bit_value(self):
        assert PhotonPacket(basis=Basis.Z, state=State.ZERO, sequence_num=0).bit_value == 0
        assert PhotonPacket(basis=Basis.X, state=State.ONE, sequence_num=0).bit_value == 1


class TestEthernetFrame:
    """Test Ethernet frame construction and parsing."""

    def test_frame_minimum_size(self):
        pkt = PhotonPacket(basis=Basis.Z, state=State.ZERO, sequence_num=0)
        frame = pkt.to_ethernet_frame()
        assert len(frame) >= 60  # Minimum Ethernet frame size

    def test_frame_ethertype(self):
        pkt = PhotonPacket(basis=Basis.Z, state=State.ZERO, sequence_num=0)
        frame = pkt.to_ethernet_frame()
        ethertype = struct.unpack("!H", frame[12:14])[0]
        assert ethertype == ETHERTYPE_PHOTON

    def test_frame_round_trip(self):
        pkt = PhotonPacket(
            basis=Basis.X, state=State.ONE, sequence_num=12345, wavelength=7
        )
        dst = b"\x02\x00\x00\x00\x00\x02"
        src = b"\x02\x00\x00\x00\x00\x01"
        frame = pkt.to_ethernet_frame(dst_mac=dst, src_mac=src)
        restored = PhotonPacket.from_ethernet_frame(frame)
        assert restored.basis == Basis.X
        assert restored.state == State.ONE
        assert restored.sequence_num == 12345
        assert restored.wavelength == 7

    def test_frame_wrong_ethertype(self):
        # Build a frame with wrong EtherType
        frame = (b"\xff" * 6) + (b"\x00" * 6) + struct.pack("!H", 0x0800) + (b"\x00" * 44)
        with pytest.raises(ValueError, match="Expected EtherType"):
            PhotonPacket.from_ethernet_frame(frame)

    def test_frame_too_short(self):
        with pytest.raises(ValueError, match="Frame too short"):
            PhotonPacket.from_ethernet_frame(b"\x00" * 10)

    def test_mac_addresses_preserved(self):
        pkt = PhotonPacket(basis=Basis.Z, state=State.ZERO, sequence_num=0)
        dst = b"\xaa\xbb\xcc\xdd\xee\xff"
        src = b"\x11\x22\x33\x44\x55\x66"
        frame = pkt.to_ethernet_frame(dst_mac=dst, src_mac=src)
        assert frame[:6] == dst
        assert frame[6:12] == src
