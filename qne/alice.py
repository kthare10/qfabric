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

"""Alice — BB84 photon source and sender.

Generates random basis+state photons, sends them as custom Ethernet
frames through a raw socket, and performs classical sifting with Bob.
"""

from __future__ import annotations

import socket
import time

import numpy as np

from qne.bb84 import AliceRecord
from qne.channel import ClassicalClient
from qne.config import ScenarioConfig
from qne.metrics import MetricsCollector
from qne.photon import PhotonPacket
from qne.reconcile import ChannelRpc, bits_to_int, serve_parities


class Alice:
    """BB84 sender (photon source).

    Attributes:
        config: Experiment scenario configuration.
        interface: Network interface for raw socket (e.g., "veth1").
        bob_host: Bob's IP/hostname for classical channel.
        bob_port: Bob's classical channel port.
    """

    def __init__(
        self,
        config: ScenarioConfig,
        interface: str = "veth1",
        bob_host: str = "127.0.0.1",
        bob_port: int = 5100,
        dst_mac: bytes | None = None,
        src_mac: bytes | None = None,
        auth_key: bytes | str | None = None,
    ):
        self.config = config
        self.interface = interface
        self.bob_host = bob_host
        self.bob_port = bob_port
        self.auth_key = auth_key
        self.rng = np.random.default_rng(config.seed)
        self.sent_log: list[AliceRecord] = []
        self.final_key: int | None = None    # extracted secret (post Cascade + PA)
        self.collector = MetricsCollector(config.name)

        # Destination MAC for photon frames (default: dummy, override for FABRIC)
        self.dst_mac = dst_mac or b"\x02\x00\x00\x00\x00\x02"
        self.src_mac = src_mac or b"\x02\x00\x00\x00\x00\x01"

    def run(self) -> None:
        """Execute the full BB84 sender protocol.

        1. Send photon packets through raw socket.
        2. Connect to Bob's classical channel.
        3. Exchange basis info and perform sifting.
        4. Compute QBER and key rate.
        """
        self.collector.start()
        self.collector.set_config(self.config.to_dict())

        # Phase 1: Send photons
        self._send_photons()

        # Small delay to let photons arrive
        time.sleep(1.0)

        # Phase 2: Classical sifting
        self._run_sifting()

        metrics = self.collector.finalize()
        print("\n=== Alice Results ===")
        print(f"  Photons sent:    {metrics.photons_sent}")
        print(f"  Sifted bits:     {metrics.sifted_bits}")
        print(f"  QBER:            {metrics.qber:.4f}")
        print(f"  Secure key rate: {metrics.secure_key_rate:.4f}")
        print(f"  Final key bits:  {metrics.final_key_bits}")
        if metrics.reconciled:
            print(f"  Reconciled:      yes ({metrics.corrections} corrections, "
                  f"{metrics.bits_leaked} bits leaked)")
            print(f"  Secure key bits: {metrics.secure_key_bits}")
        print(f"  Elapsed:         {metrics.elapsed_seconds:.2f}s")

        return metrics

    def _send_photons(self) -> None:
        """Generate and send photon packets via raw socket."""
        num_photons = self.config.protocol.num_photons
        wavelength = self.config.protocol.wavelength
        send_interval = 1.0 / self.config.protocol.send_rate_hz

        # Open raw socket
        sock = socket.socket(
            socket.AF_PACKET, socket.SOCK_RAW, socket.htons(0x7101)
        )
        sock.bind((self.interface, 0))

        print(f"Alice: Sending {num_photons} photons on {self.interface}")

        bias = self.config.protocol.basis_bias   # P(Z); 0.5 = standard BB84
        for seq in range(num_photons):
            basis = 0 if self.rng.random() < bias else 1
            state = int(self.rng.integers(0, 2))

            pkt = PhotonPacket(
                basis=basis,
                state=state,
                sequence_num=seq,
                wavelength=wavelength,
            )

            frame = pkt.to_ethernet_frame(dst_mac=self.dst_mac, src_mac=self.src_mac)
            sock.send(frame)

            self.sent_log.append(AliceRecord(
                sequence_num=seq,
                basis=basis,
                bit_value=state,
            ))
            self.collector.record_sent()

            # Rate limiting (skip for max-rate operation)
            if send_interval > 1e-6:
                time.sleep(send_interval)

        sock.close()
        print(f"Alice: Finished sending {num_photons} photons")

    def _run_sifting(self) -> None:
        """Connect to Bob; sift, answer the sample request, then serve Cascade.

        Bob picks the disclosed QBER sample (Alice reveals ONLY those bits) and
        drives Cascade; Alice answers parity queries and applies the announced
        Toeplitz hash, so both extract the identical secret (qne.reconcile).
        """
        print(f"Alice: Connecting to Bob at {self.bob_host}:{self.bob_port}")
        channel = ClassicalClient.connect(self.bob_host, self.bob_port,
                                          auth_key=self.auth_key)

        try:
            # Send Alice's basis list to Bob
            basis_list = {
                rec.sequence_num: rec.basis for rec in self.sent_log
            }
            channel.send_message({
                "type": "alice_bases",
                "bases": basis_list,
            })

            # Receive sifting result from Bob
            msg = channel.recv_message()
            assert msg["type"] == "sifting_result"

            matching_seqs = msg["matching_indices"]
            bob_detected_seqs = set(msg["detected_sequences"])

            # Index sent log for fast lookup (used to answer Bob's sample request).
            sent_by_seq = {rec.sequence_num: rec for rec in self.sent_log}

            # Bob requests a random SAMPLE for QBER; only those bits are revealed.
            sample: list[int] = []
            sample_req = channel.recv_message()
            if sample_req.get("type") == "request_sample":
                sample = [int(s) for s in sample_req["sample_indices"]]
                channel.send_message({
                    "type": "alice_sample_bits",
                    "bits": [sent_by_seq[seq].bit_value for seq in sample],
                })

            # Receive QBER estimate from Bob
            qber_msg = channel.recv_message()
            assert qber_msg["type"] == "qber_result"

            # Key = sifted minus disclosed, in the shared sorted order; serve
            # Cascade parities over it if Bob decided the run is reconcilable.
            sample_set = set(sample)
            key_order = [s for s in matching_seqs if s not in sample_set]
            key_bits = [sent_by_seq[s].bit_value for s in key_order]
            reconciled = False
            corrections = bits_leaked = 0
            final = key_bits
            if qber_msg.get("reconcile"):
                final, corrections, bits_leaked = serve_parities(
                    ChannelRpc(channel), key_bits)
                reconciled = True
            self.final_key = bits_to_int(final) if reconciled else None

            self.collector.record_received(len(bob_detected_seqs))
            self.collector.set_sifting_results(
                sifted_bits=len(matching_seqs),
                qber=qber_msg["qber"],
                confidence=tuple(qber_msg["confidence_interval"]),
            )
            self.collector.set_key_rate(
                raw_rate=qber_msg["raw_key_rate"],
                secure_rate=qber_msg["secure_key_rate"],
                final_bits=qber_msg["final_key_bits"],
            )
            self.collector.set_reconciliation(
                reconciled, corrections, bits_leaked,
                len(final) if reconciled else 0,
            )

        finally:
            channel.close()
