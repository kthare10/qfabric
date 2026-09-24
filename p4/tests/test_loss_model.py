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

"""PTF data plane test for P4 quantum channel loss model.

Sends 10K photon packets through BMv2 and verifies that the drop rate
matches the installed loss threshold within 2 standard deviations. Also
checks that EtherType 0x7102 classical frames are forwarded without loss.

Requires: PTF (pip install ptf), BMv2 running with veth topology, and
simple_switch_CLI on PATH to read the installed wavelength-0 threshold.
Usage: sudo ptf --test-dir p4/tests --pypath . --interface 0@veth1 --interface 1@veth3
"""

import math
import re
import struct
import subprocess
import time

import ptf
import ptf.testutils as testutils
from ptf.base_tests import BaseTest

ETHERTYPE_PHOTON = 0x7101
ETHERTYPE_CLASSICAL = 0x7102
ALICE_MAC = "02:00:00:00:00:01"
BOB_MAC = "02:00:00:00:00:02"


def build_photon_frame(seq_num, basis=0, state=0, wavelength=0):
    """Build a raw photon Ethernet frame."""
    dst = bytes.fromhex(BOB_MAC.replace(":", ""))
    src = bytes.fromhex(ALICE_MAC.replace(":", ""))
    eth_hdr = dst + src + struct.pack("!H", ETHERTYPE_PHOTON)
    photon_hdr = struct.pack(
        "!4B3IB",
        0x01,        # version
        basis,
        state,
        wavelength,
        seq_num,
        0,           # timestamp_hi
        0,           # timestamp_lo
        0,           # padding
    )
    # Pad to 60 bytes minimum
    frame = eth_hdr + photon_hdr
    if len(frame) < 60:
        frame += b"\x00" * (60 - len(frame))
    return frame


def _count_received_packets(test, port, ethertype):
    """Count matching frames until PTF reports a receive timeout."""
    received = 0
    ether_type_bytes = struct.pack("!H", ethertype)
    while True:
        try:
            result = testutils.dp_poll(test, timeout=0.1)
        except TimeoutError:
            break
        if isinstance(result, test.dataplane.PollFailure):
            break
        if isinstance(result, tuple) and not hasattr(result, "packet"):
            received_port, packet = result[1:3]
        else:
            received_port, packet = result.port, result.packet
        if packet is None:
            break
        if received_port == port and bytes(packet)[12:14] == ether_type_bytes:
            received += 1
    return received


class QuantumChannelLossTest(BaseTest):
    """Test that P4 quantum channel drops photons at the configured rate."""

    NUM_PACKETS = 10_000
    SIGMA_TOLERANCE = 2  # Accept within 2 standard deviations

    def setUp(self):
        BaseTest.setUp(self)
        self.dataplane = ptf.dataplane_instance
        self.dataplane.flush()
        # Port 0 = veth1 (Alice), Port 1 = veth3 (Bob)
        self.alice_port = 0
        self.bob_port = 1
        dump = subprocess.run(
            ["simple_switch_CLI", "--thrift-port", "9090"],
            input="table_dump_entry_from_key quantum_channel_params 0\n",
            capture_output=True, text=True, check=True,
        ).stdout
        threshold_match = re.search(
            r"\bset_channel_params\b\s*-\s*(0x[0-9a-f]+|[0-9]+)\b", dump, re.IGNORECASE,
        )
        if threshold_match is None:
            raise RuntimeError(f"Cannot read installed photon-loss threshold:\n{dump}")
        self.EXPECTED_LOSS_RATE = int(threshold_match.group(1), 0) / 2**32

    def runTest(self):
        for seq in range(self.NUM_PACKETS):
            frame = build_photon_frame(seq_num=seq)
            testutils.send_packet(self, self.alice_port, frame)

        # Wait and count received packets
        # Use a timeout to collect all packets that arrive
        time.sleep(2)
        received = _count_received_packets(self, self.bob_port, ETHERTYPE_PHOTON)

        dropped = self.NUM_PACKETS - received
        observed_loss_rate = dropped / self.NUM_PACKETS

        # Statistical validation
        expected_drops = self.EXPECTED_LOSS_RATE * self.NUM_PACKETS
        sigma = math.sqrt(
            self.EXPECTED_LOSS_RATE * (1 - self.EXPECTED_LOSS_RATE) * self.NUM_PACKETS
        )
        lower_bound = expected_drops - self.SIGMA_TOLERANCE * sigma
        upper_bound = expected_drops + self.SIGMA_TOLERANCE * sigma

        print("\n=== Quantum Channel Loss Test Results ===")
        print(f"  Packets sent:     {self.NUM_PACKETS}")
        print(f"  Packets received: {received}")
        print(f"  Packets dropped:  {dropped}")
        print(f"  Observed loss:    {observed_loss_rate:.4f}")
        print(f"  Expected loss:    {self.EXPECTED_LOSS_RATE:.4f}")
        print(f"  Expected drops:   {expected_drops:.0f} ± {self.SIGMA_TOLERANCE * sigma:.0f}")
        print(f"  Acceptable range: [{lower_bound:.0f}, {upper_bound:.0f}]")

        assert lower_bound <= dropped <= upper_bound, (
            f"Drop count {dropped} outside {self.SIGMA_TOLERANCE}σ range "
            f"[{lower_bound:.0f}, {upper_bound:.0f}]"
        )


class ClassicalChannelForwardingTest(BaseTest):
    """Test that EtherType 0x7102 frames bypass photon loss."""

    NUM_PACKETS = 100

    def setUp(self):
        BaseTest.setUp(self)
        self.dataplane = ptf.dataplane_instance
        self.dataplane.flush()
        self.alice_port = 0
        self.bob_port = 1

    def runTest(self):
        ethernet_header = (
            bytes.fromhex(BOB_MAC.replace(":", ""))
            + bytes.fromhex(ALICE_MAC.replace(":", ""))
            + struct.pack("!H", ETHERTYPE_CLASSICAL)
        )
        for sequence_num in range(self.NUM_PACKETS):
            frame = (ethernet_header + struct.pack("!I", sequence_num)).ljust(60, b"\x00")
            testutils.send_packet(self, self.alice_port, frame)

        time.sleep(2)
        received = _count_received_packets(self, self.bob_port, ETHERTYPE_CLASSICAL)
        assert received == self.NUM_PACKETS, (
            f"Classical channel lost {self.NUM_PACKETS - received} of {self.NUM_PACKETS} frames"
        )
