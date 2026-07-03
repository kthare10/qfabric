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
matches the configured loss threshold within 2 standard deviations.

Requires: PTF (pip install ptf), BMv2 running with veth topology.
Usage: sudo ptf --test-dir p4/tests --pypath . --interface 0@veth1 --interface 1@veth3
"""

import math
import struct

import ptf.testutils as testutils
from ptf.base_tests import BaseTest

ETHERTYPE_PHOTON = 0x7101
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


class QuantumChannelLossTest(BaseTest):
    """Test that P4 quantum channel drops photons at the configured rate."""

    NUM_PACKETS = 10_000
    # Default threshold for 1km @ 0.2 dB/km: ~4.5% loss
    EXPECTED_LOSS_RATE = 1.0 - 10 ** (-0.2 * 1.0 / 10.0)
    SIGMA_TOLERANCE = 2  # Accept within 2 standard deviations

    def setUp(self):
        BaseTest.setUp(self)
        # Port 0 = veth1 (Alice), Port 1 = veth3 (Bob)
        self.alice_port = 0
        self.bob_port = 1

    def runTest(self):
        received = 0

        for seq in range(self.NUM_PACKETS):
            frame = build_photon_frame(seq_num=seq)
            testutils.send_packet(self, self.alice_port, frame)

        # Wait and count received packets
        # Use a timeout to collect all packets that arrive
        import time
        time.sleep(2)

        while True:
            try:
                (port, pkt) = testutils.dp_poll(self, timeout=0.1)
                if port == self.bob_port:
                    received += 1
            except Exception:
                break

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
